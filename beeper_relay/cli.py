"""Command-line interface for Beeper Relay."""

import argparse
import json
import os
import sys
from pathlib import Path

from .api import APIError, BeeperClient
from .relay import DuplicateOperation, Forwarder, RelayError

DEFAULT_BASE_URL = "http://127.0.0.1:23373"


def load_token():
    token = os.environ.get("BEEPER_ACCESS_TOKEN")
    if token:
        return token.strip()
    path = os.environ.get("BEEPER_TOKEN_FILE")
    if not path:
        xdg_config = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        path = str(Path(xdg_config) / "beeper-relay" / "token.json")
    token_path = Path(path)
    if not token_path.exists():
        raise RelayError(
            "set BEEPER_ACCESS_TOKEN or BEEPER_TOKEN_FILE; see README.md for setup"
        )
    contents = token_path.read_text().strip()
    if contents.startswith("{"):
        token = json.loads(contents)["access_token"]
    else:
        token = contents
    if not token:
        raise RelayError("the Beeper token is empty")
    return token


def make_client():
    return BeeperClient(
        os.environ.get("BEEPER_BASE_URL", DEFAULT_BASE_URL), load_token()
    )


def data_directory():
    configured = os.environ.get("BEEPER_RELAY_DATA_DIR")
    if configured:
        return Path(configured)
    xdg_data = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
    return Path(xdg_data) / "beeper-relay"


def show_chats(client, query, limit):
    response = client.list_chats(query=query, limit=limit)
    for chat in response.get("items", [])[:limit]:
        print(
            "\t".join(
                [
                    chat["id"],
                    chat.get("network", ""),
                    chat.get("title", ""),
                    chat.get("type", ""),
                    f"read_only={chat.get('isReadOnly', False)}",
                ]
            )
        )


def show_messages(client, chat_id, limit):
    response = client.list_messages(chat_id, limit=limit)
    for message in response.get("items", [])[:limit]:
        preview = (message.get("text") or "").replace("\n", " ")[:100]
        print(
            "\t".join(
                [
                    message["id"],
                    message.get("timestamp", ""),
                    f"own={message.get('isSender', False)}",
                    message.get("type", ""),
                    preview,
                ]
            )
        )


def forward(client, arguments):
    forwarder = Forwarder(client, data_directory())
    plan = forwarder.prepare(
        arguments.source_chat, arguments.message, arguments.destination_chat
    )
    print(f"Relay ID: {plan['relay_id']}")
    print(f"From: {plan['source']['network']} / {plan['source']['title']}")
    print(f"To: {plan['destination']['network']} / {plan['destination']['title']}")
    print("Forwarded text:\n" + plan["forwarded_text"])
    if not arguments.confirm:
        print("\nDry run only. Re-run with --confirm to send once.")
        return 0

    ledger = forwarder.execute(plan)
    print(f"Status: {ledger['status']}")
    if ledger.get("pending_message_id"):
        print(f"Pending message ID: {ledger['pending_message_id']}")
    if ledger.get("remote_message_id"):
        print(f"Remote message ID: {ledger['remote_message_id']}")
    print(f"Ledger: {forwarder.ledger_path}")
    if ledger["status"] == "unknown":
        print(
            "Outcome unknown: check the destination; no automatic retry was attempted."
        )
    return 0 if ledger["status"] == "sent" else 1


def build_parser():
    parser = argparse.ArgumentParser(
        prog="beeper-relay",
        description="Manually forward your own text messages through Beeper Desktop.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    chats = subparsers.add_parser("chats", help="list/search chats")
    chats.add_argument("--query", help="optional chat search query")
    chats.add_argument("--limit", type=int, default=50)

    messages = subparsers.add_parser("messages", help="list recent messages in a chat")
    messages.add_argument("--chat", required=True, help="chat ID")
    messages.add_argument("--limit", type=int, default=50)

    forward_parser = subparsers.add_parser(
        "forward", help="prepare or send one manual forward"
    )
    forward_parser.add_argument("--source-chat", required=True, help="source chat ID")
    forward_parser.add_argument("--message", required=True, help="source message ID")
    forward_parser.add_argument(
        "--destination-chat", required=True, help="destination chat ID"
    )
    forward_parser.add_argument(
        "--confirm", action="store_true", help="actually send once; default is dry-run"
    )
    return parser


def main(argv=None):
    arguments = build_parser().parse_args(argv)
    try:
        client = make_client()
        if arguments.command == "chats":
            show_chats(client, arguments.query, arguments.limit)
        elif arguments.command == "messages":
            show_messages(client, arguments.chat, arguments.limit)
        else:
            return forward(client, arguments)
        return 0
    except (APIError, RelayError, DuplicateOperation, OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
