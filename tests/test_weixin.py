"""Offline protocol contracts; every HTTP call uses synthetic payloads."""

import asyncio
import base64
import json
import stat
from dataclasses import replace

import httpx
import pytest

from chatbridge.channels.weixin import (
    API_BASE,
    CDN_BASE,
    QrChallenge,
    WeixinChannel,
    WeixinCredentials,
    WeixinError,
    WeixinLogin,
    _crypt,
)
from chatbridge.model import Attachment, InboundMessage, TaskResult


@pytest.fixture
def credentials():
    return WeixinCredentials("test-account", "test-only-not-a-valid-token")


@pytest.fixture
def inbound(credentials):
    return InboundMessage(
        "event-1",
        "weixin",
        credentials.account_id,
        "alice",
        "alice",
        "process",
        reply_token="test-reply-context",
    )


def wire_message(event="event-1", *, sender="alice", text="process", items=None):
    return {
        "message_id": event,
        "from_user_id": sender,
        "message_type": 1,
        "context_token": "test-reply-context",
        "item_list": items if items is not None else [{"type": 1, "text_item": {"text": text}}],
    }


def batch(messages, cursor="cursor-1"):
    return httpx.Response(200, json={"ret": 0, "msgs": messages, "get_updates_buf": cursor})


async def test_unaccepted_batch_replays_after_restart(tmp_path, credentials):
    cursors = []

    def transport(request):
        cursors.append(json.loads(request.content)["get_updates_buf"])
        return batch([wire_message()])

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        first = WeixinChannel(credentials, tmp_path, allowed_senders={"alice"}, client=client)
        stream = first.receive()
        assert (await anext(stream)).event_id == "event-1"
        await stream.aclose()
        assert not list(tmp_path.glob("cursor-*.json"))
        second = WeixinChannel(credentials, tmp_path, allowed_senders={"alice"}, client=client)
        stream = second.receive()
        assert (await anext(stream)).event_id == "event-1"
        await stream.aclose()
        assert cursors == ["", ""]


async def test_cursor_waits_for_every_message_in_batch(tmp_path, credentials):
    cursors = []

    def transport(request):
        cursors.append(json.loads(request.content)["get_updates_buf"])
        if len(cursors) == 1:
            return batch([wire_message("one"), wire_message("two")], "accepted-batch")
        return batch([wire_message("three")], "next-batch")

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        channel = WeixinChannel(
            credentials, tmp_path, allowed_senders={"alice"}, client=client, poll_interval=0
        )
        stream = channel.receive()
        assert (await anext(stream)).event_id == "one"
        assert (await anext(stream)).event_id == "two"
        assert not list(tmp_path.glob("cursor-*.json"))
        assert (await anext(stream)).event_id == "three"
        cursor_path = next(tmp_path.glob("cursor-*.json"))
        assert json.loads(cursor_path.read_text())["cursor"] == "accepted-batch"
        assert stat.S_IMODE(cursor_path.stat().st_mode) == 0o600
        assert cursors == ["", "accepted-batch"]
        await stream.aclose()


