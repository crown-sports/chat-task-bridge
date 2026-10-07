"""Feishu transport with explicit persistence and delivery acknowledgements."""

from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import suppress
from typing import Any
from urllib.parse import quote

import httpx

from chatbridge.model import Attachment, DeliveryReceipt, InboundMessage, TaskResult


class FeishuError(RuntimeError):
    """Carry a safe diagnostic without platform payloads or credentials."""


class _AmbiguousResponse(FeishuError):
    pass


def _filename(value: str) -> str:
    leaf = value.replace("\\", "/").rsplit("/", 1)[-1]
    return re.sub(r"[\x00-\x1f\x7f]", "_", leaf).strip(" .")[:180] or "attachment.bin"


def _required(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise FeishuError("Required platform field is missing")
    return value


def _command_text(text: str, mentions: Any) -> str:
    """Remove leading platform mention placeholders only before a slash command."""
    if not isinstance(mentions, list):
        return text
    keys = {
        item.get("key")
        for item in mentions
        if isinstance(item, dict) and isinstance(item.get("key"), str)
    }
    remainder = text
    while match := re.match(r"^\s*(@_user_\d+)(?:\s+|$)", remainder):
        if match.group(1) not in keys:
            break
        remainder = remainder[match.end() :]
    return remainder if remainder.startswith("/") else text


def _sdk_worker(app_id: str, secret: str, connection: Any) -> None:
    """Isolate the official SDK's blocking event loop and acknowledge committed events."""
    try:
        import lark_oapi as lark
        from lark_oapi.core.log import logger

        logger.disabled = True

        def callback(event: Any) -> None:
            payload = json.loads(lark.JSON.marshal(event))
            ticket = str(time.monotonic_ns())
            connection.send({"event": payload, "ticket": ticket})
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if not connection.poll(max(0, deadline - time.monotonic())):
                    break
                acknowledgement = connection.recv()
                if acknowledgement[0] == ticket:
                    if acknowledgement[1] is True:
                        return
                    break
            raise FeishuError("Inbound persistence was not confirmed")

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(callback)
            .build()
        )
        lark.ws.Client(app_id, secret, event_handler=handler, log_level=lark.LogLevel.ERROR).start()
    except Exception:
        with suppress(OSError):
            connection.send({"error": "Feishu connection stopped"})
    finally:
        connection.close()


