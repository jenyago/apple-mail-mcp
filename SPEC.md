# Apple Mail MCP specification

Version: 0.1.0

Status: implemented locally; read access, account allowlist, cross-account inbox search and (one-off, not automated) draft creation verified live

Verification date: 2026-09-26

## Purpose

Let an MCP client use Apple Mail on the local Mac to discover accounts and
mailboxes, find and read email, and prepare drafts for human review.
The server uses existing Mail accounts; it does not require email passwords,
OAuth credentials, or a hosted service.

## Scope

Implemented: account discovery, recursive account mailbox discovery, bounded
subject/sender search in one mailbox, first-page inbox search across every permitted
account, message reading, and (opt-in) visible saved draft creation.
Cross-cutting: an account allowlist, read-only-by-default operation, and an audit log.

Excluded from this version: sending, deleting, archiving, moving, marking read,
attachments, body search, search across mailboxes other than each account's inbox,
one cursor that pages through several accounts, local “On My Mac” mailboxes,
CC/BCC, draft editing, and notifications. No arbitrary script execution tool
is exposed.

## Runtime and architecture

- macOS with Apple Mail configured and Automation permission granted to the
  launching application.
- Python 3.11+; official MCP Python SDK 1.x (`mcp>=1.20,<2`). The checked-in
  `uv.lock` resolves the installed dependencies reproducibly.
- An MCP client starts `server.py` through the project's virtual environment.
- Transport: MCP over stdin/stdout. Protocol logging goes to stderr.
- `server.py` validates MCP arguments and invokes `/usr/bin/osascript -l
  JavaScript -` with a fixed JXA bridge (`mail.js`).
- Each operation executes in a new subprocess with a 45-second timeout.
- The bridge addresses `com.apple.mail` using Mail's scripting interface.
- Arguments are double JSON encoded into the script sent through stdin, not
  interpolated as executable JavaScript or passed through a shell.

## Configuration and access control

| Variable | Behavior |
| --- | --- |
| `APPLE_MAIL_ACCOUNTS` | Comma-separated addresses, matched case-insensitively against each account's addresses. Unset: every account. Set: only matching accounts are listed or reachable. Set but empty, or containing an entry without `@`: startup and every call fail closed. |
| `APPLE_MAIL_ALLOW_DRAFTS` | Exactly `1` registers `create_draft`; any other value leaves the server read-only. |
| `APPLE_MAIL_AUDIT_LOG` | Audit log path; default `~/Library/Logs/apple-mail-mcp/audit.log`. |

The allowlist is passed to the JXA bridge as JSON data by `call_mail`, after and
overriding any caller-supplied value, and enforced in one place (`mail.js`): account
lookup refuses non-permitted accounts and `accounts` filters them out. `create_draft`
additionally requires a `sender` that is a listed address, because Mail files a draft
under its default account when sender is empty. `python server.py --list-accounts`
prints every account for setup and is not an MCP tool.

Each bridge call appends one JSON line (directory `0700`, file `0600`), so `search_inboxes`
writes one line for the account listing and one per account searched (its mailbox is
recorded as `inbox`): timestamp, operation,
account ID, permitted account address, mailbox path, message ID, result count, and
outcome (`ok`, or `timeout`, `automation_denied`, `blocked_by_allowlist`, `failed`).
Subjects, senders, bodies, search text, and draft content are never logged. A log write
failure is reported on stderr and does not fail the tool call.

## Tool contracts

All account IDs come from `list_accounts`. Mailbox paths are arrays of exact
mailbox names, from outermost to innermost, returned by `list_mailboxes`.
Tools return MCP tool results containing the following logical payloads.

### list_accounts

Input: none.

Output: array of `{id: string, name: string, email_addresses: string[]}`, limited to
permitted accounts.

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
It stops at the return limit, the scan limit, a 30-second time budget (always after
at least one message), or the end of mailbox. Continue using `next_offset`, even
when a page has no matches. Null means the scan reached the end. Concurrent mailbox
changes can cause duplicates or skipped messages; pagination does not provide
snapshot isolation.

Implementation note: the bridge takes all message IDs in one call, then reads
properties by ID. Positional access (`messages[i]`) costs time proportional to the
mailbox size on every access; it made a 200-message scan exceed the bridge timeout on
Gmail inboxes of a few thousand messages. A timed-out call does not cancel work already
queued in Mail, so later calls can time out until Mail catches up.

### search_inboxes

Searches the inbox of every permitted account and returns the first page from each, so a
client can review all accounts in one call (for example `unread_only: true`).

| Argument | Type | Default | Constraint |
| --- | --- | --- | --- |
| query | string | empty | Case-insensitive subject/sender substring |
| unread_only | boolean | false | Include only unread messages if true |
| limit_per_account | integer | 5 | 1–50 returned matches per account |
| scan_limit | integer | 100 | 1–1,000 messages examined per account |

Output: `{accounts: AccountPage[], not_reached: {account_id, email}[], search_scope: string}`.
`AccountPage` is `{account_id, email}` plus either the single-account search fields
(`messages`, `total_in_mailbox`, `next_offset`) and `mailbox_path`, or `error: string`.

- The inbox is the account's top-level mailbox named `inbox`, matched case-insensitively
  (`INBOX` on most providers, `Inbox` on some). Zero or several matches is that account's `error`.
