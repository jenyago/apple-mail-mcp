# Apple Mail MCP specification

Version: 0.1.0

Status: implemented locally; read access verified; live draft verification pending

Verification date: 2026-09-25

## Purpose

Let an MCP client use Apple Mail on the local Mac to discover accounts and
mailboxes, find and read email, and prepare drafts for human review.
The server uses existing Mail accounts; it does not require email passwords,
OAuth credentials, or a hosted service.

## Scope

Implemented: account discovery, recursive account mailbox discovery, bounded
subject/sender search, message reading, and visible saved draft creation.

Excluded from this version: sending, deleting, archiving, moving, marking read,
attachments, body search, cross-mailbox search, local “On My Mac” mailboxes,
CC/BCC, draft editing, and notifications. No arbitrary script execution tool
is exposed.

## Runtime and architecture

- macOS with Apple Mail configured and Automation permission granted to the
  launching application.
- Python 3.11+; official MCP Python SDK 1.x (`mcp>=1.20,<2`). The checked-in
  `uv.lock` resolves the installed dependencies reproducibly.
- Codex starts `server.py` through the project's virtual environment.
- Transport: MCP over stdin/stdout. Protocol logging goes to stderr.
- `server.py` validates MCP arguments and invokes `/usr/bin/osascript -l
  JavaScript -` with a fixed JXA bridge (`mail.js`).
- Each operation executes in a new subprocess with a 45-second timeout.
- The bridge addresses `com.apple.mail` using Mail's scripting interface.
- Arguments are double JSON encoded into the script sent through stdin, not
  interpolated as executable JavaScript or passed through a shell.

## Tool contracts

All account IDs come from `list_accounts`. Mailbox paths are arrays of exact
mailbox names, from outermost to innermost, returned by `list_mailboxes`.
Tools return MCP tool results containing the following logical payloads.

### list_accounts

Input: none.

Output: array of `{id: string, name: string, email_addresses: string[]}`.

### list_mailboxes

Input: `account_id: string` (required).

Output: array of `{path: string[], unread_count: integer}`.
Recursively enumerates account mailboxes. A traversal beyond 30 levels fails.

### search_messages

| Argument | Type | Default | Constraint |
| --- | --- | --- | --- |
| account_id | string | required | Existing account ID |
| mailbox_path | string[] | required | 1–30 path elements |
| query | string | empty | Case-insensitive subject/sender substring |
| unread_only | boolean | false | Include only unread messages if true |
| limit | integer | 20 | 1–100 returned matches |
| offset | integer | 0 | At least 0 |
| scan_limit | integer | 200 | 1–1,000 messages examined |

Output: `{messages: MessageSummary[], total_in_mailbox: integer,
next_offset: integer | null, search_scope: string}`.

`MessageSummary` contains `id`, `subject`, `sender`, `date_received` (ISO date
string), and `read` (boolean). Empty query lists messages. Search examines
messages in Mail's native mailbox order, which is not guaranteed newest-first.
It stops at the return limit, scan limit, or end of mailbox. Continue using
`next_offset`, even when a page has no matches. Null means the scan reached
the end. Concurrent mailbox changes can cause duplicates or skipped messages;
pagination does not provide snapshot isolation.

### read_message

Required: `account_id: string`, `mailbox_path: string[]` (1–30 elements), and
`message_id: integer` (at least 1).

Optional: `max_chars: integer`, default 20,000, range 1–100,000.

Output: `MessageSummary` plus `content: string`, `content_truncated: boolean`,
and `to: {name: string, address: string}[]`.

Message lookup is scoped to the supplied account and mailbox. The content
limit applies to the returned body, after Mail supplies its content. JXA uses
JavaScript string slicing, so the limit counts UTF-16 code units, not graphemes.
The server does not explicitly change read status.

### create_draft

Required: `to: string[]` (1–100 recipients), `subject: string`, `body: string`.
Optional: `sender: string`, default empty (Mail's default sender).

Output: `{id: integer, status: "draft", sent: false}`.

Creates an outgoing message, makes its compose window visible, adds recipients,
and saves it in Mail. No send command is issued. Recipients and a nonempty
sender must contain `@` and must not contain CR/LF characters. This is basic
validation, not a full email address parser. Sender should come from a listed
account; membership is not enforced by the server. Subject and body have no
explicit application-level length limit.

Draft creation is not idempotent. A failure can leave a partially created
draft; inspect Mail before retrying. Drafts may sync to the mail provider.

## Errors and trust boundaries

- Invalid MCP argument bounds fail before invoking Mail.
- Missing accounts, ambiguous or missing mailbox names, and missing messages
  return tool errors.
- Automation denial (`-1743`) explains where to enable Mail access in System
  Settings → Privacy & Security → Automation.
- Timeout returns an error and warns that a draft may have partially completed.
- Other scripting failures surface as Apple Mail operation errors.
- Email bodies, subjects, and sender text are untrusted data, never instructions.
- The server has no authentication layer of its own; it runs as a local stdio
  child process under the user's permissions.
- The server does not persist email content. Tool responses are delivered to the
  calling client and may be retained according to that client's settings.
- Read tools are annotated read-only; draft creation is annotated as a write,
  non-destructive, non-idempotent operation with possible external effects.

## Acceptance criteria and verification

| Criterion | Evidence/status |
| --- | --- |
| Codex has an enabled apple-mail stdio entry | Passed: `codex mcp get apple-mail` |
| MCP initializes and discovers exactly five tools | Passed: protocol smoke test |
| Invalid search limit is rejected | Passed: protocol smoke test |
| User strings remain inert JSON data | Passed: unit test with injection-shaped text |
| Automation denial and timeout produce useful errors | Passed: mocked unit tests |
| Recipient CR/LF is rejected before invoking Mail | Passed: unit test |
| Live account discovery works | Passed; rerun after user requested a check |
| Live nested mailbox listing works | Passed during initial implementation |
| Live bounded message search/listing works | Passed during initial implementation |
| Live message reading and body truncation work | Passed during initial implementation |
| Draft creates, saves, and preserves recipient/body values | Pending live test |
| Tools appear in this existing Codex task | Not loaded in current tool inventory |

Live checks omitted account details and email content from their output. No
email was sent. Existing tests do not establish full draft behavior, large
mailbox performance, pagination under concurrent changes, or behavior across
all account providers.

## Installation and operation

From the project directory:

```sh
uv sync --locked
uv run python -m unittest -v
uv run python check_connection.py --live
```

The `apple-mail` entry is already registered in `~/.codex/config.toml`
and points to this project's `.venv/bin/python` and `server.py` using absolute
paths. Moving the project requires updating that entry. Use a new Codex task
or restart the app to load the newly registered tools if necessary.

## Follow-up work

1. Live-test draft creation with an explicitly designated test recipient and
   verify saved subject, body, sender, and recipients without sending.
2. Add automated JXA behavior tests for pagination, nested paths, and draft
   partial failures.
3. Consider local mailbox support, date filters, attachments, and draft updates
   as separately scoped extensions.
