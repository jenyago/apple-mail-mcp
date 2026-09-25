"""Smoke test protocol, validation, and (with --live) read-only Mail access."""
import asyncio
from pathlib import Path
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(command=sys.executable, args=[str(Path(__file__).with_name('server.py'))])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = {t.name for t in (await session.list_tools()).tools}
            assert names == {'list_accounts', 'list_mailboxes', 'search_messages', 'read_message', 'create_draft'}, names
            bad = await session.call_tool('search_messages', {'account_id':'invalid', 'mailbox_path':['INBOX'], 'limit':0})
            assert bad.isError, 'Invalid limit was accepted'
            print('PASS: MCP initialization, five tools, input validation')
            if '--live' in sys.argv:
                result = await session.call_tool('list_accounts', {})
                if result.isError:
                    print('Live Mail access failed:', result.content)
                    raise SystemExit(1)
                print('PASS: live Apple Mail account listing (account data omitted)')

asyncio.run(main())
