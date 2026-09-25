# Apple Mail MCP

[Specification](SPEC.md) — tool contracts, limits, and verification status.

A local stdio MCP server for Apple's Mail app on macOS, using the official
[MCP Python SDK](https://py.sdk.modelcontextprotocol.io/v1/) and Mail's installed
scripting dictionary. No email credentials or remote server required.

## Tools

- `list_accounts`: permitted account IDs, names, and email addresses.
- `list_mailboxes`: nested mailbox paths for an account.
- `search_messages`: paginated, case-insensitive subject/sender search in a mailbox.
- `read_message`: message metadata and bounded plain-text content.
- `create_draft`: save a visible draft for review in Mail; never sends it.
  **Off by default** — see Configuration.

Use account IDs and mailbox path arrays returned by the listing tools. Search
scans at most 200 messages by default (up to 1,000), in Mail's native order, and
stops after about 30 seconds on a slow mailbox. Follow
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

Rebuild `.venv` with `uv sync --locked` on each Mac; do not copy it between machines.
Account IDs and addresses must be discovered again on the destination Mac.

## Configuration

Environment variables, all optional:

| Variable | Effect |
| --- | --- |
| `APPLE_MAIL_ACCOUNTS` | Comma-separated email addresses. Only accounts owning one of them are visible or reachable; everything else is refused inside the JXA bridge. **Unset = every account.** Set but empty = the server refuses to start. |
| `APPLE_MAIL_ALLOW_DRAFTS` | `1` registers `create_draft`. Any other value keeps the server read-only. With `APPLE_MAIL_ACCOUNTS` set, `sender` is required and must be a listed address, because Mail files a draft under its default account otherwise. |
| `APPLE_MAIL_AUDIT_LOG` | Audit log path. Default `~/Library/Logs/apple-mail-mcp/audit.log`. |

`uv run python server.py --list-accounts` prints every account (addresses, name, ID) to
your terminal, so you can pick addresses for `APPLE_MAIL_ACCOUNTS` without routing the
list through a model. It is a command-line option only, never an MCP tool.

The audit log is one JSON line per call, mode `0600`: time, operation, account, mailbox
path, message ID, result count, outcome. Blocked attempts are logged with
`blocked_by_allowlist`. It never contains subjects, senders, bodies, search text, or
draft content.

## Security model

Email is attacker-controlled input. A message body can carry instructions aimed at the
model reading it. This server cannot stop that; it limits what the server itself can do
(read-only by default, account allowlist, no send/delete/move tool, audit log). What
decides the outcome is **what else the session can do** after reading a hostile email:
if it also has tools that send mail, post messages, fetch URLs, or a pre-approved shell,
an injected instruction can use them without a prompt.

- Read mail in a dedicated session that has no other tools — see the Claude Code recipe.
- Do not register this server at user scope next to pre-approved outbound tools.
- macOS grants Automation permission to the launching *app*, not to this server. Any
  process started from that app can drive Mail with `osascript`, including sending. The
  "no send tool" property holds for the MCP interface only; an isolated session (no shell)
  is what makes it real.
- Mail bodies you read are sent to the model provider of your client. Use
  `APPLE_MAIL_ACCOUNTS` to keep accounts you must not share out of reach.

## Register with a client

### Claude Code (recommended: isolated session)

The flags below start a session whose only tools are this server's. `--restricted` removes
the shell, code-running tools and `WebFetch` and ignores your settings files (so broad
allow rules do not apply); `--strict-mcp-config` skips every other MCP server, including
connectors; `--tools ""` removes the remaining built-in tools. Check `/mcp` on launch.

```sh
cat > "$HOME/.claude/mcp-apple-mail.json" <<EOF
{"mcpServers":{"apple-mail":{"type":"stdio","command":"$PWD/.venv/bin/python","args":["$PWD/server.py"],
 "env":{"APPLE_MAIL_ACCOUNTS":"you@example.com"}}}}
EOF
alias claude-mail='claude --tools "" --restricted --strict-mcp-config --mcp-config "$HOME/.claude/mcp-apple-mail.json"'
```

Run it from the cloned directory, and put the alias in your shell profile. Add
`"APPLE_MAIL_ALLOW_DRAFTS":"1"` to `env` only when you want drafts.

Registering with `claude mcp add --scope user` loads the server into every session,
which is only safe if none of them pre-approves outbound tools.

### Codex

From the cloned directory:

```sh
codex mcp add apple-mail -- "$PWD/.venv/bin/python" "$PWD/server.py"
```

Set `APPLE_MAIL_ACCOUNTS` in the server's environment as Codex's MCP configuration
allows (not verified here).

### Claude Desktop

Merge this entry into `~/Library/Application Support/Claude/claude_desktop_config.json`,
preserving other settings. Replace both paths with absolute paths to your clone:

```json
{
  "mcpServers": {
    "apple-mail": {
      "command": "/absolute/path/apple-mail-mcp/.venv/bin/python",
      "args": ["/absolute/path/apple-mail-mcp/server.py"],
      "env": {"APPLE_MAIL_ACCOUNTS": "you@example.com"}
    }
  }
}
```

Start a new session or restart the client if the tools do not appear.
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

The live check lists accounts without printing their contents, then proves against real
Mail that an allowlist filters `list_accounts`, blocks other accounts, and is logged.
It does not create drafts or send email. Unit tests cover script injection resistance,
permission errors, timeouts, recipient validation, allowlist parsing and fail-closed
behavior, draft gating, and that the audit log never contains message content. Protocol
tests check initialization, tool discovery, and parameter bounds.

## Try it

1. “Use apple-mail to list my Mail accounts.”
2. “List the mailboxes for [account].”
3. “List five messages from its Inbox using apple-mail.”
4. “Read the first message from that result.”
5. Optional, with drafts enabled: “Create a draft to [your own email] from [allowed
   sender] with subject MCP test and body This is a draft test. Do not send it.”

Verify the optional draft in Mail. No send tool is exposed. Message order is
Mail's native order, not guaranteed newest-first.

## License

[MIT](LICENSE)
