# Apple Mail MCP specification

Version: 0.2.0

Status: implemented locally; v0.2.0 live Mail checks were not run in this implementation environment

Verification date: 2026-09-28

## Purpose

Let an MCP client use Apple Mail on the local Mac to discover accounts and
mailboxes, find and read email, and prepare drafts for human review.
The server uses existing Mail accounts; it does not require email passwords,
OAuth credentials, or a hosted service.

## Scope

Implemented: account discovery, recursive account mailbox discovery, bounded
subject/sender search in one mailbox with recipient/date filters, first-page inbox
search across every permitted account, message reading, RFC-header-linked same-mailbox
thread summaries, per-mailbox statistics, attachment metadata discovery, and (opt-in)
visible saved draft creation.
Cross-cutting: an account allowlist, read-only-by-default operation, and an audit log.

Excluded from this version: sending, deleting, archiving, moving, marking read,
attachment byte retrieval, body/semantic search, search across unselected mailboxes,
one cursor that pages through several accounts, local “On My Mac” mailboxes,
draft editing, and notifications. No arbitrary script execution tool is exposed.

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
Account IDs are bounded to 256 characters; mailbox path elements are non-empty and
bounded to 255 characters (1–30 elements).
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
| query | string | empty | Case-insensitive subject/sender substring; at most 1,000 characters |
| unread_only | boolean | false | Include only unread messages if true |
| limit | integer | 20 | 1–100 returned matches |
| offset | integer | 0 | At least 0 |
| scan_limit | integer | 200 | 1–1,000 messages examined |
| since | string or null | null | Inclusive ISO-8601 date or timezone-aware datetime |
| until | string or null | null | Inclusive ISO-8601 date or timezone-aware datetime |
| recipient | string or null | null | Case-insensitive substring of To/Cc/Bcc addresses |

Output: `{messages: MessageSummary[], total_in_mailbox: integer,
next_offset: integer | null, partial: boolean, unreadable_count: integer,
partial_reason: string | null, search_scope: string, applied_filters: object}`.

`MessageSummary` contains `id`, `subject`, `sender`, `date_received` (ISO date
string), and `read` (boolean). Subject and sender are capped at 1,000 characters,
with a matching `*_truncated` flag only when a value is clipped. Empty query lists messages. Search examines
messages in Mail's native mailbox order, which is not guaranteed newest-first.
It stops at the return limit, the scan limit, a 30-second time budget (always after
at least one message), or the end of mailbox. Continue using `next_offset`, even
when a page has no matches. Null means the scan reached the end. Concurrent mailbox
changes can cause duplicates or skipped messages; pagination does not provide
snapshot isolation. The existing subject/sender substring semantics are unchanged;
date and recipient filters are combined with them. Date-only bounds cover a whole UTC
calendar day (00:00:00.000 through 23:59:59.999); date-times must include a timezone and
are normalized to UTC at Mail's millisecond precision (lower bounds round up and upper
bounds round down). Malformed dates, naive date-times, and inverted ranges fail before
Mail is called. The result describes which filters were active and returns normalized
date bounds. Recipient matching examines To, Cc, and Bcc addresses, not display names or
message bodies.

Implementation note: the bridge takes all message IDs in one call, then reads
properties by ID. Positional access (`messages[i]`) costs time proportional to the
mailbox size on every access; it made a 200-message scan exceed the bridge timeout on
Gmail inboxes of a few thousand messages. A timed-out call does not cancel work already
queued in Mail, so later calls can time out until Mail catches up.

### get_thread

Required: `account_id` (1–256 characters), `mailbox_path` (1–30 non-empty elements,
each at most 255 characters), and `message_id` (integer at
least 1). Optional: `include_bodies` (false), `max_chars` (20,000; 1–100,000),
`limit` (50; 1–100), `offset` (0; nonnegative), `scan_limit` (200; 1–1,000), and
`scan_offset` (0; nonnegative).

