import json
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from beeper_relay.api import BeeperClient
from beeper_relay.relay import DuplicateOperation, Forwarder, RelayError


class FakeBeeperHandler(BaseHTTPRequestHandler):
    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _authorized(self):
        return self.headers.get("Authorization") == "Bearer test-token"

    def do_GET(self):
        if not self._authorized():
            self._json({"error": "unauthorized"}, 401)
            return
        path = urllib.parse.unquote(self.path)
        state = self.server.state
        if path == "/v1/chats/source-chat":
            self._json(state["source_chat"])
        elif path == "/v1/chats/destination-chat":
            self._json(state["destination_chat"])
        elif path == "/v1/chats/source-chat/messages/message-1":
            self._json(state["source_message"])
        elif path == "/v1/chats/destination-chat/messages/pending-1":
            self._json(state["sent_message"])
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._authorized():
            self._json({"error": "unauthorized"}, 401)
            return
        path = urllib.parse.unquote(self.path)
        if path != "/v1/chats/destination-chat/messages":
            self._json({"error": "not found"}, 404)
            return
        state = self.server.state
        state["post_count"] += 1
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        state["forwarded_text"] = body["text"]
        state["sent_message"].update(
            {
                "id": "remote-1",
                "chatID": "destination-chat",
                "text": body["text"],
                "sendStatus": {"status": state["provider_status"]},
            }
        )
        mode = state["mode"]
        if mode == "timeout":
            time.sleep(0.2)
        if mode == "http_error":
            self._json({"error": "rejected"}, 422)
        elif mode == "missing_pending":
            self._json({"chatID": "destination-chat"})
        else:
            self._json(
                {
                    "chatID": "other-chat"
                    if mode == "mismatch"
                    else "destination-chat",
                    "pendingMessageID": "pending-1",
                }
            )

    def log_message(self, format, *args):
        pass


class ForwarderTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        state = {
            "source_chat": {
                "id": "source-chat",
                "localChatID": "source-local",
                "network": "Slack",
                "title": "Saved messages",
                "type": "single",
                "isReadOnly": False,
            },
            "destination_chat": {
                "id": "destination-chat",
                "localChatID": "destination-local",
                "network": "Twitter/X",
                "title": "Test recipient",
                "type": "single",
                "isReadOnly": False,
                "capabilities": {"maxTextLength": 10000},
            },
            "source_message": {
                "id": "message-1",
                "chatID": "source-chat",
                "isSender": True,
                "isDeleted": False,
                "type": "TEXT",
                "text": "hello @channel",
            },
            "sent_message": {},
            "post_count": 0,
            "provider_status": "SUCCESS",
            "mode": "success",
        }
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeBeeperHandler)
        self.server.state = state
        self.state = state
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = BeeperClient(
            f"http://127.0.0.1:{self.server.server_address[1]}",
            "test-token",
            timeout=0.05,
        )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tempdir.cleanup()

    def forwarder(self, reconcile_seconds=0.05):
        return Forwarder(
            self.client, Path(self.tempdir.name), reconcile_seconds=reconcile_seconds
        )

    def prepare(self, forwarder=None):
        return (forwarder or self.forwarder()).prepare(
            "source-chat", "message-1", "destination-chat"
        )

    def test_prepare_is_read_only_and_neutralizes_mentions(self):
        forwarder = self.forwarder()
        plan = forwarder.prepare("source-chat", "message-1", "destination-chat")
        self.assertIn("@\u200bchannel", plan["forwarded_text"])
        self.assertIn(plan["relay_id"], plan["forwarded_text"])
        self.assertEqual(self.state["post_count"], 0)
        second = forwarder.prepare("source-chat", "message-1", "destination-chat")
        self.assertEqual(plan["relay_id"], second["relay_id"])

    def test_success_writes_metadata_only_ledger(self):
        forwarder = self.forwarder()
        plan = forwarder.prepare("source-chat", "message-1", "destination-chat")
        ledger = forwarder.execute(plan)
        self.assertEqual(ledger["status"], "sent")
        self.assertEqual(ledger["remote_message_id"], "remote-1")
        self.assertEqual(self.state["post_count"], 1)
        raw = forwarder.ledger_path.read_text()
        self.assertNotIn(plan["original_text"], raw)
        self.assertNotIn(plan["forwarded_text"], raw)
        self.assertIn("forwarded_text_sha256", raw)

    def test_duplicate_never_resends(self):
        forwarder = self.forwarder()
        plan = forwarder.prepare("source-chat", "message-1", "destination-chat")
        forwarder.execute(plan)
        with self.assertRaises(DuplicateOperation):
            forwarder.execute(plan)
        self.assertEqual(self.state["post_count"], 1)

    def test_non_self_message_is_rejected_before_send(self):
        self.state["source_message"]["isSender"] = False
        with self.assertRaises(RelayError):
            self.prepare()
        self.assertEqual(self.state["post_count"], 0)

    def test_html_message_is_rejected_before_send(self):
        self.state["source_message"]["text"] = "<p>hello <b>world</b></p>"
        with self.assertRaises(RelayError):
            self.prepare()
        self.assertEqual(self.state["post_count"], 0)

    def test_merged_destination_is_rejected_before_send(self):
        self.state["destination_chat"]["merge"] = {"chatIDs": ["another-chat"]}
        with self.assertRaises(RelayError):
            self.prepare()
        self.assertEqual(self.state["post_count"], 0)

    def test_group_destination_is_rejected_before_send(self):
        self.state["destination_chat"]["type"] = "group"
        with self.assertRaises(RelayError):
            self.prepare()
        self.assertEqual(self.state["post_count"], 0)

    def test_timeout_is_unknown_and_not_retried(self):
        self.state["mode"] = "timeout"
        forwarder = self.forwarder()
        plan = forwarder.prepare("source-chat", "message-1", "destination-chat")
        ledger = forwarder.execute(plan)
        self.assertEqual(ledger["status"], "unknown")
        self.assertEqual(self.state["post_count"], 1)

    def test_pending_becomes_unknown_without_retry(self):
        self.state["provider_status"] = "PENDING"
        forwarder = self.forwarder(reconcile_seconds=0.01)
        plan = forwarder.prepare("source-chat", "message-1", "destination-chat")
        ledger = forwarder.execute(plan)
        self.assertEqual(ledger["status"], "unknown")
        self.assertEqual(self.state["post_count"], 1)

    def test_definite_http_failure_is_not_retried(self):
        self.state["mode"] = "http_error"
        forwarder = self.forwarder()
        plan = forwarder.prepare("source-chat", "message-1", "destination-chat")
        ledger = forwarder.execute(plan)
        self.assertEqual(ledger["status"], "failed")
        self.assertEqual(self.state["post_count"], 1)

    def test_actual_route_mismatch_is_reported(self):
        self.state["mode"] = "mismatch"
        forwarder = self.forwarder()
        plan = forwarder.prepare("source-chat", "message-1", "destination-chat")
        ledger = forwarder.execute(plan)
        self.assertEqual(ledger["status"], "route_mismatch")
        self.assertEqual(self.state["post_count"], 1)

    def test_missing_pending_id_is_unknown(self):
        self.state["mode"] = "missing_pending"
        forwarder = self.forwarder()
        plan = forwarder.prepare("source-chat", "message-1", "destination-chat")
        ledger = forwarder.execute(plan)
        self.assertEqual(ledger["status"], "unknown")
        self.assertEqual(self.state["post_count"], 1)


if __name__ == "__main__":
    unittest.main()
