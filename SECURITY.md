# Security policy

## Supported versions

Security fixes target the latest release. Upgrade to the latest release before
reporting a vulnerability when possible.

## Reporting a vulnerability

Do not report vulnerabilities in a public issue. Use GitHub's private
**Report a vulnerability** flow for this repository when it is enabled; otherwise
contact the repository maintainers privately through GitHub.

Include the affected version, macOS version, a concise impact description, and
reproduction steps that do not contain real message content, account addresses,
credentials, or attachment data. Maintainers will acknowledge reports and coordinate
disclosure and remediation with the reporter.

## Security boundaries to preserve

- Keep `APPLE_MAIL_ACCOUNTS` fail-closed when set but empty, and enforce it inside the
  fixed JXA bridge before account, mailbox, message, thread, statistics, or attachment
  access.
- Keep user-provided values JSON data passed to fixed JXA code; never add arbitrary
  script execution, shell interpolation, or credential collection.
- Keep the default interface read-only. `create_draft` remains opt-in, allowlist-aware,
  visible in Mail, and incapable of sending.
- Keep audit entries content-free: no message subjects, senders, bodies, search filters,
  attachment names/bytes, or caller-provided paths.
- Treat message and attachment metadata/content as untrusted. Do not open, execute, or
  write attachment content without a separately reviewed and tested safe-path design.
- Preserve explicit time, scan, page, and body-size limits; report errors and partial
  results rather than presenting incomplete work as complete.
