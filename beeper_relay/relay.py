"""Prepare and execute one-shot forwards with durable duplicate protection."""

import hashlib
import json
import os
import time
import uuid
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

from .api import APIError, SendOutcomeUnknown


class RelayError(RuntimeError):
    pass


class DuplicateOperation(RelayError):
    pass


def utc_now():
    return datetime.now(UTC).isoformat()


def neutralize_mentions(text):
    """Prevent provider mention parsing while retaining readable text."""
    return text.replace("@", "@\u200b")


def text_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


class HTMLDetector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.found = False

    def handle_starttag(self, tag, attrs):
        self.found = True

    def handle_endtag(self, tag):
        self.found = True


def contains_html(text):
    parser = HTMLDetector()
    parser.feed(text)
    parser.close()
    return parser.found


class Forwarder:
    def __init__(self, client, data_dir, *, reconcile_seconds=20):
        self.client = client
        self.data_dir = Path(data_dir)
        self.reconcile_seconds = reconcile_seconds

    def prepare(
        self, source_chat_id, message_id, destination_chat_id, *, text_override=None
    ):
        source = self.client.get_chat(source_chat_id)
        destination = self.client.get_chat(destination_chat_id)
        message = self.client.get_message(source["id"], message_id)
        self._validate_source(source, message)
        self._validate_destination(destination)

        relay_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"beeper-relay:{message['id']}:{destination['id']}",
            )
        )
        original_text = message["text"] if text_override is None else text_override
        if not original_text.strip():
            raise RelayError("there is no message content to forward")
        forwarded_text = (
            f'[Forwarded from {source["network"]} chat "{source["title"]}" '
            f"| Relay {relay_id}]\n{neutralize_mentions(original_text)}"
        )
        max_length = destination.get("capabilities", {}).get("maxTextLength")
        if max_length is not None and len(forwarded_text) > max_length:
            raise RelayError("forwarded message exceeds destination text limit")

        return {
            "relay_id": relay_id,
            "source": source,
            "destination": destination,
            "message": message,
            "original_text": original_text,
            "forwarded_text": forwarded_text,
        }

    @staticmethod
    def _validate_source(chat, message):
        if chat.get("type") != "single":
            raise RelayError("source must be a direct chat")
        if message.get("chatID") not in {chat["id"], chat.get("localChatID")}:
            raise RelayError("source message belongs to a different chat")
        if message.get("isSender") is not True:
            raise RelayError("only your own messages can be forwarded")
        if message.get("type") != "TEXT" or not message.get("text"):
            raise RelayError("only non-empty text messages are supported")
        if contains_html(message["text"]):
            raise RelayError(
                "HTML/rich-text messages are not supported; choose plain text"
            )
        if message.get("isDeleted"):
            raise RelayError("deleted messages cannot be forwarded")

    @staticmethod
    def _validate_destination(chat):
        if chat.get("type") != "single":
            raise RelayError("destination must be a direct chat")
        if chat.get("isReadOnly"):
            raise RelayError("destination is read-only")
        if chat.get("merge"):
            raise RelayError(
                "merged chats have ambiguous routing; choose a concrete chat"
            )

    def _ledger_path(self, relay_id):
        return self.data_dir / "ledger" / f"{relay_id}.json"

    def _save(self, plan, status, **values):
        path = self._ledger_path(plan["relay_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        ledger = getattr(self, "_ledger", None)
        if ledger is None:
            ledger = {
                "relay_id": plan["relay_id"],
                "created_at": utc_now(),
                "source_chat_id": plan["source"]["id"],
                "source_message_id": plan["message"]["id"],
                "destination_chat_id": plan["destination"]["id"],
                "original_text_sha256": text_hash(plan["original_text"]),
                "forwarded_text_sha256": text_hash(plan["forwarded_text"]),
                "automatic_retry": False,
            }
        ledger.update(values)
        ledger["status"] = status
        ledger["updated_at"] = utc_now()
        temporary = path.with_suffix(".tmp")
        temporary.touch(mode=0o600, exist_ok=True)
        temporary.write_text(json.dumps(ledger, indent=2) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        self._ledger = ledger
        self.ledger_path = path
        return ledger

    def execute(self, plan):
        if self._ledger_path(plan["relay_id"]).exists():
            raise DuplicateOperation(
                "this source message was already forwarded to this destination; "
                "inspect the ledger before trying again"
            )
        self._ledger = None
        self._save(plan, "sending")
        try:
            response = self.client.send_message(
                plan["destination"]["id"], plan["forwarded_text"]
            )
        except SendOutcomeUnknown:
            self._save(plan, "unknown")
            return self._ledger
        except APIError as error:
            status = (
                "failed" if error.status and 400 <= error.status < 500 else "unknown"
            )
            self._save(plan, status, http_status=error.status)
            return self._ledger

        pending_id = response.get("pendingMessageID")
        actual_chat_id = response.get("chatID")
        self._save(
            plan,
            "pending",
            pending_message_id=pending_id,
            actual_chat_id=actual_chat_id,
        )
        aliases = {plan["destination"]["id"], plan["destination"].get("localChatID")}
        if actual_chat_id not in aliases:
            self._save(plan, "route_mismatch")
            return self._ledger
        if not pending_id:
            self._save(plan, "unknown")
            return self._ledger
        return self._reconcile(plan, pending_id)

    def _reconcile(self, plan, pending_id):
        deadline = time.monotonic() + self.reconcile_seconds
        while time.monotonic() < deadline:
            try:
                message = self.client.get_message(plan["destination"]["id"], pending_id)
                remote_id = message.get("id")
                if remote_id:
                    self._save(plan, "pending", remote_message_id=remote_id)
                send_status = message.get("sendStatus", {}).get("status")
                if send_status == "SUCCESS":
                    self._save(plan, "sent", remote_message_id=remote_id)
                    return self._ledger
                if send_status in {"FAIL_RETRIABLE", "FAIL_PERMANENT"}:
                    self._save(plan, "failed", provider_status=send_status)
                    return self._ledger
            except APIError as error:
                if (
                    error.status
                    and 400 <= error.status < 500
                    and error.status not in {404, 408, 429}
                ):
                    self._save(plan, "unknown", http_status=error.status)
                    return self._ledger
            time.sleep(min(1, self.reconcile_seconds))
        self._save(plan, "unknown")
        return self._ledger