async def test_default_deny_all_and_groups_never_become_tasks(tmp_path, credentials):
    calls = 0

    def transport(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return batch([wire_message()], "ignored")
        raise RuntimeError("end-of-fixture")

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        channel = WeixinChannel(credentials, tmp_path, client=client, poll_interval=0)
        with pytest.raises(RuntimeError, match="end-of-fixture"):
            await anext(channel.receive())
        cursor_path = next(tmp_path.glob("cursor-*.json"))
        assert json.loads(cursor_path.read_text())["cursor"] == "ignored"
        channel.allowed_senders = frozenset({"alice"})
        assert await channel._decode({**wire_message(), "group_id": "group-1"}) is None


async def test_file_only_message_decrypts_and_normalizes_name(tmp_path, credentials):
    key, content = bytes(range(16)), b"name,amount\nA,8\n"
    item = {
        "type": 4,
        "file_item": {
            "file_name": "../../orders.csv",
            "len": str(len(content)),
            "media": {
                "aes_key": base64.b64encode(key).decode(),
                "encrypt_query_param": "test-download-reference",
                "encrypt_type": 1,
            },
        },
    }
    calls = []

    def transport(request):
        calls.append(request)
        if request.url.host == "ilinkai.weixin.qq.com":
            return batch([wire_message(items=[item])])
        assert request.url.host == "novac2c.cdn.weixin.qq.com"
        assert "authorization" not in request.headers
        return httpx.Response(200, content=_crypt(content, key))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        channel = WeixinChannel(credentials, tmp_path, allowed_senders={"alice"}, client=client)
        stream = channel.receive()
        message = await anext(stream)
        assert message.text == ""
        assert message.attachments == (Attachment("orders.csv", content),)
        assert message.reply_token == "test-reply-context"
        assert len(calls) == 2
        await stream.aclose()


@pytest.mark.parametrize(
    "url",
    [
        "http://novac2c.cdn.weixin.qq.com/x",
        "https://evil.test/x",
        "https://novac2c.cdn.weixin.qq.com.evil.test/x",
        "https://user@novac2c.cdn.weixin.qq.com/x",
        "https://novac2c.cdn.weixin.qq.com:444/x",
    ],
)
async def test_media_urls_rejected_before_network(tmp_path, credentials, url):
    def transport(request):
        raise AssertionError("untrusted URL reached transport")

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        channel = WeixinChannel(credentials, tmp_path, client=client)
        with pytest.raises(WeixinError, match="untrusted_endpoint"):
            await channel._download(
                {"media": {"full_url": url, "aes_key": base64.b64encode(bytes(16)).decode()}}
            )


@pytest.mark.parametrize("mode", ["oversize", "length_mismatch", "invalid_padding", "redirect"])
async def test_invalid_media_does_not_yield_or_advance_cursor(tmp_path, credentials, mode):
    key = bytes(16)
    item = {
        "type": 4,
        "file_item": {
            "file_name": "input.csv",
            "len": "3",
            "media": {
                "full_url": f"{CDN_BASE}/download?opaque=fixture",
                "aes_key": base64.b64encode(key).decode(),
            },
        },
    }

    def transport(request):
        if request.url.host == "ilinkai.weixin.qq.com":
            return batch([wire_message(items=[item])])
        if mode == "redirect":
            return httpx.Response(302, headers={"location": "https://evil.test/private"})
        if mode == "invalid_padding":
            return httpx.Response(200, content=b"not an AES block")
        return httpx.Response(200, content=_crypt(b"12345", key))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), follow_redirects=True
    ) as client:
        channel = WeixinChannel(
            credentials,
            tmp_path,
            allowed_senders={"alice"},
            client=client,
            max_attachment_bytes=4 if mode == "oversize" else 8,
        )
        with pytest.raises(WeixinError):
            await anext(channel.receive())
        assert not list(tmp_path.glob("cursor-*.json"))


def test_aes_matches_nist_first_block_and_accepts_empty_file():
    key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    plaintext = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
    encrypted = _crypt(plaintext, key)
    assert encrypted[:16].hex() == "3ad77bb40d7a3660a89ecaf32466ef97"
    assert _crypt(encrypted, key, decrypt=True) == plaintext
    assert _crypt(_crypt(b"", key), key, decrypt=True) == b""


async def test_send_uses_original_reply_token_and_encrypts_file(tmp_path, credentials, inbound):
    messages, uploads = [], []
    upload_key = None

    def transport(request):
        nonlocal upload_key
        if request.url.path.endswith("getuploadurl"):
            value = json.loads(request.content)
            upload_key = bytes.fromhex(value["aeskey"])
            assert value["media_type"] == 3
            assert value["rawsize"] == 3
            assert value["filesize"] == 16
            return httpx.Response(200, json={"upload_param": "test-upload-reference"})
        if request.url.host == "novac2c.cdn.weixin.qq.com":
            assert "authorization" not in request.headers
            assert _crypt(request.content, upload_key, decrypt=True) == b"csv"
            uploads.append(request.content)
            return httpx.Response(200, headers={"x-encrypted-param": "test-download-reference"})
        messages.append(json.loads(request.content)["msg"])
        return httpx.Response(200, json={"ret": 0})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        channel = WeixinChannel(credentials, tmp_path, allowed_senders={"alice"}, client=client)
        receipt = await channel.send(
            inbound, TaskResult("done", (Attachment("result.csv", b"csv"),))
        )
        assert receipt.status == "delivered"
        assert len(messages) == 2 and len(uploads) == 1
        assert all(message["context_token"] == inbound.reply_token for message in messages)
        assert all(message["to_user_id"] == "alice" for message in messages)
        media = messages[1]["item_list"][0]["file_item"]["media"]
        assert base64.b64decode(media["aes_key"]).decode() == upload_key.hex()
        assert receipt.message_id == messages[-1]["client_id"]
        assert messages[0]["client_id"] != messages[1]["client_id"]


