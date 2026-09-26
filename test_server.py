import asyncio
import importlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import call, patch
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

class SearchTests(unittest.TestCase):
    def test_scan_time_budget_is_below_the_bridge_timeout(self):
        with patch.object(server, 'audit'), \
             patch.object(server.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, OK, '')) as run:
            server.search_messages('acct-1', ['INBOX'])
            self.assertEqual(payload_of(run)['time_budget_ms'], server.SEARCH_BUDGET_MS)
            self.assertLess(server.SEARCH_BUDGET_MS, run.call_args.kwargs['timeout'] * 1000)

def done(result, meta=None):
    return subprocess.CompletedProcess([], 0, json.dumps({'result': result, 'meta': meta or {}}), '')

def accounts_result(*emails):
    return [{'id': f'acct-{i}', 'name': f'Account {i}', 'email_addresses': [e]} for i, e in enumerate(emails, 1)]

def inbox_result(count=1, next_offset=None):
    return {'messages': [{'id': n, 'subject': 'SECRET-SUBJECT', 'sender': 'SECRET-SENDER',
                          'date_received': '2026-09-26T00:00:00.000Z', 'read': False} for n in range(1, count + 1)],
            'total_in_mailbox': 500, 'next_offset': next_offset, 'mailbox_path': ['INBOX'],
            'search_scope': 'Subject and sender, in Mail mailbox order; continue with next_offset.'}

def payload_from(call):
    literal = call.kwargs['input'].split('JSON.parse(', 1)[1].split(');\n', 1)[0]
    return json.loads(json.loads(literal))

class SearchInboxesTests(unittest.TestCase):
    def test_each_account_is_searched_in_its_own_bridge_call(self):
        outputs = [done(accounts_result('a@x.com', 'b@y.org')), done(inbox_result(2), {'account': 'a@x.com'}),
                   done(inbox_result(1), {'account': 'b@y.org'})]
        with patch.object(server, 'audit'), patch.object(server.subprocess, 'run', side_effect=outputs) as run:
            result = server.search_inboxes(query='invoice', unread_only=True, limit_per_account=7, scan_limit=50)
        payloads = [payload_from(c) for c in run.call_args_list]
        self.assertEqual([p['op'] for p in payloads], ['accounts', 'search', 'search'])
        self.assertEqual([p.get('account_id') for p in payloads[1:]], ['acct-1', 'acct-2'])
        for p in payloads[1:]:
            self.assertEqual((p['inbox'], p['query'], p['unread_only'], p['limit'], p['scan_limit'], p['offset']),
                             (True, 'invoice', True, 7, 50, 0))
        self.assertEqual([(a['account_id'], a['email'], len(a['messages'])) for a in result['accounts']],
                         [('acct-1', 'a@x.com', 2), ('acct-2', 'b@y.org', 1)])
        self.assertEqual(result['accounts'][0]['mailbox_path'], ['INBOX'])
        self.assertEqual(result['not_reached'], [])

    def test_one_failing_account_does_not_abort_the_others(self):
        outputs = [done(accounts_result('a@x.com', 'b@y.org', 'c@z.net')), done(inbox_result(1)),
                   subprocess.CompletedProcess([], 1, '', 'Error: Mailbox path missing or ambiguous'),
                   done(inbox_result(3))]
        with patch.object(server, 'audit'), patch.object(server.subprocess, 'run', side_effect=outputs):
            result = server.search_inboxes()
        self.assertEqual(['messages' in a for a in result['accounts']], [True, False, True])
        self.assertIn('Mailbox path missing', result['accounts'][1]['error'])
        self.assertEqual(result['accounts'][1]['account_id'], 'acct-2')

    def test_allowlist_applies_to_the_listing_and_every_search(self):
        outputs = [done(accounts_result('a@x.com')), done(inbox_result())]
        with patch.dict(os.environ, {'APPLE_MAIL_ACCOUNTS': 'a@x.com'}), patch.object(server, 'audit'), \
             patch.object(server.subprocess, 'run', side_effect=outputs) as run:
            server.search_inboxes()
        self.assertEqual([payload_from(c)['allowed_emails'] for c in run.call_args_list], [['a@x.com']] * 2)

    def test_exhausted_budget_reports_accounts_not_reached_instead_of_running_them(self):
        clock = {'now': 0.0}
        outputs = iter([done(accounts_result('a@x.com', 'b@y.org', 'c@z.net')), done(inbox_result(1))])
        def slow_run(*args, **kwargs):
            result = next(outputs)
            if payload_from(call(**kwargs))['op'] == 'search':
                clock['now'] += server.SEARCH_ALL_BUDGET_MS / 1000 + 1  # the first search uses the whole budget
            return result
        with patch.object(server, 'audit'), patch.object(server, 'monotonic', lambda: clock['now']), \
             patch.object(server.subprocess, 'run', side_effect=slow_run) as run:
            result = server.search_inboxes()
        self.assertEqual(run.call_count, 2)  # accounts + the first search only
        self.assertEqual([a['account_id'] for a in result['accounts']], ['acct-1'])
        self.assertEqual([a['account_id'] for a in result['not_reached']], ['acct-2', 'acct-3'])

    def test_each_search_gets_a_fair_share_of_the_time_left_capped_below_the_bridge_timeout(self):
        outputs = [done(accounts_result('a@x.com', 'b@y.org')), done(inbox_result()), done(inbox_result())]
        with patch.object(server, 'audit'), patch.object(server, 'monotonic', lambda: 0.0), \
             patch.object(server.subprocess, 'run', side_effect=outputs) as run:
            server.search_inboxes()
        budgets = [payload_from(c)['time_budget_ms'] for c in run.call_args_list[1:]]
        # No time has passed: the first of two accounts gets half, the last gets everything left but never
        # more than a single-account search would (which is itself below the 45 s bridge timeout).
        self.assertEqual(budgets, [server.SEARCH_ALL_BUDGET_MS // 2, min(server.SEARCH_ALL_BUDGET_MS, server.SEARCH_BUDGET_MS)])

    def test_audit_has_one_line_per_account_and_never_content(self):
        outputs = [done(accounts_result('a@x.com', 'b@y.org')), done(inbox_result(2), {'account': 'a@x.com'}),
                   done(inbox_result(1), {'account': 'b@y.org'})]
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'audit.log'
            with patch.dict(os.environ, {'APPLE_MAIL_AUDIT_LOG': str(log)}), \
                 patch.object(server.subprocess, 'run', side_effect=outputs):
                server.search_inboxes(query='SECRET-QUERY')
            text = log.read_text()
        self.assertNotIn('SECRET', text)
        entries = [json.loads(line) for line in text.splitlines()]
        self.assertEqual([(e['op'], e['account'], e.get('mailbox'), e['count']) for e in entries[1:]],
                         [('search', 'a@x.com', 'inbox', 2), ('search', 'b@y.org', 'inbox', 1)])

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
        self.assertEqual(self.tool_names(None), {'list_accounts', 'list_mailboxes', 'search_messages', 'search_inboxes', 'read_message'})

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
