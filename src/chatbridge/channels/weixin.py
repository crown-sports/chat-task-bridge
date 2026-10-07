"""Personal Weixin transport with explicit acknowledgement and bounded file transfer."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import tempfile
from collections.abc import AsyncIterator, Collection
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from chatbridge.model import Attachment, DeliveryReceipt, InboundMessage, TaskResult

API_BASE = "https://ilinkai.weixin.qq.com"
CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"
PROTOCOL_VERSION = "2.4.8"
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_BATCH_BYTES = 32 * 1024 * 1024


class WeixinError(RuntimeError):
    """Expose a fixed diagnostic code without platform bodies or credential URLs."""

    def __init__(self, code: str, *, ambiguous: bool = False):
        super().__init__(code)
        self.ambiguous = ambiguous


def _safe_url(value: str, host: str, *, base: bool = False) -> str:
    try:
        parsed = urlsplit(value)
        safe = (
            parsed.scheme == "https"
            and parsed.hostname == host
            and parsed.port in (None, 443)
            and not parsed.username
            and not parsed.password
            and not parsed.fragment
        )
        if base:
            safe = safe and parsed.path in ("", "/") and not parsed.query
        if not safe:
            raise ValueError
    except (TypeError, ValueError):
        raise WeixinError("untrusted_endpoint") from None
    return value.rstrip("/") if base else value


def _private_directory(directory: Path) -> None:
    if directory.is_symlink():
        raise WeixinError("unsafe_state_directory")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)


def _save_json(path: Path, value: dict) -> None:
    """Replace state atomically, with permissions fixed before writing secrets."""
    _private_directory(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _load_json(path: Path) -> dict:
    if path.is_symlink():
        raise WeixinError("unsafe_state_file")
    try:
        if path.stat().st_size > 128 * 1024:
            raise ValueError
        value = json.loads(path.read_bytes())
        if not isinstance(value, dict):
            raise ValueError
        path.chmod(0o600)
        return value
    except (OSError, ValueError):
        raise WeixinError("invalid_state_file") from None


@dataclass(frozen=True)
class WeixinCredentials:
    account_id: str
    token: str = field(repr=False)
    api_base: str = API_BASE
    owner_id: str = ""

    def __post_init__(self) -> None:
        _safe_url(self.api_base, "ilinkai.weixin.qq.com", base=True)
        if not all(
            isinstance(value, str) and value for value in (self.account_id, self.token)
        ) or not isinstance(self.owner_id, str):
            raise WeixinError("incomplete_credentials")

    def persist(self, state_dir: Path | str) -> None:
        """Store one account in a private deployment state directory."""
        _save_json(
            Path(state_dir) / "credentials.json",
            {
                "account_id": self.account_id,
                "token": self.token,
                "api_base": self.api_base,
                "owner_id": self.owner_id,
            },
        )

    @classmethod
    def load(cls, state_dir: Path | str) -> WeixinCredentials:
        value = _load_json(Path(state_dir) / "credentials.json")
        try:
            return cls(**value)
        except (TypeError, ValueError):
            raise WeixinError("invalid_credentials_file") from None


class _Transport:
    def __init__(self, client: httpx.AsyncClient | None):
        self.owned = client is None
        self.client = client or httpx.AsyncClient(trust_env=False, timeout=40)

    async def request(
        self, method: str, url: str, *, limit: int = 2 * 1024 * 1024, **kwargs
    ) -> tuple[bytes, httpx.Headers]:
        try:
            async with self.client.stream(
                method,
                url,
                follow_redirects=False,
                timeout=40,
                **kwargs,
            ) as response:
                if response.status_code != 200:
                    raise WeixinError(
                        "http_rejected",
                        ambiguous=response.status_code >= 500,
                    )
                data = bytearray()
                async for chunk in response.aiter_bytes(8192):
                    data.extend(chunk)
                    if len(data) > limit:
                        raise WeixinError("response_too_large", ambiguous=True)
                return bytes(data), response.headers
        except httpx.HTTPError:
            raise WeixinError("transport_interrupted", ambiguous=True) from None

    async def json(self, method: str, url: str, **kwargs) -> dict:
        content, _ = await self.request(method, url, **kwargs)
        try:
            value = json.loads(content)
            if not isinstance(value, dict):
                raise ValueError
        except (ValueError, UnicodeError):
            raise WeixinError("invalid_response", ambiguous=True) from None
        for key in ("ret", "errcode"):
            if key in value:
                if type(value[key]) is not int:
                    raise WeixinError("invalid_status_code", ambiguous=True)
                if value[key] != 0:
                    raise WeixinError("platform_rejected")
        return value

    async def aclose(self) -> None:
        if self.owned:
            await self.client.aclose()


def _headers(token: str = "", *, authenticated: bool = True) -> dict[str, str]:
    result = {"iLink-App-Id": "bot", "iLink-App-ClientVersion": str(0x020408)}
    if authenticated:
        result.update(
            {
                "AuthorizationType": "ilink_bot_token",
                "X-WECHAT-UIN": base64.b64encode(str(secrets.randbits(32)).encode()).decode(),
            }
        )
    if token:
        result["Authorization"] = f"Bearer {token}"
    return result


def _crypt(content: bytes, key: bytes, *, decrypt: bool = False) -> bytes:
    if len(key) != 16:
        raise WeixinError("invalid_media_key")
    try:
        cipher = Cipher(algorithms.AES(key), modes.ECB())
        if decrypt:
            worker = cipher.decryptor()
            padded = worker.update(content) + worker.finalize()
            unpadder = padding.PKCS7(128).unpadder()
            return unpadder.update(padded) + unpadder.finalize()
        padder = padding.PKCS7(128).padder()
        padded = padder.update(content) + padder.finalize()
        worker = cipher.encryptor()
        return worker.update(padded) + worker.finalize()
    except ValueError:
        raise WeixinError("invalid_encrypted_media") from None


def _media_key(value: str) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
        if len(decoded) == 32:
            decoded = bytes.fromhex(decoded.decode("ascii"))
        if len(decoded) != 16:
            raise ValueError
        return decoded
    except (TypeError, ValueError, UnicodeError):
        raise WeixinError("invalid_media_key") from None


def _filename(value: str) -> str:
    name = value.replace("\\", "/").split("/")[-1]
    name = "".join(char for char in name if char.isprintable())[:200]
    return name if name not in ("", ".", "..") else "attachment.bin"


class WeixinChannel:
    """Adapt a single authorized personal account to replayable task messages."""

    def __init__(
        self,
        credentials: WeixinCredentials,
        state_dir: Path | str,
        *,
        allowed_senders: Collection[str] = (),
        client: httpx.AsyncClient | None = None,
        max_attachment_bytes: int = MAX_FILE_BYTES,
        poll_interval: float = 1,
    ):
        if not 0 < max_attachment_bytes <= MAX_FILE_BYTES or poll_interval < 0:
            raise ValueError("invalid_weixin_limits")
        self.credentials = credentials
        self.allowed_senders = frozenset(allowed_senders)
        self.max_attachment_bytes = max_attachment_bytes
        self.poll_interval = poll_interval
        self._http = _Transport(client)
        account_hash = hashlib.sha256(credentials.account_id.encode()).hexdigest()[:24]
        self._cursor_path = Path(state_dir) / f"cursor-{account_hash}.json"
        _private_directory(self._cursor_path.parent)
        self._cursor = ""
        if self._cursor_path.exists():
            self._cursor = _load_json(self._cursor_path).get("cursor", "")
            if not isinstance(self._cursor, str):
                raise WeixinError("invalid_cursor")
        self._closed = False
        self._receiving = False

    async def _post(self, operation: str, payload: dict) -> dict:
        return await self._http.json(
            "POST",
            f"{self.credentials.api_base.rstrip('/')}/ilink/bot/{operation}",
            headers=_headers(self.credentials.token),
            json={
                **payload,
                "base_info": {
                    "channel_version": PROTOCOL_VERSION,
                    "bot_agent": "ChatTaskBridge/0.1.0",
                },
            },
        )

    async def _updates(self) -> dict:
        """Retry read-only polling failures without advancing the persisted cursor."""
        for attempt in range(4):
            try:
                return await self._post("getupdates", {"get_updates_buf": self._cursor})
            except WeixinError as error:
                retryable = str(error) == "transport_interrupted" or (
                    str(error) == "http_rejected" and error.ambiguous
                )
                if not retryable or attempt == 3:
                    raise
                await asyncio.sleep(2**attempt)
        raise WeixinError("poll_retries_exhausted")

    async def receive(self) -> AsyncIterator[InboundMessage]:
        """Commit each batch cursor only after its yielded events are durably accepted."""
        if self._receiving:
            raise WeixinError("receiver_already_active")
        self._receiving = True
        try:
            while not self._closed:
                batch = await self._updates()
                messages, cursor = batch.get("msgs", []), batch.get("get_updates_buf", "")
                if not isinstance(messages, list) or not isinstance(cursor, str):
                    raise WeixinError("invalid_update_batch")
                for raw in messages:
                    message = await self._decode(raw)
                    if message is not None:
                        yield message
                if cursor:
                    _save_json(self._cursor_path, {"cursor": cursor})
                    self._cursor = cursor
                if not self._closed:
                    await asyncio.sleep(self.poll_interval)
        finally:
            self._receiving = False

    async def _decode(self, raw: dict) -> InboundMessage | None:
        if not isinstance(raw, dict):
            raise WeixinError("invalid_message")
        sender = raw.get("from_user_id", "")
        if (
            not isinstance(sender, str)
            or sender not in self.allowed_senders
            or type(raw.get("message_type")) is not int
            or raw.get("message_type") != 1
            or raw.get("group_id")
        ):
            return None
        event_id = raw.get("message_id")
        items = raw.get("item_list", [])
        if type(event_id) not in (str, int) or not str(event_id) or not isinstance(items, list):
            raise WeixinError("invalid_message")
        token = raw.get("context_token", "")
        if not isinstance(token, str):
            raise WeixinError("invalid_reply_context")
        unsupported = []
        media_fields = {2: "image_item", 3: "voice_item", 5: "video_item"}
        for item in items:
            if not isinstance(item, dict) or type(item.get("type")) is not int:
                raise WeixinError("invalid_message_item")
            kind = item["type"]
            if kind in media_fields:
                if not isinstance(item.get(media_fields[kind]), dict):
                    raise WeixinError("invalid_message_item")
                unsupported.append(media_fields[kind].removesuffix("_item"))
            elif kind not in (1, 4):
                raise WeixinError("unsupported_message_item")
        if unsupported:
            return InboundMessage(
                str(event_id),
                "weixin",
                self.credentials.account_id,
                sender,
                sender,
                "/unsupported " + ",".join(sorted(set(unsupported))),
                reply_token=token,
            )
        text, attachments = [], []
        for item in items:
            if not isinstance(item, dict):
                raise WeixinError("invalid_message_item")
            if item.get("type") == 1:
                text_item = item.get("text_item")
                if not isinstance(text_item, dict):
                    raise WeixinError("invalid_text_item")
                content = text_item.get("text", "")
                if not isinstance(content, str):
                    raise WeixinError("invalid_text_item")
                text.append(content)
            elif item.get("type") == 4:
                attachments.append(await self._download(item.get("file_item", {})))
                if sum(len(file.content) for file in attachments) > MAX_BATCH_BYTES:
                    raise WeixinError("attachment_batch_too_large")
            else:
                raise WeixinError("unsupported_message_item")
        if not text and not attachments:
            return None
        return InboundMessage(
            str(event_id),
            "weixin",
            self.credentials.account_id,
            sender,
            sender,
            "\n".join(text),
            tuple(attachments),
            token,
        )

    async def _download(self, item: dict) -> Attachment:
        if not isinstance(item, dict) or not isinstance(item.get("media"), dict):
            raise WeixinError("invalid_file_item")
        media = item["media"]
        key = _media_key(media.get("aes_key", ""))
        full_url, query = media.get("full_url"), media.get("encrypt_query_param")
        if not full_url and not query:
            raise WeixinError("missing_media_reference")
        url = full_url or f"{CDN_BASE}/download?{urlencode({'encrypted_query_param': query})}"
        _safe_url(url, "novac2c.cdn.weixin.qq.com")
        if media.get("encrypt_type", 1) != 1:
            raise WeixinError("unsupported_media_encryption")
        ciphertext, _ = await self._http.request("GET", url, limit=self.max_attachment_bytes + 16)
        content = _crypt(ciphertext, key, decrypt=True)
        if len(content) > self.max_attachment_bytes:
            raise WeixinError("attachment_too_large")
        if "len" in item and str(item["len"]) != str(len(content)):
            raise WeixinError("attachment_length_mismatch")
        return Attachment(_filename(str(item.get("file_name", "attachment.bin"))), content)

    async def _upload(self, recipient: str, attachment: Attachment) -> dict:
        key, file_key = secrets.token_bytes(16), secrets.token_hex(16)
        ciphertext = _crypt(attachment.content, key)
        response = await self._post(
            "getuploadurl",
            {
                "filekey": file_key,
                "media_type": 3,
                "to_user_id": recipient,
                "rawsize": len(attachment.content),
                "rawfilemd5": hashlib.md5(attachment.content, usedforsecurity=False).hexdigest(),
                "filesize": len(ciphertext),
                "no_need_thumb": True,
                "aeskey": key.hex(),
            },
        )
        full_url, query = response.get("upload_full_url"), response.get("upload_param")
        if not full_url and not query:
            raise WeixinError("missing_upload_reference")
        url = (
            full_url
            or f"{CDN_BASE}/upload?{urlencode({'encrypted_query_param': query, 'filekey': file_key})}"
        )
        _safe_url(url, "novac2c.cdn.weixin.qq.com")
        _, headers = await self._http.request(
            "POST",
            url,
            content=ciphertext,
            headers={"Content-Type": "application/octet-stream"},
        )
        download_param = headers.get("x-encrypted-param")
        if not download_param:
            raise WeixinError("missing_upload_receipt")
        return {
            "type": 4,
            "file_item": {
                "file_name": _filename(attachment.name),
                "len": str(len(attachment.content)),
                "media": {
                    "encrypt_query_param": download_param,
                    "aes_key": base64.b64encode(key.hex().encode()).decode(),
                    "encrypt_type": 1,
                },
            },
        }

    async def send(self, message: InboundMessage, result: TaskResult) -> DeliveryReceipt:
        """Never retry an ambiguous send or reuse another message's reply context."""
        if (
            message.channel != "weixin"
            or message.account_id != self.credentials.account_id
            or message.sender_id not in self.allowed_senders
            or message.conversation_id != message.sender_id
            or not message.reply_token
        ):
            return DeliveryReceipt("failed", detail="invalid_reply_scope")
        if (
            any(len(file.content) > self.max_attachment_bytes for file in result.attachments)
            or sum(len(file.content) for file in result.attachments) > MAX_BATCH_BYTES
        ):
            return DeliveryReceipt("failed", detail="attachment_too_large")
        sent = 0
        attempted = False
        client_id = ""
        try:
            items = [{"type": 1, "text_item": {"text": result.text}}] if result.text else []
            for attachment in result.attachments:
                items.append(await self._upload(message.sender_id, attachment))
            if not items:
                return DeliveryReceipt("failed", detail="empty_result")
            for item in items:
                client_id = f"ctb-{secrets.token_hex(16)}"
                attempted = True
                response = await self._post(
                    "sendmessage",
                    {
                        "msg": {
                            "from_user_id": "",
                            "to_user_id": message.sender_id,
                            "client_id": client_id,
                            "message_type": 2,
                            "message_state": 2,
                            "context_token": message.reply_token,
                            "item_list": [item],
                        }
                    },
                )
                if type(response.get("ret")) is not int or response["ret"] != 0:
                    raise WeixinError("missing_delivery_confirmation", ambiguous=True)
                sent += 1
            return DeliveryReceipt("delivered", message_id=client_id)
        except WeixinError as error:
            status = "unknown" if sent or (attempted and error.ambiguous) else "failed"
            return DeliveryReceipt(status, message_id=client_id, detail=str(error))

    async def aclose(self) -> None:
        self._closed = True
        await self._http.aclose()