@pytest.mark.parametrize(
    ("response", "status"),
    [
        (httpx.Response(200, json={"ret": -1, "errmsg": "test-server-secret"}), "failed"),
        (httpx.Response(200, json={}), "unknown"),
        (httpx.Response(200, content=b"truncated"), "unknown"),
        (httpx.Response(503, text="test-server-secret"), "unknown"),
        (httpx.Response(401, text="test-server-secret"), "failed"),
    ],
)
async def test_delivery_requires_explicit_confirmation(
    tmp_path, credentials, inbound, response, status
):
    requests = []

    def transport(request):
        requests.append(request)
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        channel = WeixinChannel(credentials, tmp_path, allowed_senders={"alice"}, client=client)
        receipt = await channel.send(inbound, TaskResult("done"))
        assert receipt.status == status
        assert "test-server-secret" not in repr(receipt)
        assert len(requests) == 1


async def test_timeout_is_unknown_without_blind_retry(tmp_path, credentials, inbound):
    count = 0

    def transport(request):
        nonlocal count
        count += 1
        raise httpx.ReadTimeout("test-transport-secret", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        channel = WeixinChannel(credentials, tmp_path, allowed_senders={"alice"}, client=client)
        receipt = await channel.send(inbound, TaskResult("done"))
        assert receipt.status == "unknown"
        assert count == 1
        assert "test-transport-secret" not in repr(receipt)


async def test_partial_multi_message_delivery_is_unknown(tmp_path, credentials, inbound):
    count = 0

    def transport(request):
        nonlocal count
        if request.url.path.endswith("getuploadurl"):
            return httpx.Response(200, json={"upload_param": "test-upload"})
        if request.url.host == "novac2c.cdn.weixin.qq.com":
            return httpx.Response(200, headers={"x-encrypted-param": "test-download"})
        count += 1
        return httpx.Response(200, json={"ret": 0 if count == 1 else -1})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        channel = WeixinChannel(credentials, tmp_path, allowed_senders={"alice"}, client=client)
        receipt = await channel.send(inbound, TaskResult("done", (Attachment("file.csv", b"a"),)))
        assert receipt.status == "unknown"
        assert count == 2


@pytest.mark.parametrize(
    "change",
    [
        {"reply_token": ""},
        {"account_id": "another"},
        {"sender_id": "bob"},
        {"conversation_id": "other"},
        {"channel": "feishu"},
    ],
)
async def test_reply_scope_checked_before_network(tmp_path, credentials, inbound, change):
    def transport(request):
        raise AssertionError("invalid scope reached transport")

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        channel = WeixinChannel(credentials, tmp_path, allowed_senders={"alice"}, client=client)
        assert (
            await channel.send(replace(inbound, **change), TaskResult("done"))
        ).status == "failed"


def test_credentials_private_atomic_and_hidden_in_repr(tmp_path, credentials):
    credentials.persist(tmp_path)
    assert credentials.token not in repr(credentials)
    assert WeixinCredentials.load(tmp_path) == credentials
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "credentials.json").stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".state-*"))
    credentials.persist(tmp_path)
    assert WeixinCredentials.load(tmp_path) == credentials


def test_symlink_state_is_rejected(tmp_path, credentials):
    real = tmp_path / "actual"
    real.mkdir()
    shortcut = tmp_path / "shortcut"
    shortcut.symlink_to(real, target_is_directory=True)
    with pytest.raises(WeixinError, match="unsafe_state_directory"):
        credentials.persist(shortcut)


