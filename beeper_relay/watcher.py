"""Bounded polling for one-way, new-message-only relay routes."""

import fcntl
import json
import os
import time
from pathlib import Path

from .api import APIError
from .relay import DuplicateOperation, Forwarder, RelayError

RELAY_PREFIX = "/relay "


def sort_key(message):
    try:
        return int(message.get("sortKey"))
    except (TypeError, ValueError):
        return 0


class Watcher:
    def __init__(self, client, route_name, route, data_dir, *, reconcile_seconds=20):
        self.client = client
        self.route_name = route_name
        self.source_chat_id = route["source_chat"]
        self.destination_chat_id = route["destination_chat"]
        self.mode = route.get("mode", "opt_in")
        if self.mode not in {"opt_in", "dedicated"}:
            raise RelayError(f"unsupported route mode: {self.mode}")
        self.data_dir = Path(data_dir)
        self.forwarder = Forwarder(
            client, self.data_dir, reconcile_seconds=reconcile_seconds
        )
        self.state_path = self.data_dir / "state" / f"{route_name}.json"
        self.lock_path = self.data_dir / "state" / f"{route_name}.lock"
        self._lock_file = None

    def _load_state(self):
        if not self.state_path.exists():
            return None
        return json.loads(self.state_path.read_text())

    def _save_state(self, state):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.touch(mode=0o600, exist_ok=True)
        temporary.write_text(json.dumps(state, indent=2) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.state_path)

    @staticmethod
    def _is_relay_generated(message):
        text = message.get("text") or ""
        return text.startswith("[Forwarded from ") and " | Relay " in text

    def _advance(self, state, message):
        key = sort_key(message)
        state["cursor"] = max(state["cursor"], key)
        state["seen_ids"].append(message["id"])
        state["seen_ids"] = state["seen_ids"][-500:]
        self._save_state(state)

    def describe(self):
        source = self.client.get_chat(self.source_chat_id)
        destination = self.client.get_chat(self.destination_chat_id)
        return source, destination

    def run_once(self):
        state = self._load_state()
        response = self.client.list_messages(self.source_chat_id, limit=50)
        messages = response.get("items", [])
        if state is None:
            state = {
                "cursor": max((sort_key(message) for message in messages), default=0),
                "seen_ids": [message["id"] for message in messages][-500:],
            }
            self._save_state(state)
            return [{"status": "initialized", "count": len(messages)}]

        candidates = sorted(
            (
                message
                for message in messages
                if message["id"] not in state["seen_ids"]
                and sort_key(message) >= state["cursor"]
            ),
            key=sort_key,
        )
        results = []
        for message in candidates:
            if self._is_relay_generated(message):
                self._advance(state, message)
                results.append({"id": message["id"], "status": "relay_echo_suppressed"})
                continue
            text = message.get("text") or ""
            if self.mode == "opt_in" and not text.startswith(RELAY_PREFIX):
                self._advance(state, message)
                results.append({"id": message["id"], "status": "not_requested"})
                continue
            try:
                plan = self.forwarder.prepare(
                    self.source_chat_id,
                    message["id"],
                    self.destination_chat_id,
                    text_override=(
                        text[len(RELAY_PREFIX) :] if self.mode == "opt_in" else None
                    ),
                )
                ledger = self.forwarder.execute(plan)
                status = ledger["status"]
            except DuplicateOperation:
                status = "duplicate_suppressed"
            except RelayError as error:
                status = f"skipped: {error}"
            self._advance(state, message)
            results.append({"id": message["id"], "status": status})
        return results

    def acquire_lock(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file = self.lock_path.open("a+")
        os.chmod(self.lock_path, 0o600)
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock_file.close()
            self._lock_file = None
            raise RelayError(
                f"route {self.route_name!r} is already being watched"
            ) from None

    def release_lock(self):
        if self._lock_file is not None:
            fcntl.flock(self._lock_file, fcntl.LOCK_UN)
            self._lock_file.close()
            self._lock_file = None

    def run_forever(self, *, interval=2, once=False):
        self.acquire_lock()
        try:
            while True:
                try:
                    results = self.run_once()
                    for result in results:
                        print(
                            f"{self.route_name}: {result['status']}"
                            + (f" ({result['id']})" if "id" in result else ""),
                            flush=True,
                        )
                except APIError as error:
                    print(
                        f"{self.route_name}: Beeper unavailable ({error}); retrying",
                        flush=True,
                    )
                if once:
                    return
                time.sleep(interval)
        finally:
            self.release_lock()
