# Beeper Relay

A small, safety-first CLI for manually forwarding **your own plain-text messages** between direct chats connected to Beeper Desktop.

It is intentionally not an automatic relay or a new inbox. Beeper remains the messaging core and can be proprietary; this companion is independent of WeChat and does not connect to it.

## Safety behavior

- Dry-run by default. `--confirm` is required to send.
- Verifies that the source message is yours, is not deleted, and contains plain text without HTML.
- Supports direct chats only and rejects read-only or ambiguous merged-chat destinations.
- Shows source, destination, and forwarded text before sending.
- Adds source attribution and a unique relay ID to the forwarded message.
- Neutralizes `@` mention syntax in forwarded content.
- Uses a deterministic operation ID to prevent forwarding the same message twice from one installation.
- Sends once. A timeout becomes `unknown`, never an automatic retry.
- Stores message hashes and delivery metadata, not message bodies, in its local ledger.

## Requirements

- Beeper Desktop with **Settings → Integrations → Desktop API** enabled.
- Beeper **Remote Access disabled**. The default API URL is `http://127.0.0.1:23373`.
- Python and [`uv`](https://docs.astral.sh/uv/).
- A Beeper token with `read` access for discovery and `read write` access for sending.

Keep tokens outside this repository. Either set `BEEPER_ACCESS_TOKEN` or point `BEEPER_TOKEN_FILE` at a mode-600 file containing either the raw token or JSON with an `access_token` field.

```sh
export BEEPER_TOKEN_FILE=/secure/local/path/beeper-token.json
```

Optional environment variables:

- `BEEPER_BASE_URL`: override the localhost API URL.
- `BEEPER_RELAY_DATA_DIR`: override the ledger directory. Defaults to `$XDG_DATA_HOME/beeper-relay` or `~/.local/share/beeper-relay`.

## Find a chat and source message

```sh
uv run python -m beeper_relay chats --query 'chat name'
uv run python -m beeper_relay messages --chat 'CHAT_ID' --limit 20
```

The message listing includes message IDs, whether each message is yours, and a local preview.

## Preview a forward

```sh
uv run python -m beeper_relay forward \
  --source-chat 'SOURCE_CHAT_ID' \
  --message 'SOURCE_MESSAGE_ID' \
  --destination-chat 'DESTINATION_CHAT_ID'
```

This performs read-only validation and prints the exact forwarded text. It does not send anything.

## Send once

After reviewing the preview, append `--confirm`:

```sh
uv run python -m beeper_relay forward \
  --source-chat 'SOURCE_CHAT_ID' \
  --message 'SOURCE_MESSAGE_ID' \
  --destination-chat 'DESTINATION_CHAT_ID' \
  --confirm
```

Possible final states:

- `sent`: Beeper reported remote-network success.
- `failed`: Beeper returned a definite failure.
- `unknown`: the outcome could not be confirmed. Check the destination; do not blindly retry.
- `route_mismatch`: the returned destination did not match the requested route.
- `pending`: accepted locally but still being reconciled when the command exits.

The ledger is stored under `BEEPER_RELAY_DATA_DIR/ledger/`. Repeating the same source-message-to-destination operation is rejected instead of sent again.

## Automatic one-way relay

Create a private route configuration based on [routes.example.json](routes.example.json). The default location is `~/.config/beeper-relay/routes.json`, or pass `--config`.

```json
{
  "routes": {
    "x-to-slack": {
      "source_chat": "X_CHAT_ID",
      "destination_chat": "SLACK_CHAT_ID",
      "mode": "dedicated"
    }
  }
}
```

Validate the friendly route without sending:

```sh
uv run python -m beeper_relay watch --route x-to-slack
```

Start the watcher:

```sh
uv run python -m beeper_relay watch --route x-to-slack --confirm
```

Route mode `dedicated` forwards every new outgoing plain-text message in that relay-only chat. Use it only for destinations reserved for relaying. If mode is omitted or set to `opt_in`, only new outgoing messages beginning with `/relay ` are forwarded, and the prefix is removed from the forwarded copy.

For example, in `opt_in` mode, `/relay this goes to Slack` becomes `this goes to Slack` at the destination. Press `Ctrl+C` to stop either mode.

Watcher safeguards:

- The first run snapshots existing history and forwards nothing.
- Later polls process only unseen messages at or after the saved cursor.
- In `opt_in` mode, only your new outgoing messages explicitly starting with `/relay ` are eligible; in `dedicated` mode, all your new outgoing text messages are eligible.
- Relay-generated messages are suppressed to prevent echo loops.
- Each route has one active watcher and independent cursor/ledger state.
- Failed or unknown sends are recorded but never retried automatically.
- Catch-up is bounded to the 50 most recent source messages.

Use one-way routes by default. A reverse route is supported only between two dedicated relay chats with relay-generated echo suppression. Do not create arbitrary route chains or reuse ordinary conversations for automatic bidirectional forwarding.

## Tests

```sh
uv run python -m unittest discover -s tests -v
```

The tests run a local fake Beeper HTTP API and cover success, self-message enforcement, route validation, duplicate suppression, definite failures, pending outcomes, and network timeouts. They use no real accounts or messages.

## Current limitations

- No bidirectional relay or arbitrary route chains.
- No group chats, attachments, reactions, edits, or deletion propagation.
- No guarantee of remote delivery beyond the status exposed by Beeper.
- No duplicate suppression across separate computers.
- No additional end-to-end-encryption guarantee. Review each Beeper connection's on-device/cloud topology and the external service's own policies.

## License

MIT. See [LICENSE](LICENSE).