async def test_qr_login_is_explicit_persists_only_confirmation(tmp_path):
    count = 0

    def transport(request):
        nonlocal count
        count += 1
        assert "authorization" not in request.headers
        if request.url.path.endswith("get_bot_qrcode"):
            assert request.method == "POST"
            assert json.loads(request.content) == {"local_token_list": []}
            return httpx.Response(
                200,
                json={
                    "qrcode": "test-challenge-private",
                    "qrcode_img_content": "test-display-private",
                },
            )
        assert "authorizationtype" not in request.headers
        assert request.url.params["qrcode"] == "test-challenge-private"
        if count == 2:
            return httpx.Response(200, json={"status": "need_verifycode"})
        assert request.url.params["verify_code"] == "123456"
        return httpx.Response(
            200,
            json={
                "status": "confirmed",
                "bot_token": "test-login-private",
                "ilink_bot_id": "test-account",
                "baseurl": API_BASE,
                "ilink_user_id": "alice",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        login = WeixinLogin(tmp_path, client=client)
        challenge = await login.start()
        assert "private" not in repr(challenge)
        assert (await login.poll(challenge)).status == "need_verifycode"
        assert not (tmp_path / "credentials.json").exists()
        result = await login.poll(challenge, "123456")
        assert result.status == "confirmed"
        assert "private" not in repr(result)
        assert WeixinCredentials.load(tmp_path).owner_id == "alice"
        await login.aclose()
        assert not client.is_closed


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "scaned_but_redirect", "redirect_host": "evil.test"},
        {
            "status": "confirmed",
            "bot_token": "test-private",
            "ilink_bot_id": "test-account",
            "baseurl": "https://evil.test",
        },
        {"status": "confirmed", "ilink_bot_id": "test-account"},
    ],
)
async def test_qr_unsafe_or_incomplete_confirmation_cannot_store_tokens(tmp_path, payload):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload),
        )
    ) as client:
        login = WeixinLogin(tmp_path, client=client)
        with pytest.raises(WeixinError) as error:
            await login.poll(QrChallenge("fixture", "fixture"))
        assert "test-private" not in str(error.value)
        assert not (tmp_path / "credentials.json").exists()


async def test_second_receiver_rejected_and_cancellation_preserves_cursor(tmp_path, credentials):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: batch([wire_message()]),
        )
    ) as client:
        channel = WeixinChannel(credentials, tmp_path, allowed_senders={"alice"}, client=client)
        stream = channel.receive()
        await anext(stream)
        with pytest.raises(WeixinError, match="receiver_already_active"):
            await anext(channel.receive())
        with pytest.raises(asyncio.CancelledError):
            await stream.athrow(asyncio.CancelledError())
        assert not list(tmp_path.glob("cursor-*.json"))


async def test_oversize_reply_is_rejected_before_upload(tmp_path, credentials, inbound):
    def transport(request):
        raise AssertionError("oversize reply reached transport")

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        channel = WeixinChannel(
            credentials, tmp_path, allowed_senders={"alice"}, client=client, max_attachment_bytes=4
        )
        result = TaskResult("done", (Attachment("too-large.csv", b"12345"),))
        assert (await channel.send(inbound, result)).status == "failed"


async def test_upload_failure_before_any_message_is_failed_not_partial(
    tmp_path, credentials, inbound
):
    def transport(request):
        assert request.url.path.endswith("getuploadurl")
        raise httpx.ReadTimeout("fixture", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        channel = WeixinChannel(credentials, tmp_path, allowed_senders={"alice"}, client=client)
        result = TaskResult("done", (Attachment("input.csv", b"fixture"),))
        receipt = await channel.send(inbound, result)
        assert receipt.status == "failed"
        assert receipt.message_id == ""


async def test_invalid_later_event_preserves_entire_batch(tmp_path, credentials):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: batch(
                [wire_message(), wire_message(items=[{"type": 1, "text_item": None}])]
            ),
        )
    ) as client:
        channel = WeixinChannel(credentials, tmp_path, allowed_senders={"alice"}, client=client)
        stream = channel.receive()
        assert (await anext(stream)).event_id == "event-1"
        with pytest.raises(WeixinError, match="invalid_text_item"):
            await anext(stream)
        assert not list(tmp_path.glob("cursor-*.json"))


async def test_hex_encoded_media_key_is_accepted(tmp_path, credentials):
    key = bytes(range(16))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=_crypt(b"input", key)),
        )
    ) as client:
        channel = WeixinChannel(credentials, tmp_path, client=client)
        attachment = await channel._download(
            {
                "file_name": "data.csv",
                "len": "5",
                "media": {
                    "aes_key": base64.b64encode(key.hex().encode()).decode(),
                    "encrypt_query_param": "test-reference",
                },
            }
        )
        assert attachment.content == b"input"