Output: `{messages: MessageSummary[], discovered_count, next_offset, scan_complete,
scan_window_complete, mailbox_scan_finished, complete_under_rfc_headers, partial_reasons,
continuation_scan_offset, continuation_instruction, scanned, total_in_mailbox,
native_thread_membership_supported, membership_method, body_budget_exhausted}` plus
optional `scan_error`. Messages include bounded `content` and `content_truncated` only
when `include_bodies=true`. Results page through the discovered
membership set with `offset`/`next_offset`. Mailbox scanning is separately windowed:
when capped or timed out, `continuation_scan_offset` tells the caller where to resume
with the same anchor/account/mailbox. Each window links headers back to the anchor and
returns discovered summaries; the client must append window results. The server retains
no cross-call cursor state, so `complete_under_rfc_headers` is true only when a single
call covered the whole mailbox with readable headers. `mailbox_scan_finished` only indicates that the
current window reached the end; it does not certify previously collected windows.
Each window has a 20-second scan budget and the normal 45-second subprocess timeout.
Partial reasons identify a time stop, scan cap, continued window, unreadable headers,
or messages missing RFC Message-IDs.
When bodies are requested, the aggregate response body budget is 250,000 UTF-16 code
units, in addition to the per-message `max_chars`; if it is exhausted, later page
messages carry empty, truncated content and `body_budget_exhausted` is true.

**Mail's JXA dictionary has no native conversation/thread-membership property.** The
bridge uses `Message-ID`, `References`, and `In-Reply-To` headers to connect messages
within the selected mailbox. `complete_under_rfc_headers` means the bounded scan and
available headers were complete under that method; it does not assert equivalence to
Mail UI's subject grouping, identify messages filed in other mailboxes, or infer links
where headers are missing. An anchor without an RFC Message-ID fails explicitly rather than returning a
success-shaped anchor-only result. Account and mailbox permission checks run before the
anchor message is resolved.

### get_statistics

Required: `account_id` and `mailbox_path` (1–30 elements). Optional inclusive `since`
and `until` bounds follow `search_messages` ISO-8601 rules.

Output: `{account_id, mailbox_path, total_count, unread_count, date_window,
unread_count_unavailable_reason, statistics_scope}`. Total and unread counts come from
the explicitly selected Mail mailbox; if Mail cannot provide unread count it is null
with a reason. Date-window counts use `date received` and are computed only when every
message fits within the 2,000-message and 15-second scan bounds and every date can be read.
Otherwise `count`
is null and `unavailable_reason` explains why; partial estimates are not presented as
exact. Without date bounds, the date count is unavailable. This tool does not aggregate
accounts or implicitly traverse mailboxes.

### list_attachments

Required: `account_id`, `mailbox_path` (1–30 elements), and `message_id` (integer at
least 1). Optional `limit` defaults to 50 (1–100) and `offset` defaults to 0.

Output: `{attachments: [{id, name, mime_type, size_bytes, downloaded}], total,
next_offset, attachment_scope}`. Size is Mail's approximate metadata value. This release
is metadata-only: it has no filesystem destination parameter and never saves attachment
bytes, opens a file, or executes content. Retrieval is deferred because the current
architecture has no verified safe destination/path design; do not infer byte-level
safety from metadata or downloaded state. Treat names, MIME types, and attachment
contents as untrusted input. Returned IDs, names, and MIME types are bounded to 512,
1,024, and 255 characters; clipping is reported by per-field `*_truncated` flags.

### search_inboxes

Searches the inbox of every permitted account and returns the first page from each, so a
client can review all accounts in one call (for example `unread_only: true`).

| Argument | Type | Default | Constraint |
| --- | --- | --- | --- |
| query | string | empty | Case-insensitive subject/sender substring; at most 1,000 characters |
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
- The budget sits below Codex's documented 60 s default tool timeout (`tool_timeout_sec`).
  Claude Code's default is about 28 hours (`MCP_TOOL_TIMEOUT`); Claude Desktop's is
  undocumented, so a call over 60 s is unverified there. A search that hangs after the budget
  check can still run to the 45 s bridge timeout, so the hard upper bound is about 85 s. The
  worst case measured on 2026-09-26 was 39 s.
- No cross-account cursor: to go deeper in one account, call `search_messages` with that
  entry's `account_id`, `mailbox_path` and `next_offset`.
- Message order is Mail's native order. On the 11 accounts measured on 2026-09-26 it was
  newest-first on all of them; that remains a property of Mail, not a guarantee of this server.

### read_message

Required: `account_id` (1–256 characters), `mailbox_path` (1–30 non-empty elements,
each at most 255 characters), and
`message_id: integer` (at least 1).

Optional: `max_chars: integer`, default 20,000, range 1–100,000.

