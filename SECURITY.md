# Security model

Beeper Relay handles message content and can send as the user. Treat the following as security boundaries:

- The CLI and its local ledger.
- Beeper Desktop's localhost API and OAuth/token grants.
- Beeper's on-device or cloud connection for each account.
- Each external messaging service and the destination audience.

A localhost API does not make the complete message path local. Check the connection type for every Beeper account.

## Required operation

- Keep Beeper Remote Access disabled unless you have independently assessed its exposure.
- Store tokens outside the repository in a mode-600 file or a secret manager.
- Use read-only tokens for discovery and grant write access only when sending is required.
- Do not commit tokens, cookies, QR codes, account IDs, chat exports, logs, screenshots, or ledgers.
- Do not enable automatic retries after an unknown send outcome; duplicates are possible.
- Review the destination and rendered text before every confirmed manual send.
- For watched routes, validate the route before starting, run only one watcher per route, and do not relay initial history. Use one-way routes by default; bidirectional forwarding is limited to dedicated relay pairs with echo suppression.
- Keep private route configurations outside the repository and suppress relay-generated echoes before forwarding.

## Data retention

The ledger contains relay IDs, chat/message IDs, timestamps, delivery state, and SHA-256 hashes of source and forwarded text. It does not intentionally store message bodies. Delete the ledger and revoke API tokens when they are no longer needed. Beeper and external services have separate retention and deletion behavior.

Message content is untrusted input. Do not evaluate it as shell commands, HTML, or executable Markdown. Mention neutralization reduces accidental notifications but is not a complete defense against every provider-specific formatting or phishing behavior.

## Scope

This project does not provide a new encryption layer, anonymity, protection from a compromised desktop, or a high-security whistleblowing tool. Beeper and external messaging providers retain their own trust and policy boundaries.

## Reporting a vulnerability

Do not attach credentials, private messages, account identifiers, or unredacted logs to a public issue. Use synthetic data and describe the affected code path and expected versus actual behavior.