@dataclass(frozen=True)
class QrChallenge:
    qrcode: str = field(repr=False)
    display_content: str = field(repr=False)


@dataclass(frozen=True)
class LoginStatus:
    status: str
    credentials: WeixinCredentials | None = field(default=None, repr=False)


class WeixinLogin:
    """Expose QR steps without printing tokens or authorizing an account implicitly."""

    def __init__(self, state_dir: Path | str, *, client: httpx.AsyncClient | None = None):
        self.state_dir = Path(state_dir)
        self._http = _Transport(client)

    async def start(self) -> QrChallenge:
        value = await self._http.json(
            "POST",
            f"{API_BASE}/ilink/bot/get_bot_qrcode?bot_type=3",
            json={"local_token_list": []},
            headers=_headers(),
        )
        if not all(
            isinstance(value.get(key), str) and value[key]
            for key in ("qrcode", "qrcode_img_content")
        ):
            raise WeixinError("incomplete_qr_challenge")
        return QrChallenge(value["qrcode"], value["qrcode_img_content"])

    async def poll(self, challenge: QrChallenge, verification_code: str = "") -> LoginStatus:
        query = {"qrcode": challenge.qrcode}
        if verification_code:
            query["verify_code"] = verification_code
        value = await self._http.json(
            "GET",
            f"{API_BASE}/ilink/bot/get_qrcode_status",
            params=query,
            headers=_headers(authenticated=False),
        )
        status = value.get("status")
        if status == "scaned_but_redirect":
            _safe_url(
                f"https://{value.get('redirect_host', '')}", "ilinkai.weixin.qq.com", base=True
            )
            return LoginStatus("wait")
        if status == "confirmed":
            token, account = value.get("bot_token"), value.get("ilink_bot_id")
            if not isinstance(token, str) or not isinstance(account, str):
                raise WeixinError("incomplete_login_confirmation")
            credentials = WeixinCredentials(
                account,
                token,
                value.get("baseurl") or API_BASE,
                value.get("ilink_user_id", ""),
            )
            credentials.persist(self.state_dir)
            return LoginStatus("confirmed", credentials)
        if status not in {
            "wait",
            "scaned",
            "expired",
            "need_verifycode",
            "verify_code_blocked",
            "binded_redirect",
        }:
            raise WeixinError("unknown_login_status")
        return LoginStatus(status)

    async def aclose(self) -> None:
        await self._http.aclose()