Output: `MessageSummary` plus `content: string`, `content_truncated: boolean`,
and `to: {name: string, address: string}[]`.

Message lookup is scoped to the supplied account and mailbox. The content
limit applies to the returned body, after Mail supplies its content. JXA uses
JavaScript string slicing, so the limit counts UTF-16 code units, not graphemes.
The server does not explicitly change read status. At most 100 To recipients are
returned, with `to_total` and `to_truncated` describing any cap; each name and address
is capped at 500 and 320 characters.

### create_draft

Registered only when `APPLE_MAIL_ALLOW_DRAFTS=1`. When `APPLE_MAIL_ACCOUNTS` is set,
`sender` is required and its address must be listed.

Required: `to: string[]` (1–100 addresses, each 3–320 characters), `subject: string` (at most 998
characters), `body: string` (at most 100,000 characters).
Optional: `sender: string` (at most 320 characters), default empty (Mail's default sender).

Output: `{id: integer, status: "draft", sent: false}`.

Creates an outgoing message, makes its compose window visible, adds recipients,
and saves it in Mail. No send command is issued. Recipients and a nonempty
sender must contain `@` and must not contain CR/LF characters. This is basic
validation, not a full email address parser. Sender should come from a listed
account; membership is not enforced by the server.

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
| MCP initializes and discovers eight read-only tools by default | Passed: protocol and unit tests |
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
| Audit log records allowed and blocked calls, never message content, filters, or attachment metadata | Passed: unit tests |
| Draft creates, saves, and preserves recipient/body values, and is not sent | Passed: one-off live test (2026-09-25) to the account's own address with an allowed sender; test draft removed (not automated) |
| Search by ID returns the same messages in the same order as positional access | Passed: one-off comparison on a 100- and a 1,438-message inbox at two offsets (not automated) |
| Worst-case search (unread-only, no-match query, 1,000-message scan) completes within the bridge timeout on every INBOX | Passed: `check_connection.py --live`. Measured 2026-09-25 on 11 accounts, largest inbox 11,596 messages: default scan 4-6 s, worst case 16-27 s |
| An exhausted time budget still advances `next_offset` | Passed: one-off live probe with a zero budget (not automated) |
| Isolated client session exposes only this server's tools | Passed: one-off headless `claude --restricted --strict-mcp-config --tools ""` tool listing (not automated) |
| `search_inboxes` searches each permitted account's inbox in its own call; a failing account does not stop the rest; the allowlist is applied to the listing and every search; an exhausted budget lists the remaining accounts in `not_reached`; the audit log has one content-free line per account | Passed: unit tests (`SearchInboxesTests`) |
| `search_inboxes` finds every account's inbox, including a differently cased `Inbox` | Passed: `check_connection.py --live` and a headline call (`unread_only`, defaults) on 2026-09-26: 11 of 11 accounts, 0 not reached, 10 s |
| `search_inboxes` worst case (unread-only, no-match query, 1,000-message scan, 50 per account) reaches every account with no error, though the shared budget can end a large account's scan before 1,000 messages (`next_offset` non-null) | Passed: `check_connection.py --live` and a live client run (isolated session) 2026-09-26/27. 11 of 11 accounts, 0 not reached, 0 errors, 39 s of the 40 s budget; only the 3 smallest inboxes (100-279 messages) finished their scan, the other 8 stopped at 124-197 of up to 1,000 messages checked |
| `search_inboxes` never reaches an account outside the allowlist | Passed: `check_connection.py --live` |
| Search date bounds validate before Mail; recipient/date filters preserve existing query semantics | Passed: unit tests |
| Thread defaults to summaries and reports RFC-header method and partial/continuation offsets | Passed: unit tests; live JXA thread behavior unverified |
| Statistics are per mailbox, bounded, and never present partial date counts as exact | Passed: unit tests; live counts unverified |
| Attachment listing is bounded metadata only; no download or filesystem path operation is exposed | Passed: unit tests; byte retrieval intentionally deferred |

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

1. Verify thread header behavior against a disposable/test mailbox on supported macOS
   versions; keep RFC-header matching distinct from Mail UI conversation grouping.
2. Consider attachment retrieval only with a separately reviewed and verified
   destination design for traversal, symlinks, collisions, overwrite prevention,
   per-file/aggregate byte caps, and untrusted content.
3. Consider local mailbox support and draft updates as separately scoped extensions.
