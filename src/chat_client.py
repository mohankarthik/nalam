"""Zero-dependency Unix-socket client for the host unified chat bridge.

Uses only the Python standard library so both host automations and isolated
Docker containers (with `/root/.local/state/agentic/gateway` bind-mounted at
`/run/agentic`) can import or vendor this module directly as `src/chat_client.py`.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import socket
from pathlib import Path
from typing import Any, Optional

MAX_MESSAGE_BYTES = 64 * 1024 * 1024
VALID_CHANNELS = ("steward", "alerts", "finance", "medical")
VALID_TRANSPORTS = ("telegram", "matrix")


class ChatSocketError(Exception):
    """Base error raised by chat bridge Unix-socket RPC calls."""

    def __init__(
        self,
        message: str,
        *,
        error_type: Optional[str] = None,
        response: Optional[dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.error_type = error_type
        self.response = response


class ChatGatewayUnavailableError(ChatSocketError):
    """Bridge socket is unreachable, timed out on connect, or transport is unavailable."""


class ChatChannelPermissionError(ChatSocketError):
    """Peer container or uid is not permitted to access the requested channel."""


class ChatProtocolError(ChatSocketError):
    """Bridge returned malformed, truncated, or oversized protocol framing."""


def default_socket_path() -> Path:
    """Resolve the chat bridge Unix socket path for host or container callers."""
    for env_name in ("AGENTIC_CHAT_SOCKET", "CHAT_BRIDGE_SOCKET"):
        val = os.environ.get(env_name)
        if val:
            return Path(val)
    run_dir = Path("/run/agentic")
    if run_dir.exists():
        return run_dir / "chat.sock"
    state_dir = Path(
        os.environ.get("AGENTIC_STATE_DIR", Path.home() / ".local" / "state" / "agentic")
    )
    return state_dir / "gateway" / "chat.sock"


def _validate_channel(channel: str) -> str:
    if not isinstance(channel, str) or channel not in VALID_CHANNELS:
        raise ValueError(f"invalid channel {channel!r}; expected one of {VALID_CHANNELS}")
    return channel


def _validate_transport(transport: Optional[str]) -> Optional[str]:
    if transport is not None and (
        not isinstance(transport, str) or transport not in VALID_TRANSPORTS
    ):
        raise ValueError(
            f"invalid transport {transport!r}; expected None or one of {VALID_TRANSPORTS}"
        )
    return transport


class ChatSocketClient:
    """Client for newline-delimited JSON RPC over the host chat bridge Unix socket."""

    def __init__(
        self,
        socket_path: Optional[Path | str] = None,
        timeout_s: float = 30.0,
        max_message_bytes: int = MAX_MESSAGE_BYTES,
    ):
        self._explicit_socket_path = Path(socket_path) if socket_path is not None else None
        self.timeout_s = float(timeout_s)
        self.max_message_bytes = int(max_message_bytes)

    @property
    def socket_path(self) -> Path:
        return (
            self._explicit_socket_path
            if self._explicit_socket_path is not None
            else default_socket_path()
        )

    def _request(
        self, request: dict[str, Any], timeout_s: Optional[float] = None
    ) -> dict[str, Any]:
        eff_timeout = self.timeout_s if timeout_s is None else float(timeout_s)
        try:
            payload = (json.dumps(request, ensure_ascii=False, allow_nan=False) + "\n").encode(
                "utf-8"
            )
        except (TypeError, ValueError) as exc:
            raise ChatProtocolError(f"cannot encode request: {exc}") from exc
        if len(payload) > self.max_message_bytes:
            raise ChatProtocolError(f"request exceeded {self.max_message_bytes} bytes")

        sock_path = self.socket_path
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(eff_timeout)
            try:
                sock.connect(str(sock_path))
            except OSError as exc:
                raise ChatGatewayUnavailableError(
                    f"cannot connect to chat bridge at {sock_path}: {exc}",
                    error_type="unavailable",
                ) from exc

            try:
                sock.sendall(payload)
                try:
                    sock.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
            except OSError as exc:
                raise ChatGatewayUnavailableError(
                    f"failed sending request to chat bridge: {exc}",
                    error_type="unavailable",
                ) from exc

            chunks: list[bytes] = []
            total = 0
            try:
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    nl = chunk.find(b"\n")
                    if nl != -1:
                        part = chunk[: nl + 1]
                        total += len(part)
                        if total > self.max_message_bytes:
                            raise ChatProtocolError(
                                f"bridge response exceeded {self.max_message_bytes} bytes",
                                error_type="protocol",
                            )
                        chunks.append(part)
                        break
                    total += len(chunk)
                    if total > self.max_message_bytes:
                        raise ChatProtocolError(
                            f"bridge response exceeded {self.max_message_bytes} bytes",
                            error_type="protocol",
                        )
                    chunks.append(chunk)
            except socket.timeout as exc:
                raise ChatSocketError(
                    f"chat bridge request timed out after {eff_timeout}s",
                    error_type="timeout",
                ) from exc
            except OSError as exc:
                raise ChatGatewayUnavailableError(
                    f"error reading from chat bridge: {exc}",
                    error_type="unavailable",
                ) from exc
        finally:
            sock.close()

        raw_line = b"".join(chunks).strip()
        if not raw_line:
            raise ChatProtocolError("empty response from chat bridge", error_type="protocol")
        try:
            resp = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ChatProtocolError(
                f"invalid JSON from chat bridge: {exc}", error_type="protocol"
            ) from exc
        if not isinstance(resp, dict):
            raise ChatProtocolError(
                "chat bridge response is not a JSON object", error_type="protocol"
            )
        if not resp.get("ok"):
            err_msg = str(resp.get("error") or "chat bridge error")
            err_type = resp.get("error_type") or resp.get("error_code")
            if err_type == "permission":
                raise ChatChannelPermissionError(err_msg, error_type=err_type, response=resp)
            if err_type == "unavailable":
                raise ChatGatewayUnavailableError(err_msg, error_type=err_type, response=resp)
            if err_type == "protocol":
                raise ChatProtocolError(err_msg, error_type=err_type, response=resp)
            raise ChatSocketError(err_msg, error_type=err_type, response=resp)
        return resp

    def ping(self, timeout_s: float = 5.0) -> dict[str, Any]:
        return self._request({"op": "ping"}, timeout_s=timeout_s)

    def is_available(self, timeout_s: float = 2.0) -> bool:
        try:
            resp = self.ping(timeout_s=timeout_s)
            return bool(resp.get("ok"))
        except (ChatSocketError, OSError):
            return False

    def register_channel(self, channel: str, transport: Optional[str] = None) -> dict[str, Any]:
        _validate_channel(channel)
        _validate_transport(transport)
        return self._request({"op": "register_channel", "channel": channel, "transport": transport})

    def pull_events(
        self,
        channel: str,
        after_seq: int = 0,
        wait_s: float = 0.0,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        _validate_channel(channel)
        eff_timeout = max(self.timeout_s, float(wait_s) + 15.0)
        resp = self._request(
            {
                "op": "pull_events",
                "channel": channel,
                "after_seq": int(after_seq),
                "wait_s": float(wait_s),
                "limit": int(limit),
            },
            timeout_s=eff_timeout,
        )
        events = resp.get("events")
        if not isinstance(events, list):
            raise ChatProtocolError(
                "pull_events response missing 'events' list", error_type="protocol"
            )
        return events

    def ack_event(self, channel: str, seq: int) -> dict[str, Any]:
        _validate_channel(channel)
        return self._request({"op": "ack_event", "channel": channel, "seq": int(seq)})

    def fetch_attachment(self, channel: str, attachment_id: str) -> dict[str, Any]:
        _validate_channel(channel)
        if not isinstance(attachment_id, str) or not attachment_id.strip():
            raise ValueError("attachment_id must be a non-empty string")
        resp = self._request(
            {"op": "fetch_attachment", "channel": channel, "attachment_id": attachment_id}
        )
        b64 = resp.get("data_b64")
        if not isinstance(b64, str):
            raise ChatProtocolError(
                "fetch_attachment response missing 'data_b64'", error_type="protocol"
            )
        try:
            data = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ChatProtocolError(
                f"invalid base64 in fetch_attachment: {exc}", error_type="protocol"
            ) from exc
        out: dict[str, Any] = {
            "attachment_id": resp.get("attachment_id", attachment_id),
            "filename": resp.get("filename"),
            "mime_type": resp.get("mime_type"),
            "size_bytes": resp.get("size_bytes", len(data)),
            "sha256": resp.get("sha256"),
            "data": data,
        }
        return out

    def send_message(
        self,
        channel: str,
        text: str,
        reply_to: Optional[str | int] = None,
    ) -> dict[str, Any]:
        _validate_channel(channel)
        if not isinstance(text, str):
            raise ValueError("text must be a string")
        return self._request(
            {
                "op": "send_message",
                "channel": channel,
                "text": text,
                "reply_to": str(reply_to) if reply_to is not None else None,
            }
        )

    def send_card(self, channel: str, notification: dict[str, Any]) -> dict[str, Any]:
        _validate_channel(channel)
        if not isinstance(notification, dict):
            raise ValueError("notification must be a dict")
        return self._request(
            {
                "op": "send_card",
                "channel": channel,
                "notification": notification,
            }
        )