- The accounts come from the same allowlist-filtered listing as `list_accounts`.
- Each account is searched in its own bridge call, in listing order, from offset 0. A failing
  account (timeout, missing inbox) is reported in its own entry and does not stop the others.
- Overall budget 40 s (`SEARCH_ALL_BUDGET_MS`). Each search gets an equal share of the time
  left, capped at the single-account budget of 30 s; its scan stops there and reports
  `next_offset`. An account not started before the budget is spent is listed in `not_reached`.
- No cross-account cursor: to go deeper in one account, call `search_messages` with that
  entry's `account_id`, `mailbox_path` and `next_offset`.
- Message order is Mail's native order. On the 11 accounts measured on 2026-09-26 it was
  newest-first on all of them; that remains a property of Mail, not a guarantee of this server.

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

Registered only when `APPLE_MAIL_ALLOW_DRAFTS=1`. When `APPLE_MAIL_ACCOUNTS` is set,
`sender` is required and its address must be listed.

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
- Missing accounts, accounts outside the allowlist, ambiguous or missing mailbox
  names, and missing messages return tool errors.
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
- The server cannot prevent prompt injection through email content. It bounds its own
  reach (allowlist, read-only default, audit log); the surrounding session's other
  tools and permissions decide what an injected instruction can do. macOS Automation
  consent belongs to the launching app, so processes started from that app can script
  Mail directly; run the server in an isolated client session (see README).

## Acceptance criteria and verification

| Criterion | Evidence/status |
| --- | --- |
| A Claude Code client lists the server as connected | Passed: `claude mcp get apple-mail` (2026-09-25) |
| MCP initializes and discovers exactly five read-only tools by default | Passed: protocol smoke test |
| `create_draft` is registered only with `APPLE_MAIL_ALLOW_DRAFTS=1` | Passed: unit test and protocol smoke test |
| Invalid search limit is rejected | Passed: protocol smoke test |
| User strings remain inert JSON data | Passed: unit test with injection-shaped text |
| Automation denial and timeout produce useful errors | Passed: mocked unit tests |
| Recipient CR/LF is rejected before invoking Mail | Passed: unit test |
| Live account discovery works | Passed; rerun after user requested a check |
| Live nested mailbox listing works | Passed during initial implementation |
| Live bounded message search/listing works | Passed during initial implementation |
| Live message reading and body truncation work | Passed during initial implementation |
| Allowlist filters `list_accounts` and blocks other accounts in real Mail | Passed: `check_connection.py --live` |
| An allowlist matching no account hides every account | Passed: `check_connection.py --live` |
| Allowlist parsing fails closed; callers cannot override it | Passed: unit tests |
| Draft sender outside the allowlist, or empty, is refused before Mail creates anything | Passed: one-off live probe with a non-matching allowlist (not automated) |
| Audit log records allowed and blocked calls, never message content | Passed: unit tests; live log check |
| Draft creates, saves, and preserves recipient/body values, and is not sent | Passed: one-off live test (2026-09-25) to the account's own address with an allowed sender; test draft removed (not automated) |
| Search by ID returns the same messages in the same order as positional access | Passed: one-off comparison on a 100- and a 1,438-message inbox at two offsets (not automated) |
| Worst-case search (unread-only, no-match query, 1,000-message scan) completes within the bridge timeout on every INBOX | Passed: `check_connection.py --live`. Measured 2026-09-25 on 11 accounts, largest inbox 11,596 messages: default scan 4-6 s, worst case 16-27 s |
| An exhausted time budget still advances `next_offset` | Passed: one-off live probe with a zero budget (not automated) |
| Isolated client session exposes only this server's tools | Passed: one-off headless `claude --restricted --strict-mcp-config --tools ""` tool listing (not automated) |
| `search_inboxes` searches each permitted account's inbox in its own call; a failing account does not stop the rest; the allowlist is applied to the listing and every search; an exhausted budget lists the remaining accounts in `not_reached`; the audit log has one content-free line per account | Passed: unit tests (`SearchInboxesTests`) |
| `search_inboxes` finds every account's inbox, including a differently cased `Inbox` | Passed: `check_connection.py --live` and a headline call (`unread_only`, defaults) on 2026-09-26: 11 of 11 accounts, 0 not reached, 10 s |
| `search_inboxes` worst case (unread-only, no-match query, 1,000-message scan, 50 per account) completes with no failing account | Passed: `check_connection.py --live`. 11 of 11 accounts, 0 not reached, 39 s of the 40 s budget |
| `search_inboxes` never reaches an account outside the allowlist | Passed: `check_connection.py --live` |

Live checks omitted account details and email content from their output. No
email was sent. Existing tests do not establish pagination under concurrent
changes, behavior across all account providers, or mailboxes larger than the
11,596-message inbox measured above.

## Installation and operation

From the project directory:

```sh
uv sync --locked
uv run python -m unittest -v
uv run python check_connection.py --live
```

Client registration points at this project's `.venv/bin/python` and `server.py` using
absolute paths; moving the project requires updating it. See the README for the
recommended isolated Claude Code session and per-client entries. Start a new client
session to load newly registered tools.

## Follow-up work

1. Automate the one-off live draft test (a designated test recipient, verify the
   saved draft, remove it) if a safe cleanup path can be found; Mail's move to Trash
   leaves a copy that needs purging.
2. Add automated JXA behavior tests for pagination, nested paths, and draft
   partial failures.
3. Consider local mailbox support, date filters, attachments, and draft updates
   as separately scoped extensions.
