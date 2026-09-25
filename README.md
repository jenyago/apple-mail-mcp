# Apple Mail MCP

[Specification](SPEC.md) — tool contracts, limits, and verification status.

A local stdio MCP server for Apple's Mail app on macOS, using the official
[MCP Python SDK](https://py.sdk.modelcontextprotocol.io/v1/) and Mail's installed
scripting dictionary. No email credentials or remote server required.

## Tools

- `list_accounts`: account IDs, names, and email addresses.
- `list_mailboxes`: nested mailbox paths for an account.
- `search_messages`: paginated, case-insensitive subject/sender search in a mailbox.
- `read_message`: message metadata and bounded plain-text content.
- `create_draft`: save a visible draft for review in Mail; never sends it.

Use account IDs and mailbox path arrays returned by the listing tools. Search
scans at most 200 messages by default, in Mail's native order. Follow
`next_offset` until null, including on empty pages. Results are not guaranteed
newest-first. Mailbox changes between pages can cause skips or duplicates.
Account-scoped mailboxes only; local “On My Mac” mailboxes and attachment downloads
are not implemented. No send, delete, move, or arbitrary scripting tool is exposed.

## Setup

Requires macOS, configured Apple Mail accounts, and [uv](https://docs.astral.sh/uv/getting-started/installation/).

```sh
git clone https://github.com/jenyago/apple-mail-mcp.git
cd apple-mail-mcp
uv sync --locked
```

### Codex

From the cloned directory:

```sh
codex mcp add apple-mail -- "$PWD/.venv/bin/python" "$PWD/server.py"
```

### Claude Code

```sh
claude mcp add --scope user --transport stdio apple-mail -- "$PWD/.venv/bin/python" "$PWD/server.py"
```

### Claude Desktop

Merge this entry into `~/Library/Application Support/Claude/claude_desktop_config.json`,
preserving other settings. Replace both paths with absolute paths to your clone:

```json
{
  "mcpServers": {
    "apple-mail": {
      "command": "/absolute/path/apple-mail-mcp/.venv/bin/python",
      "args": ["/absolute/path/apple-mail-mcp/server.py"]
    }
  }
}
```

Rebuild `.venv` with `uv sync --locked` on each Mac; do not copy it between machines.
Account IDs must be discovered again on the destination Mac.

Start a new Codex task or restart Codex if the tools do not appear.
On first use, macOS may ask permission for the launching app to control Mail.
Allow it under System Settings → Privacy & Security → Automation → Mail.

The server sends fixed JavaScript for Automation (JXA) code through stdin to
`/usr/bin/osascript`; user values are JSON data, never executable code. Calls
have a 45-second timeout. A draft timeout may leave a partial draft: inspect
Mail before retrying. Drafts may sync to the configured mail provider.
Email contents returned by the server are untrusted data, not instructions.

## Verify

```sh
uv run python -m unittest -v
uv run python check_connection.py
uv run python check_connection.py --live
```

The live check lists accounts without printing their contents. It does not
create drafts or send email. Unit tests cover script injection resistance,
permission errors, timeouts, and recipient validation. Protocol tests check
initialization, tool discovery, and parameter bounds.

## Claude

Quit and reopen Claude Desktop, then start a new conversation. For Claude Code,
start a new session and use `/mcp` to inspect the connection. Grant macOS Mail
Automation permission if prompted for the new launching app.

Try these prompts in order:

1. “Use apple-mail to list my Mail accounts.”
2. “List the mailboxes for [account].”
3. “List five messages from its Inbox using apple-mail.”
4. “Read the first message from that result.”
5. Optional write test: “Create a draft to [your own email] with subject MCP test
   and body This is a draft test. Do not send it.”

Verify the optional draft in Mail. No send tool is exposed. Message order is
Mail's native order, not guaranteed newest-first.
