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
              'search_all_accounts', 'read_message', 'get_thread', 'get_statistics',
              'list_attachments'}
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
            bad_role = await session.call_tool('search_all_accounts', {'mailbox': 'trash'})
            assert bad_role.isError, 'An unsupported mailbox role was accepted'
            print(f'PASS: MCP initialization, {len(READ_TOOLS)} read-only tools, input validation')
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
        # A no-match query returns no message content. Nothing may error. SPEC.md documents a hard
        # upper bound of ~85s (SEARCH_ALL_BUDGET_MS's 40s budget-check granularity, plus one account's
        # subprocess allowed to run to its own timeout before the next budget check catches it).
        async with client() as session:
            live_accounts = listing(await session.call_tool('list_accounts', {}))
            start = time.monotonic()
            result = await session.call_tool('search_inboxes', {
                'query': 'zz-no-such-subject-zz', 'unread_only': True, 'limit_per_account': 50, 'scan_limit': 1000})
            elapsed = time.monotonic() - start
            assert not result.isError, f'search_inboxes failed: {result.content[0].text[:80]}'
            report = json.loads(result.content[0].text)
            errors = [a['error'][:80] for a in report['accounts'] if 'error' in a]
            assert not errors, f'search_inboxes had failing accounts: {errors}'
            # Compared against a listing taken immediately before this call, not the one from
            # minutes earlier at the top of main(): a stale comparison can flake if an account is
            # added/removed by the user mid-run, which would look like "search_inboxes lost an
            # account" even though nothing was actually lost.
            assert len(report['accounts']) + len(report['not_reached']) == len(live_accounts), 'search_inboxes lost an account'
            assert elapsed < 90, f'search_inboxes took {elapsed:.0f}s, over the ~85s documented worst case'
        print(f'PASS: search_inboxes covered {len(report["accounts"])} of {len(live_accounts)} account(s) in {elapsed:.0f}s, '
              f'{len(report["not_reached"])} not reached')

        async with client(APPLE_MAIL_ACCOUNTS=address) as session:
            result = await session.call_tool('search_inboxes', {'query': 'zz-no-such-subject-zz', 'scan_limit': 1})
            report = json.loads(result.content[0].text)
            reached = {a['account_id'] for a in report['accounts']}
            skipped = {a['account_id'] for a in report['not_reached']}
            # <=, not ==: an allowlisted account can legitimately land in not_reached under budget
            # pressure. That is not a leak; only an ID outside the allowlist would be.
            assert reached <= expected, f'search_inboxes reached accounts outside the allowlist: {reached - expected}'
            assert skipped <= expected, f'search_inboxes listed an out-of-allowlist account as not_reached: {skipped - expected}'
        print('PASS: search_inboxes stays inside the allowlist')

        # Sent and Drafts by role across every account: no folder names are guessed. A no-match
        # query returns no message content. An account with no such mailbox is reported, not
        # failed; any other per-account error fails the check.
        async with client() as session:
            live_accounts = listing(await session.call_tool('list_accounts', {}))
            for role in ('sent', 'drafts'):
                start = time.monotonic()
                result = await session.call_tool('search_all_accounts', {
                    'mailbox': role, 'query': 'zz-no-such-subject-zz', 'scan_limit': 50})
                elapsed = time.monotonic() - start
                assert not result.isError, f'search_all_accounts({role}) failed: {result.content[0].text[:80]}'
                report = json.loads(result.content[0].text)
                missing = [a for a in report['accounts'] if 'No single' in a.get('error', '')]
                errors = [a['error'][:80] for a in report['accounts'] if 'error' in a and a not in missing]
                assert not errors, f'search_all_accounts({role}) had failing accounts: {errors}'
                assert len(report['accounts']) + len(report['not_reached']) == len(live_accounts), \
                    f'search_all_accounts({role}) lost an account'
                print(f'PASS: search_all_accounts({role}) covered {len(report["accounts"])} of '
                      f'{len(live_accounts)} account(s) in {elapsed:.0f}s; {len(missing)} without a {role} mailbox')

        # search_body and since/until: a no-match body query must not error, an until far in the
        # past must return no messages, and an invalid date bound must fail before invoking Mail.
        async with client() as session:
            boxes = listing(await session.call_tool('list_mailboxes', {'account_id': first['id']}))
            inbox = next((b['path'] for b in boxes if b['path'][-1].upper() == 'INBOX'), None)
            if inbox is not None:
                body_result = await session.call_tool('search_messages', {
                    'account_id': first['id'], 'mailbox_path': inbox, 'query': 'zz-no-such-body-zz',
                    'search_body': True, 'limit': 1, 'scan_limit': 20})
                assert not body_result.isError, f'search_body failed: {body_result.content[0].text[:80]}'
                date_result = await session.call_tool('search_messages', {
                    'account_id': first['id'], 'mailbox_path': inbox,
                    'until': '1970-01-02', 'limit': 5, 'scan_limit': 50})
                assert not date_result.isError, f'date filter failed: {date_result.content[0].text[:80]}'
                assert json.loads(date_result.content[0].text)['messages'] == [], 'until=1970-01-02 unexpectedly matched a message'
                bad_date = await session.call_tool('search_messages', {
                    'account_id': first['id'], 'mailbox_path': inbox, 'since': 'not-a-date'})
                assert bad_date.isError, 'Invalid since was accepted'
        print('PASS: search_body and since/until date filters work against a live INBOX')

        entries = [json.loads(line) for line in Path(audit_log).read_text().splitlines()]
        assert any(e['ok'] for e in entries) and any(e.get('error') == 'blocked_by_allowlist' for e in entries)
        print('PASS: audit log records allowed and blocked calls')

asyncio.run(main())
