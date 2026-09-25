import asyncio
import importlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import server

OK = '{"result":{},"meta":{}}'
_tmp = None

def setUpModule():
    # Keep test calls out of the real audit log.
    global _tmp
    _tmp = tempfile.TemporaryDirectory()
    os.environ['APPLE_MAIL_AUDIT_LOG'] = os.path.join(_tmp.name, 'audit.log')

def tearDownModule():
    os.environ.pop('APPLE_MAIL_AUDIT_LOG', None)
    _tmp.cleanup()

def payload_of(run):
    script = run.call_args.kwargs['input']
    literal = script.split('JSON.parse(', 1)[1].split(');\n', 1)[0]
    return json.loads(json.loads(literal))

class BridgeTests(unittest.TestCase):
    def test_payload_is_data(self):
        hostile = '\"); throw Error(\"injected\"); //\n🍎'
        with patch.object(server.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, OK, '')) as run:
            server.call_mail('search', query=hostile)
            self.assertEqual(payload_of(run)['query'], hostile)
            self.assertEqual(run.call_args.args[0], ['/usr/bin/osascript', '-l', 'JavaScript', '-'])

    def test_permission_error(self):
        with patch.object(server.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'denied (-1743)')):
            with self.assertRaisesRegex(RuntimeError, 'Automation'):
                server.list_accounts()

    def test_timeout(self):
        with patch.object(server.subprocess, 'run', side_effect=subprocess.TimeoutExpired('osascript', 45)):
            with self.assertRaisesRegex(RuntimeError, 'partially completed'):
                server.list_accounts()

    def test_invalid_recipient(self):
        with patch.object(server.subprocess, 'run') as run:
            with self.assertRaises(ValueError):
                server.create_draft(['x@example.com\nBcc:evil@example.com'], 'test', 'body')
            run.assert_not_called()

class AllowlistTests(unittest.TestCase):
    def run_with_env(self, value):
        env = {k: v for k, v in os.environ.items() if k != 'APPLE_MAIL_ACCOUNTS'}
        if value is not None:
            env['APPLE_MAIL_ACCOUNTS'] = value
        return patch.dict(os.environ, env, clear=True)

    def sent_allowlist(self):
        with patch.object(server, 'audit'), \
             patch.object(server.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, OK, '')) as run:
            server.call_mail('accounts')
            return payload_of(run)['allowed_emails']

    def test_unset_allows_every_account(self):
        with self.run_with_env(None):
            self.assertIsNone(self.sent_allowlist())

    def test_addresses_are_normalized(self):
        with self.run_with_env(' A@X.com, b@y.org ,A@x.com'):
            self.assertEqual(self.sent_allowlist(), ['a@x.com', 'b@y.org'])

    def test_set_but_empty_fails_closed(self):
        for value in ('', ' , ,'):
            with self.run_with_env(value), patch.object(server.subprocess, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'set but empty'):
                    server.list_accounts()
                run.assert_not_called()

    def test_non_address_entry_rejected(self):
        with self.run_with_env('not-an-address'), patch.object(server.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'email addresses'):
                server.list_accounts()
            run.assert_not_called()

    def test_caller_cannot_override_allowlist(self):
        with self.run_with_env('a@x.com'), patch.object(server, 'audit'), \
             patch.object(server.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, OK, '')) as run:
            server.call_mail('accounts', allowed_emails=None)
            self.assertEqual(payload_of(run)['allowed_emails'], ['a@x.com'])

    def test_list_accounts_cli_path_is_unrestricted(self):
        with self.run_with_env('a@x.com'), patch.object(server, 'audit'), \
             patch.object(server.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, OK, '')) as run:
            server.call_mail('accounts', _all_accounts=True)
            self.assertIsNone(payload_of(run)['allowed_emails'])

class DraftGatingTests(unittest.TestCase):
    def tool_names(self, allow):
        env = {k: v for k, v in os.environ.items() if k != 'APPLE_MAIL_ALLOW_DRAFTS'}
        if allow is not None:
            env['APPLE_MAIL_ALLOW_DRAFTS'] = allow
        with patch.dict(os.environ, env, clear=True):
            importlib.reload(server)
            return {t.name for t in asyncio.run(server.mcp.list_tools())}

    def tearDown(self):
        importlib.reload(server)

    def test_read_only_by_default(self):
        self.assertEqual(self.tool_names(None), {'list_accounts', 'list_mailboxes', 'search_messages', 'read_message'})

    def test_only_exact_one_enables_drafts(self):
        for value in ('0', 'true', 'yes', ''):
            self.assertNotIn('create_draft', self.tool_names(value), value)
        self.assertIn('create_draft', self.tool_names('1'))

class AuditTests(unittest.TestCase):
    def test_log_records_ids_and_counts_never_content(self):
        stdout = json.dumps({'result': {'messages': [{'id': 7, 'subject': 'SECRET-SUBJECT', 'sender': 'SECRET-SENDER'}],
                                        'next_offset': None},
                             'meta': {'account': 'me@example.com'}})
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'logs' / 'audit.log'
            with patch.dict(os.environ, {'APPLE_MAIL_AUDIT_LOG': str(log)}), \
                 patch.object(server.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout, '')):
                server.search_messages('acct-1', ['INBOX'], query='SECRET-QUERY')
            text = log.read_text()
            self.assertNotIn('SECRET', text)
            entry = json.loads(text)
            self.assertEqual((entry['op'], entry['ok'], entry['count'], entry['account'], entry['mailbox']),
                             ('search', True, 1, 'me@example.com', 'INBOX'))
            self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(log.parent.stat().st_mode), 0o700)

    def test_blocked_attempt_is_logged_with_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'audit.log'
            failure = subprocess.CompletedProcess([], 1, '', 'Error: Account not permitted by APPLE_MAIL_ACCOUNTS.')
            with patch.dict(os.environ, {'APPLE_MAIL_AUDIT_LOG': str(log)}), \
                 patch.object(server.subprocess, 'run', return_value=failure):
                with self.assertRaises(RuntimeError):
                    server.list_mailboxes('acct-blocked')
            entry = json.loads(log.read_text())
            self.assertEqual((entry['ok'], entry['error'], entry['account_id']), (False, 'blocked_by_allowlist', 'acct-blocked'))

    def test_unwritable_log_does_not_break_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {'APPLE_MAIL_AUDIT_LOG': tmp}), \
                 patch.object(server.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, OK, '')):
                self.assertEqual(server.call_mail('accounts'), {})

if __name__ == '__main__':
    unittest.main()
