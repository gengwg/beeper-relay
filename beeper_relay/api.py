"""Small Beeper Desktop HTTP client with explicit unknown send outcomes."""

import json
import urllib.error
import urllib.parse
import urllib.request


class APIError(RuntimeError):
    def __init__(self, message, *, status=None):
        super().__init__(message)
        self.status = status


class SendOutcomeUnknown(APIError):
    pass


class BeeperClient:
    def __init__(self, base_url, token, *, timeout=15):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    @staticmethod
    def _chat_path(chat_id, suffix=""):
        return f"/v1/chats/{urllib.parse.quote(chat_id, safe='')}{suffix}"

    def _request(self, method, path, *, body=None, timeout=None, sending=False):
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout if timeout is None else timeout
            ) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            try:
                payload = json.load(error)
                detail = (
                    payload.get("error") or payload.get("message") or "request failed"
                )
            except (ValueError, TypeError, AttributeError):
                detail = "request failed"
            finally:
                error.close()
            raise APIError(f"HTTP {error.code}: {detail}", status=error.code) from None
        except (TimeoutError, urllib.error.URLError, json.JSONDecodeError) as error:
            if sending:
                raise SendOutcomeUnknown("send outcome is unknown") from error
            raise APIError(f"Beeper request failed: {error}") from error

    def get_chat(self, chat_id):
        return self._request("GET", self._chat_path(chat_id))[1]

    def list_chats(self, *, query=None, limit=50):
        parameters = {"limit": limit}
        if query:
            parameters["query"] = query
        path = "/v1/chats/search?" + urllib.parse.urlencode(parameters)
        return self._request("GET", path)[1]

    def get_message(self, chat_id, message_id):
        path = self._chat_path(
            chat_id, f"/messages/{urllib.parse.quote(message_id, safe='')}"
        )
        return self._request("GET", path)[1]

    def list_messages(self, chat_id, *, limit=50):
        path = self._chat_path(
            chat_id, "/messages?" + urllib.parse.urlencode({"limit": limit})
        )
        return self._request("GET", path)[1]

    def send_message(self, chat_id, text):
        return self._request(
            "POST",
            self._chat_path(chat_id, "/messages"),
            body={"text": text},
            sending=True,
        )[1]