class FeishuChannel:
    """Adapt Feishu events and REST calls without coupling to an Agent framework."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        allowed_senders: frozenset[str] = frozenset(),
        account_id: str | None = None,
        client: httpx.AsyncClient | None = None,
        max_attachment_bytes: int = 8 * 1024 * 1024,
        submit: Callable[[InboundMessage], Awaitable[Any]] | None = None,
    ) -> None:
        if not app_id or not app_secret or max_attachment_bytes <= 0:
            raise ValueError("Feishu credentials and a positive attachment limit are required")
        self.account_id = account_id or app_id
        self._app_id = app_id
        self._secret = app_secret
        self._allowed = frozenset(allowed_senders)
        self._limit = max_attachment_bytes
        self._submit = submit
        self._client = client or httpx.AsyncClient(
            base_url="https://open.feishu.cn/open-apis/", timeout=30, follow_redirects=False
        )
        self._owns_client = client is None
        self._token = ""
        self._expires_at = 0.0
        self._token_lock = asyncio.Lock()
        self._queue: asyncio.Queue[tuple[InboundMessage, asyncio.Future[None]] | None] = (
            asyncio.Queue(32)
        )
        self._process: Any = None
        self._connection: Any = None
        self._pump: asyncio.Task[None] | None = None
        self._closed = False
        self._source_error = False
        self._stopped = asyncio.Event()
        self._pending: set[asyncio.Future[None]] = set()

    async def _access_token(self) -> str:
        async with self._token_lock:
            if self._token and time.monotonic() < self._expires_at:
                return self._token
            try:
                response = await self._client.post(
                    "auth/v3/tenant_access_token/internal",
                    json={"app_id": self._app_id, "app_secret": self._secret},
                )
                payload = self._validated_json(response)
                token = _required(payload, "tenant_access_token")
                expiry = payload.get("expire")
                if not isinstance(expiry, (int, float)) or isinstance(expiry, bool) or expiry <= 0:
                    raise FeishuError("Feishu token expiry is invalid")
            except httpx.HTTPError:
                raise FeishuError("Feishu authentication transport failed") from None
            self._token = token
            self._expires_at = time.monotonic() + max(0, expiry - 60)
            return token

    @staticmethod
    def _validated_json(response: httpx.Response) -> dict[str, Any]:
        if response.status_code >= 500:
            raise _AmbiguousResponse("Feishu server outcome is unconfirmed")
        if response.status_code < 200 or response.status_code >= 300:
            raise FeishuError("Feishu rejected the HTTP request")
        try:
            payload = response.json()
        except ValueError:
            raise _AmbiguousResponse("Feishu response is not a valid receipt") from None
        if not isinstance(payload, dict) or type(payload.get("code")) is not int:
            raise _AmbiguousResponse("Feishu response has no status code")
        if payload["code"] != 0:
            raise FeishuError("Feishu rejected the API request")
        return payload

    async def _post(self, path: str, **kwargs: Any) -> dict[str, Any]:
        token = await self._access_token()
        response = await self._client.post(
            path, headers={"Authorization": f"Bearer {token}"}, **kwargs
        )
        payload = self._validated_json(response)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise _AmbiguousResponse("Feishu response has no delivery data")
        return data

    async def _download(self, message_id: str, key: str, kind: str) -> bytes:
        token = await self._access_token()
        path = f"im/v1/messages/{quote(message_id, safe='')}/resources/{quote(key, safe='')}"
        try:
            async with self._client.stream(
                "GET", path, params={"type": kind}, headers={"Authorization": f"Bearer {token}"}
            ) as response:
                if response.status_code != 200:
                    raise FeishuError("Feishu attachment download was rejected")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > self._limit:
                        raise FeishuError("Feishu attachment exceeds the configured limit")
                    body.extend(chunk)
                return bytes(body)
        except httpx.HTTPError:
            raise FeishuError("Feishu attachment transfer failed") from None

    async def parse_event(self, payload: Mapping[str, Any]) -> InboundMessage | None:
        """Normalize authenticated SDK events; ignore unapproved senders and unsupported types."""
        header = payload.get("header")
        event = payload.get("event")
        if not isinstance(header, dict) or not isinstance(event, dict):
            raise FeishuError("Feishu event structure is invalid")
        if header.get("event_type") != "im.message.receive_v1":
            return None
        if header.get("app_id") != self._app_id:
            raise FeishuError("Feishu event belongs to another application")
        sender = event.get("sender", {})
        if not isinstance(sender, dict) or sender.get("sender_type") != "user":
            return None
        identity = sender.get("sender_id", {})
        sender_id = _required(identity, "open_id") if isinstance(identity, dict) else ""
        if sender_id not in self._allowed:
            return None
        message = event.get("message")
        if not isinstance(message, dict):
            raise FeishuError("Feishu message is missing")
        message_id = _required(message, "message_id")
        conversation = _required(message, "chat_id")
        try:
            content = json.loads(_required(message, "content"))
        except ValueError:
            raise FeishuError("Feishu message content is invalid") from None
        if not isinstance(content, dict):
            raise FeishuError("Feishu message content must be an object")
        kind = message.get("message_type")
        attachments: tuple[Attachment, ...] = ()
        if kind == "text":
            text = _command_text(_required(content, "text"), message.get("mentions"))
        elif kind in {"file", "image"}:
            key = _required(content, "image_key" if kind == "image" else "file_key")
            name = _filename(
                str(
                    content.get("file_name")
                    or ("image.png" if kind == "image" else "attachment.bin")
                )
            )
            body = await self._download(message_id, key, kind)
            attachments = (Attachment(name, body),)
            text = ""
        else:
            return None
        thread_id = message.get("thread_id") or ""
        if not isinstance(thread_id, str):
            raise FeishuError("Feishu thread identity is invalid")
        return InboundMessage(
            event_id=_required(header, "event_id"),
            channel="feishu",
            account_id=self.account_id,
            conversation_id=conversation,
            sender_id=sender_id,
            text=text,
            attachments=attachments,
            reply_token=message_id,
            thread_id=thread_id,
        )

    async def ingest(self, payload: Mapping[str, Any]) -> None:
        """Return only after durable submit or after the iterator consumer confirms persistence."""
        if self._closed:
            raise FeishuError("Feishu channel is closed")
        message = await self.parse_event(payload)
        if message is None:
            return
        if self._submit is not None:
            await self._submit(message)
            return
        acknowledgement = asyncio.get_running_loop().create_future()
        self._pending.add(acknowledgement)
        try:
            await self._queue.put((message, acknowledgement))
            await acknowledgement
        finally:
            self._pending.discard(acknowledgement)

    async def receive(self) -> AsyncIterator[InboundMessage]:
        """Resume after each yield only when the message has been durably accepted."""
        if self._submit is not None:
            raise FeishuError("Use either submit or receive, not both")
        while not self._closed:
            item = await self._queue.get()
            if item is None:
                if self._source_error:
                    raise FeishuError("Feishu connection stopped")
                return
            message, acknowledgement = item
            try:
                yield message
            except BaseException:
                if not acknowledgement.done():
                    acknowledgement.cancel()
                raise
            else:
                if not acknowledgement.done():
                    acknowledgement.set_result(None)

    async def send(self, message: InboundMessage, result: TaskResult) -> DeliveryReceipt:
        """Confirm all output parts; preserve uncertainty after a partial or ambiguous send."""
        if (
            message.channel != "feishu"
            or message.account_id != self.account_id
            or not message.reply_token
        ):
            return DeliveryReceipt("failed", detail="Feishu reply destination is invalid")
        if not result.text and not result.attachments:
            return DeliveryReceipt("failed", detail="Feishu output is empty")
        if any(len(item.content) > self._limit for item in result.attachments):
            return DeliveryReceipt("failed", detail="Feishu output exceeds the attachment limit")
        sent: list[str] = []
        sending = False
        try:
            parts: list[tuple[str, dict[str, str], str]] = []
            if result.text:
                parts.append(("text", {"text": result.text}, result.text))
            for attachment in result.attachments:
                data = await self._post(
                    "im/v1/files",
                    data={"file_type": "stream", "file_name": _filename(attachment.name)},
                    files={
                        "file": (
                            _filename(attachment.name),
                            attachment.content,
                            attachment.media_type,
                        )
                    },
                )
                identity = (
                    f"{_filename(attachment.name)}:{hashlib.sha256(attachment.content).hexdigest()}"
                )
                parts.append(("file", {"file_key": _required(data, "file_key")}, identity))
            path = f"im/v1/messages/{quote(message.reply_token, safe='')}/reply"
            for index, (kind, content, identity) in enumerate(parts):
                fingerprint = hashlib.sha256(
                    f"{self.account_id}:{message.event_id}:{index}:{kind}:{identity}".encode()
                ).hexdigest()[:40]
                sending = True
                data = await self._post(
                    path,
                    json={
                        "msg_type": kind,
                        "content": json.dumps(content, ensure_ascii=False),
                        "reply_in_thread": bool(message.thread_id),
                        "uuid": fingerprint,
                    },
                )
                message_id = data.get("message_id")
                if not isinstance(message_id, str) or not message_id:
                    raise _AmbiguousResponse("Feishu acceptance receipt has no message identity")
                sent.append(message_id)
                sending = False
        except (httpx.HTTPError, _AmbiguousResponse):
            status = "unknown" if sending or sent else "failed"
            return DeliveryReceipt(status, detail="Feishu delivery could not be confirmed")
        except FeishuError:
            status = "unknown" if sent else "failed"
            return DeliveryReceipt(status, detail="Feishu rejected all or part of the output")
        return DeliveryReceipt(
            "delivered", message_id=sent[-1], detail=f"Accepted {len(sent)} message parts"
        )

    async def start(self) -> None:
        """Start the optional official long-connection SDK in an owned worker process."""
        if self._closed or self._process is not None:
            raise FeishuError("Feishu channel cannot be started in this state")
        import importlib.util

        if importlib.util.find_spec("lark_oapi") is None:
            raise FeishuError("Install the feishu extra to enable long connections")
        context = multiprocessing.get_context("spawn")
        self._connection, child = context.Pipe()
        self._process = context.Process(
            target=_sdk_worker, args=(self._app_id, self._secret, child), daemon=True
        )
        self._process.start()
        child.close()
        self._pump = asyncio.create_task(self._pump_events())

    async def _pump_events(self) -> None:
        try:
            while not self._closed:
                if not await asyncio.to_thread(self._connection.poll, 0.2):
                    if not self._process.is_alive():
                        break
                    continue
                payload = self._connection.recv()
                if "error" in payload:
                    break
                try:
                    await self.ingest(payload["event"])
                except Exception:
                    self._connection.send((payload["ticket"], False))
                else:
                    self._connection.send((payload["ticket"], True))
        except (EOFError, OSError):
            pass
        if not self._closed:
            self._source_error = True
            self._stopped.set()
            await self._queue.put(None)

    async def wait(self) -> None:
        """Wait for listener shutdown and surface a safe connection failure."""
        await self._stopped.wait()
        if self._source_error:
            raise FeishuError("Feishu connection stopped")

    async def aclose(self) -> None:
        """Stop the owned listener and release transport resources."""
        self._closed = True
        self._stopped.set()
        for acknowledgement in self._pending:
            if not acknowledgement.done():
                acknowledgement.cancel()
        if self._pump is not None:
            self._pump.cancel()
            with suppress(asyncio.CancelledError):
                await self._pump
        if self._process is not None:
            if self._process.is_alive():
                self._process.terminate()
            await asyncio.to_thread(self._process.join, 2)
            self._connection.close()
        while not self._queue.empty():
            item = self._queue.get_nowait()
            if item is not None and not item[1].done():
                item[1].cancel()
        self._queue.put_nowait(None)
        self._secret = ""
        self._token = ""
        if self._owns_client:
            await self._client.aclose()
