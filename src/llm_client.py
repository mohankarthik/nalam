"""Zero-dependency Unix-socket client for the host CLI LLM gateway.

Uses only the Python standard library so both host automations and isolated
Docker containers (with `/root/.local/state/agentic/gateway` bind-mounted at
`/run/agentic`) can import or vendor this module directly.

Each request carries an optional "caller" tag (the `caller` argument, else
$AGENTIC_CALLER; [A-Za-z0-9_-]{1,32}); the gateway runs the session in the agy
project auto-<caller>, or auto-gateway without one.
"""

from __future__ import annotations

import base64
import json
import os
import re
import socket
from pathlib import Path
from typing import Any, Callable, Optional

MAX_RESPONSE_BYTES = 64 * 1024 * 1024
SOCKET_TIMEOUT_GRACE_S = 15.0
# Caller tag sent with each request; the gateway runs the session in the agy
# project auto-<caller> (e.g. AGENTIC_CALLER=nalam -> auto-nalam).
CALLER_ENV = "AGENTIC_CALLER"
CALLER_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}\Z")


class LLMGatewayError(Exception):
    """Base error raised by LLM gateway RPC calls."""

    def __init__(
        self,
        message: str,
        *,
        error_code: Optional[str] = None,
        response: Optional[dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.response = response


class LLMUnavailableError(LLMGatewayError):
    """Gateway socket is unreachable or all candidate model pools are unavailable."""


class LLMProtocolError(LLMGatewayError):
    """Gateway returned malformed, truncated, or oversized protocol framing."""


class LLMToolLoopError(LLMGatewayError):
    """Tool loop exceeded max_rounds without reaching a final turn."""


def default_socket_path() -> Path:
    """Resolve the LLM gateway Unix socket path for host or container callers."""
    env_sock = os.environ.get("AGENTIC_LLM_SOCKET") or os.environ.get("LLM_GATEWAY_SOCKET")
    if env_sock:
        return Path(env_sock)
    run_dir = Path("/run/agentic")
    if run_dir.exists():
        return run_dir / "llm.sock"
    state_dir = Path(
        os.environ.get("AGENTIC_STATE_DIR", Path.home() / ".local" / "state" / "agentic")
    )
    return state_dir / "gateway" / "llm.sock"


def resolve_caller(caller: Optional[str] = None) -> Optional[str]:
    """The explicit caller, else $AGENTIC_CALLER; None when neither is a valid tag
    (the gateway then uses "gateway")."""
    for cand in (caller, os.environ.get(CALLER_ENV)):
        if isinstance(cand, str) and CALLER_RE.match(cand.strip()):
            return cand.strip()
    return None


def _normalize_attachment(item: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError("attachment must be a dict")
    filename = item.get("filename")
    if not isinstance(filename, str) or not filename.strip():
        raise ValueError("attachment requires a non-empty 'filename'")
    if "data" in item and item["data"] is not None:
        raw = item["data"]
        if isinstance(raw, memoryview):
            raw = raw.tobytes()
        elif isinstance(raw, bytearray):
            raw = bytes(raw)
        if not isinstance(raw, bytes):
            raise ValueError("attachment 'data' must be bytes")
        data_b64 = base64.b64encode(raw).decode("ascii")
    elif isinstance(item.get("data_b64"), str):
        data_b64 = item["data_b64"]
    else:
        raise ValueError("attachment requires 'data' (bytes) or 'data_b64' (str)")
    out: dict[str, Any] = {"filename": filename, "data_b64": data_b64}
    if item.get("media_type") is not None:
        out["media_type"] = str(item["media_type"])
    return out


class LLMClient:
    """Client for newline-delimited JSON RPC over the host LLM gateway Unix socket."""

    def __init__(
        self,
        socket_path: Optional[Path | str] = None,
        default_timeout_s: float = 300.0,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        caller: Optional[str] = None,
    ):
        self._explicit_socket_path = Path(socket_path) if socket_path is not None else None
        self.caller = caller
        self.default_timeout_s = float(default_timeout_s)
        self.max_response_bytes = int(max_response_bytes)

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
        eff_timeout = self.default_timeout_s if timeout_s is None else float(timeout_s)
        sock_path = self.socket_path
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(eff_timeout)
            try:
                sock.connect(str(sock_path))
            except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
                raise LLMUnavailableError(
                    f"cannot connect to LLM gateway at {sock_path}: {exc}"
                ) from exc

            payload = (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")
            try:
                sock.sendall(payload)
                try:
                    sock.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
            except OSError as exc:
                raise LLMUnavailableError(f"failed sending request to LLM gateway: {exc}") from exc

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
                        if total > self.max_response_bytes:
                            raise LLMProtocolError(
                                f"gateway response exceeded {self.max_response_bytes} bytes"
                            )
                        chunks.append(part)
                        break
                    total += len(chunk)
                    if total > self.max_response_bytes:
                        raise LLMProtocolError(
                            f"gateway response exceeded {self.max_response_bytes} bytes"
                        )
                    chunks.append(chunk)
            except socket.timeout as exc:
                raise LLMGatewayError(
                    f"LLM gateway request timed out after {eff_timeout}s",
                    error_code="timeout",
                ) from exc
            except OSError as exc:
                raise LLMUnavailableError(f"error reading from LLM gateway: {exc}") from exc
        finally:
            sock.close()

        raw_line = b"".join(chunks).strip()
        if not raw_line:
            raise LLMProtocolError("empty response from LLM gateway")
        try:
            resp = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LLMProtocolError(f"invalid JSON from LLM gateway: {exc}") from exc
        if not isinstance(resp, dict):
            raise LLMProtocolError("LLM gateway response is not a JSON object")
        if not resp.get("ok"):
            err_msg = str(resp.get("error") or "gateway error")
            err_code = resp.get("error_code")
            if err_code == "unavailable":
                raise LLMUnavailableError(err_msg, error_code=err_code, response=resp)
            raise LLMGatewayError(err_msg, error_code=err_code, response=resp)
        return resp

    def ping(self, timeout_s: float = 5.0) -> dict[str, Any]:
        return self._request({"op": "ping"}, timeout_s=timeout_s)

    def is_available(self, timeout_s: float = 2.0) -> bool:
        try:
            resp = self.ping(timeout_s=timeout_s)
            return bool(resp.get("ok"))
        except (LLMGatewayError, OSError):
            return False

    def complete(
        self,
        prompt: str = "",
        *,
        system: Optional[str] = None,
        payloads: Optional[list[Any]] = None,
        attachments: Optional[list[dict[str, Any]]] = None,
        messages: Optional[list[dict[str, Any]]] = None,
        tools: Optional[list[dict[str, Any]]] = None,
        response_format: str = "text",
        json_schema: Optional[dict[str, Any]] = None,
        tier: str = "fast",
        family: Optional[str] = None,
        timeout_s: Optional[float] = None,
        title: str = "llm_gateway",
        caller: Optional[str] = None,
    ) -> dict[str, Any]:
        eff_timeout = self.default_timeout_s if timeout_s is None else float(timeout_s)
        req: dict[str, Any] = {
            "op": "complete",
            "prompt": prompt,
            "response_format": response_format,
            "tier": tier,
            "timeout_s": eff_timeout,
            "title": title,
        }
        if system is not None:
            req["system"] = system
        if payloads is not None:
            req["payloads"] = list(payloads)
        if attachments is not None:
            req["attachments"] = [_normalize_attachment(a) for a in attachments]
        if messages is not None:
            req["messages"] = list(messages)
        if tools is not None:
            req["tools"] = list(tools)
        if json_schema is not None:
            req["json_schema"] = json_schema
        if family is not None:
            req["family"] = family
        eff_caller = resolve_caller(caller if caller is not None else self.caller)
        if eff_caller is not None:
            req["caller"] = eff_caller
        return self._request(req, timeout_s=eff_timeout + SOCKET_TIMEOUT_GRACE_S)

    def complete_text(
        self,
        prompt: str = "",
        *,
        system: Optional[str] = None,
        payloads: Optional[list[Any]] = None,
        attachments: Optional[list[dict[str, Any]]] = None,
        messages: Optional[list[dict[str, Any]]] = None,
        tier: str = "fast",
        family: Optional[str] = None,
        timeout_s: Optional[float] = None,
        title: str = "llm_gateway",
        caller: Optional[str] = None,
    ) -> tuple[str, dict[str, Any]]:
        resp = self.complete(
            prompt=prompt,
            system=system,
            payloads=payloads,
            attachments=attachments,
            messages=messages,
            response_format="text",
            tier=tier,
            family=family,
            timeout_s=timeout_s,
            title=title,
            caller=caller,
        )
        if "text" not in resp:
            raise LLMProtocolError("gateway response missing 'text'")
        return str(resp["text"]), dict(resp.get("meta") or {})

    def complete_json(
        self,
        prompt: str = "",
        *,
        system: Optional[str] = None,
        payloads: Optional[list[Any]] = None,
        attachments: Optional[list[dict[str, Any]]] = None,
        messages: Optional[list[dict[str, Any]]] = None,
        json_schema: Optional[dict[str, Any]] = None,
        tier: str = "fast",
        family: Optional[str] = None,
        timeout_s: Optional[float] = None,
        title: str = "llm_gateway",
        caller: Optional[str] = None,
    ) -> tuple[Any, dict[str, Any]]:
        resp = self.complete(
            prompt=prompt,
            system=system,
            payloads=payloads,
            attachments=attachments,
            messages=messages,
            response_format="json",
            json_schema=json_schema,
            tier=tier,
            family=family,
            timeout_s=timeout_s,
            title=title,
            caller=caller,
        )
        if "data" not in resp:
            raise LLMProtocolError("gateway response missing 'data'")
        return resp["data"], dict(resp.get("meta") or {})

    def complete_pdf(
        self,
        prompt: str,
        pdf_bytes: bytes,
        *,
        filename: str = "document.pdf",
        system: Optional[str] = None,
        payloads: Optional[list[Any]] = None,
        response_format: str = "json",
        json_schema: Optional[dict[str, Any]] = None,
        tier: str = "fast",
        family: Optional[str] = None,
        timeout_s: Optional[float] = None,
        title: str = "pdf_extract",
        caller: Optional[str] = None,
    ) -> tuple[Any, dict[str, Any]]:
        resp = self.complete(
            prompt=prompt,
            system=system,
            payloads=payloads,
            attachments=[
                {"filename": filename, "data": pdf_bytes, "media_type": "application/pdf"}
            ],
            response_format=response_format,
            json_schema=json_schema,
            tier=tier,
            family=family,
            timeout_s=timeout_s,
            title=title,
            caller=caller,
        )
        key = "data" if response_format == "json" else "text"
        if key not in resp:
            raise LLMProtocolError(f"gateway response missing {key!r}")
        return resp[key], dict(resp.get("meta") or {})

    def run_tool_loop(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        dispatch: dict[str, Callable[..., Any]],
        *,
        tier: str = "fast",
        family: Optional[str] = None,
        max_rounds: int = 4,
        timeout_s: Optional[float] = None,
        title: str = "tool_loop",
        caller: Optional[str] = None,
    ) -> tuple[str, dict[str, Any]]:
        msgs: list[dict[str, Any]] = [dict(m) for m in messages]
        for _ in range(max_rounds):
            resp = self.complete(
                messages=msgs,
                tools=tools,
                tier=tier,
                family=family,
                timeout_s=timeout_s,
                title=title,
                caller=caller,
            )
            turn = resp.get("turn")
            if not isinstance(turn, dict):
                raise LLMProtocolError("gateway tool-loop response missing 'turn' object")
            ttype = turn.get("type")
            if ttype == "final":
                return str(turn.get("content") or ""), dict(resp.get("meta") or {})
            if ttype == "tool_calls":
                calls = turn.get("calls")
                if not isinstance(calls, list) or not calls:
                    raise LLMProtocolError("tool_calls turn requires a non-empty 'calls' list")
                msgs.append(
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": c["id"],
                                "type": "function",
                                "function": {
                                    "name": c["name"],
                                    "arguments": json.dumps(c.get("arguments", {})),
                                },
                            }
                            for c in calls
                        ],
                    }
                )
                for c in calls:
                    name = c["name"]
                    args = c.get("arguments", {})
                    if not isinstance(args, dict):
                        result: Any = {"error": "tool arguments must be a JSON object"}
                    elif name not in dispatch:
                        result = {"error": f"no such tool: {name}"}
                    else:
                        try:
                            result = dispatch[name](**args)
                        except Exception as exc:  # noqa: BLE001 - return tool errors to model
                            result = {"error": f"{type(exc).__name__}: {exc}"}
                    msgs.append(
                        {
                            "role": "tool",
                            "tool_call_id": c["id"],
                            "content": json.dumps(result, default=str),
                        }
                    )
                continue
            raise LLMProtocolError(f"unsupported turn type: {ttype!r}")
        raise LLMToolLoopError(f"exceeded max_rounds ({max_rounds})")


def ping(timeout_s: float = 5.0, *, socket_path: Optional[Path | str] = None) -> dict[str, Any]:
    return LLMClient(socket_path=socket_path).ping(timeout_s=timeout_s)


def is_available(timeout_s: float = 2.0, *, socket_path: Optional[Path | str] = None) -> bool:
    return LLMClient(socket_path=socket_path).is_available(timeout_s=timeout_s)


def complete(
    prompt: str = "",
    *,
    socket_path: Optional[Path | str] = None,
    **kwargs: Any,
) -> dict[str, Any]:
    return LLMClient(socket_path=socket_path).complete(prompt=prompt, **kwargs)


def complete_text(
    prompt: str = "",
    *,
    socket_path: Optional[Path | str] = None,
    **kwargs: Any,
) -> tuple[str, dict[str, Any]]:
    return LLMClient(socket_path=socket_path).complete_text(prompt=prompt, **kwargs)


def complete_json(
    prompt: str = "",
    *,
    socket_path: Optional[Path | str] = None,
    **kwargs: Any,
) -> tuple[Any, dict[str, Any]]:
    return LLMClient(socket_path=socket_path).complete_json(prompt=prompt, **kwargs)


def complete_pdf(
    prompt: str,
    pdf_bytes: bytes,
    *,
    socket_path: Optional[Path | str] = None,
    **kwargs: Any,
) -> tuple[Any, dict[str, Any]]:
    return LLMClient(socket_path=socket_path).complete_pdf(prompt, pdf_bytes, **kwargs)


def run_tool_loop(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    dispatch: dict[str, Callable[..., Any]],
    *,
    socket_path: Optional[Path | str] = None,
    **kwargs: Any,
) -> tuple[str, dict[str, Any]]:
    return LLMClient(socket_path=socket_path).run_tool_loop(messages, tools, dispatch, **kwargs)
