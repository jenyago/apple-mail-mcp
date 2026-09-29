"""Smoke test protocol and validation; with --live, read-only Mail access and allowlist enforcement."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = Path(__file__).with_name('server.py')
READ_TOOLS = {'list_accounts', 'list_mailboxes', 'search_messages', 'search_inboxes',
              'read_message', 'get_thread', 'get_statistics', 'list_attachments'}
audit_log = ''

@asynccontextmanager
async def client(**env):
    # Hermetic: the server sees only these variables, and logs to a temp audit file.
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)],
                                   env={'APPLE_MAIL_AUDIT_LOG': audit_log, **env})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session

def listing(result):
    if result.isError:
        raise SystemExit(f'Tool call failed: {result.content}')
    structured = result.structuredContent
    return structured['result'] if structured and 'result' in structured else [json.loads(c.text) for c in result.content]

async def main():
    global audit_log
    with tempfile.TemporaryDirectory() as tmp:
        audit_log = os.path.join(tmp, 'audit.log')
        async with client() as session:
            listed = (await session.list_tools()).tools
            names = {tool.name for tool in listed}
            assert names == READ_TOOLS, names
            assert all(tool.annotations and tool.annotations.readOnlyHint for tool in listed), \
                'A default read tool is not annotated read-only'
            assert all(tool.annotations and not tool.annotations.destructiveHint
                       and not tool.annotations.openWorldHint for tool in listed), \
                'A read tool has an unsafe or inaccurate annotation'
            bad = await session.call_tool('search_messages', {'account_id': 'invalid', 'mailbox_path': ['INBOX'], 'limit': 0})
            assert bad.isError, 'Invalid limit was accepted'
            bad_date = await session.call_tool('search_messages', {
                'account_id': 'invalid', 'mailbox_path': ['INBOX'], 'since': 'not-a-date'})
            assert bad_date.isError, 'Malformed date bound was accepted'
            inverted_dates = await session.call_tool('search_messages', {
                'account_id': 'invalid', 'mailbox_path': ['INBOX'],
                'since': '2026-09-30', 'until': '2026-09-01'})
            assert inverted_dates.isError, 'Inverted date range was accepted'
            bad_thread_scan = await session.call_tool('get_thread', {
                'account_id': 'invalid', 'mailbox_path': ['INBOX'],
                'message_id': 1, 'scan_limit': 1001})
            assert bad_thread_scan.isError, 'Out-of-range thread scan limit was accepted'
            bad_attachment_limit = await session.call_tool('list_attachments', {
                'account_id': 'invalid', 'mailbox_path': ['INBOX'], 'message_id': 1, 'limit': 101})
            assert bad_attachment_limit.isError, 'Unbounded attachment metadata page was accepted'
            bad_mailbox_name = await session.call_tool('list_mailboxes', {
                'account_id': 'x' * 257})
            assert bad_mailbox_name.isError, 'Unbounded account ID was accepted'
            bad_path_segment = await session.call_tool('search_messages', {
                'account_id': 'invalid', 'mailbox_path': ['x' * 256]})
            assert bad_path_segment.isError, 'Unbounded mailbox path element was accepted'
            print('PASS: MCP initialization, eight read-only tools, input validation')
        async with client(APPLE_MAIL_ALLOW_DRAFTS='1') as session:
            listed = (await session.list_tools()).tools
            names = {tool.name for tool in listed}
            assert names == READ_TOOLS | {'create_draft'}, names
            draft = next(tool for tool in listed if tool.name == 'create_draft')
            assert draft.annotations and draft.annotations.readOnlyHint is False
            assert draft.annotations.destructiveHint is False
            assert draft.annotations.idempotentHint is False
            assert draft.annotations.openWorldHint is True
            too_long_draft = await session.call_tool('create_draft', {
                'to': ['person@example.com'], 'subject': 'test', 'body': 'x' * 100001})
            assert too_long_draft.isError, 'Unbounded draft body was accepted'
            print('PASS: create_draft is registered only with APPLE_MAIL_ALLOW_DRAFTS=1')
        if '--live' not in sys.argv:
            return

        async with client() as session:
            found = listing(await session.call_tool('list_accounts', {}))
        print('PASS: live Apple Mail account listing (account data omitted)')
        first = next((a for a in found if a['email_addresses']), None)
        if first is None:
            print('SKIP: no account with an address to test the allowlist against')
            return

        address = first['email_addresses'][0]
        expected = {a['id'] for a in found if address.lower() in [e.lower() for e in a['email_addresses']]}
        async with client(APPLE_MAIL_ACCOUNTS=address) as session:
            visible = {a['id'] for a in listing(await session.call_tool('list_accounts', {}))}
            assert visible == expected, 'Allowlist did not filter list_accounts'
            others = [a for a in found if a['id'] not in expected]
            if others:
                blocked = await session.call_tool('list_mailboxes', {'account_id': others[0]['id']})
                assert blocked.isError and 'not permitted' in blocked.content[0].text, 'Blocked account was reachable'
        print('PASS: allowlist filters list_accounts and blocks other accounts' if others else
              'PASS: allowlist filters list_accounts (no other account to block)')

        async with client(APPLE_MAIL_ACCOUNTS='nobody@example.invalid') as session:
            assert listing(await session.call_tool('list_accounts', {})) == [], 'Unknown allowlist exposed accounts'
            blocked = await session.call_tool('list_mailboxes', {'account_id': first['id']})
            assert blocked.isError, 'Account reachable outside the allowlist'
        print('PASS: an allowlist matching no account hides every account')

        # Regression: by-index message access made a 200-message scan exceed the 45 s bridge
        # timeout on large Gmail inboxes. Worst case: unread-only, no-match, 1000-message scan.
        # A no-match query returns no message content.
        async with client() as session:
            slowest, checked = 0.0, 0
            for a in found:
                boxes = listing(await session.call_tool('list_mailboxes', {'account_id': a['id']}))
                inbox = next((b['path'] for b in boxes if b['path'][-1].upper() == 'INBOX'), None)
                if inbox is None:
                    continue
                start = time.monotonic()
                result = await session.call_tool('search_messages', {
                    'account_id': a['id'], 'mailbox_path': inbox, 'query': 'zz-no-such-subject-zz',
                    'unread_only': True, 'limit': 100, 'scan_limit': 1000})
                assert not result.isError, f'Search failed on an INBOX: {result.content[0].text[:80]}'
                slowest, checked = max(slowest, time.monotonic() - start), checked + 1
        print(f'PASS: worst-case INBOX search completed on {checked} account(s); slowest {slowest:.0f}s of the 45s timeout')

        # Cross-account search, worst case: unread-only, no-match, deepest scan, every permitted inbox.
        # A no-match query returns no message content. Nothing may error, and the whole call stays under a minute.
        async with client() as session:
            start = time.monotonic()
            result = await session.call_tool('search_inboxes', {
                'query': 'zz-no-such-subject-zz', 'unread_only': True, 'limit_per_account': 50, 'scan_limit': 1000})
            elapsed = time.monotonic() - start
            assert not result.isError, f'search_inboxes failed: {result.content[0].text[:80]}'
            report = json.loads(result.content[0].text)
            errors = [a['error'][:80] for a in report['accounts'] if 'error' in a]
            assert not errors, f'search_inboxes had failing accounts: {errors}'
            assert len(report['accounts']) + len(report['not_reached']) == len(found), 'search_inboxes lost an account'
            assert elapsed < 60, f'search_inboxes took {elapsed:.0f}s'
        print(f'PASS: search_inboxes covered {len(report["accounts"])} of {len(found)} account(s) in {elapsed:.0f}s, '
              f'{len(report["not_reached"])} not reached')

        async with client(APPLE_MAIL_ACCOUNTS=address) as session:
            result = await session.call_tool('search_inboxes', {'query': 'zz-no-such-subject-zz', 'scan_limit': 1})
            scoped = {a['account_id'] for a in json.loads(result.content[0].text)['accounts']}
            assert scoped == expected, 'search_inboxes reached accounts outside the allowlist'
        print('PASS: search_inboxes stays inside the allowlist')

        entries = [json.loads(line) for line in Path(audit_log).read_text().splitlines()]
        assert any(e['ok'] for e in entries) and any(e.get('error') == 'blocked_by_allowlist' for e in entries)
        print('PASS: audit log records allowed and blocked calls')

asyncio.run(main())