@pytest.mark.parametrize("kind,field", [(2, "image_item"), (3, "voice_item"), (5, "video_item")])
async def test_known_unsupported_media_is_recorded_without_poisoning_cursor(
    tmp_path, credentials, kind, field
):
    from chatbridge.ledger import Ledger

    requests = []

    def transport(request):
        assert request.url.path.endswith("getupdates")
        requests.append(json.loads(request.content)["get_updates_buf"])
        if len(requests) == 1:
            return batch([wire_message("media", items=[{"type": kind, field: {}}])], "after-media")
        return batch([wire_message("next", text="/status")], "after-next")

    ledger = Ledger(tmp_path / "ledger")
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            channel = WeixinChannel(
                credentials,
                tmp_path / "channel",
                allowed_senders={"alice"},
                client=client,
                poll_interval=0,
            )
            stream = channel.receive()
            message = await anext(stream)
            assert message.text.startswith("/unsupported ")
            assert message.reply_token == "test-reply-context"
            assert not message.attachments
            job_id = ledger.accept(message)
            assert ledger.get(job_id)["state"] == "ready"
            assert (await anext(stream)).event_id == "next"
            assert requests == ["", "after-media"]
            await stream.aclose()
    finally:
        ledger.close()


@pytest.mark.parametrize("item", [{"type": 99}, {"type": True}, {"type": 2, "image_item": None}])
async def test_unknown_or_malformed_media_keeps_cursor_uncommitted(tmp_path, credentials, item):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: batch([wire_message(items=[item])]))
    ) as client:
        channel = WeixinChannel(credentials, tmp_path, allowed_senders={"alice"}, client=client)
        with pytest.raises(WeixinError):
            await anext(channel.receive())
        assert not list(tmp_path.glob("cursor-*.json"))


@pytest.mark.parametrize("ret", [False, True, 0.0, "0", None])
async def test_delivery_status_must_be_an_integer(tmp_path, credentials, inbound, ret):
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(200, json={"ret": ret})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        channel = WeixinChannel(credentials, tmp_path, allowed_senders={"alice"}, client=client)
        assert (await channel.send(inbound, TaskResult("done"))).status == "unknown"
        assert len(requests) == 1


@pytest.mark.parametrize("failure", ["timeout", "disconnect", "server-error"])
async def test_polling_transient_failures_retry_same_cursor(tmp_path, credentials, failure):
    from unittest.mock import AsyncMock, patch

    cursors = []

    def transport(request):
        cursors.append(json.loads(request.content)["get_updates_buf"])
        if len(cursors) < 3:
            if failure == "timeout":
                raise httpx.ReadTimeout("fixture", request=request)
            if failure == "disconnect":
                raise httpx.RemoteProtocolError("fixture", request=request)
            return httpx.Response(503)
        return batch([wire_message()])

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        channel = WeixinChannel(credentials, tmp_path, allowed_senders={"alice"}, client=client)
        with patch("chatbridge.channels.weixin.asyncio.sleep", new_callable=AsyncMock) as sleep:
            stream = channel.receive()
            assert (await anext(stream)).event_id == "event-1"
            assert [call.args[0] for call in sleep.await_args_list] == [1, 2]
            assert cursors == ["", "", ""]
            await stream.aclose()


async def test_polling_retry_exhaustion_preserves_cursor(tmp_path, credentials):
    from unittest.mock import AsyncMock, patch

    requests = []

    def transport(request):
        requests.append(request)
        raise httpx.ConnectError("fixture", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        channel = WeixinChannel(credentials, tmp_path, allowed_senders={"alice"}, client=client)
        with patch("chatbridge.channels.weixin.asyncio.sleep", new_callable=AsyncMock) as sleep:
            with pytest.raises(WeixinError, match="transport_interrupted"):
                await anext(channel.receive())
            assert len(requests) == 4
            assert [call.args[0] for call in sleep.await_args_list] == [1, 2, 4]
            assert not list(tmp_path.glob("cursor-*.json"))


async def test_authentication_rejection_does_not_enter_poll_retry_loop(tmp_path, credentials):
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(401)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        channel = WeixinChannel(credentials, tmp_path, allowed_senders={"alice"}, client=client)
        with pytest.raises(WeixinError, match="http_rejected"):
            await anext(channel.receive())
        assert len(requests) == 1
