"""Smoke test protocol and validation; with --live, read-only Mail access and allowlist enforcement."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = Path(__file__).with_name('server.py')
READ_TOOLS = {'list_accounts', 'list_mailboxes', 'search_messages', 'read_message'}
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

async def tool_names(session):
    return {t.name for t in (await session.list_tools()).tools}

def accounts(result):
    if result.isError:
        raise SystemExit(f'list_accounts failed: {result.content}')
    structured = result.structuredContent
    return structured['result'] if structured and 'result' in structured else [json.loads(c.text) for c in result.content]

async def main():
    global audit_log
    with tempfile.TemporaryDirectory() as tmp:
        audit_log = os.path.join(tmp, 'audit.log')
        async with client() as session:
            names = await tool_names(session)
            assert names == READ_TOOLS, names
            bad = await session.call_tool('search_messages', {'account_id': 'invalid', 'mailbox_path': ['INBOX'], 'limit': 0})
            assert bad.isError, 'Invalid limit was accepted'
            print('PASS: MCP initialization, four read-only tools, input validation')
        async with client(APPLE_MAIL_ALLOW_DRAFTS='1') as session:
            names = await tool_names(session)
            assert names == READ_TOOLS | {'create_draft'}, names
            print('PASS: create_draft is registered only with APPLE_MAIL_ALLOW_DRAFTS=1')
        if '--live' not in sys.argv:
            return

        async with client() as session:
            found = accounts(await session.call_tool('list_accounts', {}))
        print('PASS: live Apple Mail account listing (account data omitted)')
        first = next((a for a in found if a['email_addresses']), None)
        if first is None:
            print('SKIP: no account with an address to test the allowlist against')
            return

        address = first['email_addresses'][0]
        expected = {a['id'] for a in found if address.lower() in [e.lower() for e in a['email_addresses']]}
        async with client(APPLE_MAIL_ACCOUNTS=address) as session:
            visible = {a['id'] for a in accounts(await session.call_tool('list_accounts', {}))}
            assert visible == expected, 'Allowlist did not filter list_accounts'
            others = [a for a in found if a['id'] not in expected]
            if others:
                blocked = await session.call_tool('list_mailboxes', {'account_id': others[0]['id']})
                assert blocked.isError and 'not permitted' in blocked.content[0].text, 'Blocked account was reachable'
        print('PASS: allowlist filters list_accounts and blocks other accounts' if others else
              'PASS: allowlist filters list_accounts (no other account to block)')

        async with client(APPLE_MAIL_ACCOUNTS='nobody@example.invalid') as session:
            assert accounts(await session.call_tool('list_accounts', {})) == [], 'Unknown allowlist exposed accounts'
            blocked = await session.call_tool('list_mailboxes', {'account_id': first['id']})
            assert blocked.isError, 'Account reachable outside the allowlist'
        print('PASS: an allowlist matching no account hides every account')

        entries = [json.loads(line) for line in Path(audit_log).read_text().splitlines()]
        assert any(e['ok'] for e in entries) and any(e.get('error') == 'blocked_by_allowlist' for e in entries)
        print('PASS: audit log records allowed and blocked calls')

asyncio.run(main())
