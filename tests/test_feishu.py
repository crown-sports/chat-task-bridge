"""Protocol fixtures exercise Feishu without contacting a platform account."""

import asyncio
import json
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import httpx

from chatbridge.channels.feishu import FeishuChannel, FeishuError
from chatbridge.model import Attachment, InboundMessage, TaskResult


def event(kind="text", content=None, **message_fields):
    return {
        "schema": "2.0",
        "header": {
            "event_type": "im.message.receive_v1",
            "event_id": "event-1",
            "app_id": "app-test",
        },
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": "sender-1"}},
            "message": {
                "message_id": "message-1",
                "chat_id": "chat-1",
                "chat_type": "group",
                "message_type": kind,
                "content": json.dumps(content or {"text": "clean"}),
                **message_fields,
            },
        },
    }


def reply_source():
    return InboundMessage(
        "event-1",
        "feishu",
        "app-test",
        "chat-1",
        "sender-1",
        "clean",
        reply_token="original-message",
        thread_id="thread-1",
    )


class FeishuTests(IsolatedAsyncioTestCase):
    def channel(self, handler=None, **kwargs):
        handler = handler or (lambda request: httpx.Response(500))
        client = httpx.AsyncClient(
            base_url="https://open.feishu.cn/open-apis/", transport=httpx.MockTransport(handler)
        )
        self.addAsyncCleanup(client.aclose)
        channel = FeishuChannel("app-test", "fixture-secret", client=client, **kwargs)
        self.addAsyncCleanup(channel.aclose)
        return channel

    async def test_default_deny_and_thread_identity(self):
        self.assertIsNone(await self.channel().parse_event(event()))
        channel = self.channel(allowed_senders=frozenset({"sender-1"}))
        message = await channel.parse_event(event(thread_id="thread-1"))
        self.assertEqual(message.thread_id, "thread-1")
        self.assertEqual(message.conversation_id, "chat-1")
        self.assertEqual(message.sender_id, "sender-1")
        self.assertEqual(message.reply_token, "message-1")
        self.assertEqual(message.event_id, "event-1")
        self.assertNotIn("fixture-secret", repr(channel))

    async def test_wrong_app_and_malformed_event_are_rejected(self):
        channel = self.channel(allowed_senders=frozenset({"sender-1"}))
        payload = event()
        payload["header"]["app_id"] = "other-app"
        with self.assertRaises(FeishuError):
            await channel.parse_event(payload)
        with self.assertRaises(FeishuError):
            await channel.parse_event({"event": {}})
        payload = event()
        payload["event"]["message"]["content"] = "[]"
        with self.assertRaises(FeishuError):
            await channel.parse_event(payload)

    async def test_ingress_waits_for_durable_submit(self):
        entered, committed = asyncio.Event(), asyncio.Event()
        accepted = []

        async def submit(message):
            entered.set()
            await committed.wait()
            accepted.append(message)

        channel = self.channel(allowed_senders=frozenset({"sender-1"}), submit=submit)
        task = asyncio.create_task(channel.ingest(event()))
        await entered.wait()
        self.assertFalse(task.done())
        committed.set()
        await task
        self.assertEqual(len(accepted), 1)

    async def test_ingress_acknowledges_only_after_iterator_resume(self):
        channel = self.channel(allowed_senders=frozenset({"sender-1"}))
        intake = asyncio.create_task(channel.ingest(event()))
        iterator = channel.receive()
        message = await anext(iterator)
        self.assertEqual(message.event_id, "event-1")
        self.assertFalse(intake.done())
        next_message = asyncio.create_task(anext(iterator))
        await asyncio.wait_for(intake, 1)
        next_message.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await next_message
        await iterator.aclose()

    async def test_failed_submit_is_not_acknowledged(self):
        async def submit(message):
            raise OSError("fixture storage failure")

        channel = self.channel(allowed_senders=frozenset({"sender-1"}), submit=submit)
        with self.assertRaises(OSError):
            await channel.ingest(event())

    async def test_token_cache_and_bounded_sanitized_attachment(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.path.endswith("/internal"):
                self.assertEqual(json.loads(request.content)["app_id"], "app-test")
                return httpx.Response(
                    200, json={"code": 0, "tenant_access_token": "fixture-token", "expire": 7200}
                )
            self.assertEqual(request.headers["authorization"], "Bearer fixture-token")
            self.assertEqual(request.url.params["type"], "file")
            return httpx.Response(200, content=b"a,b\n1,2\n", headers={"content-type": "text/csv"})

        channel = self.channel(
            handler, allowed_senders=frozenset({"sender-1"}), max_attachment_bytes=8
        )
        payload = event("file", {"file_key": "file-1", "file_name": "../../nested\\orders.csv"})
        for _ in range(2):
            message = await channel.parse_event(payload)
            self.assertEqual(message.attachments[0].name, "orders.csv")
            self.assertEqual(message.attachments[0].content, b"a,b\n1,2\n")
        self.assertEqual(sum(request.url.path.endswith("/internal") for request in requests), 1)

    async def test_attachment_limit_and_redirect_are_rejected(self):
        for response in (
            httpx.Response(200, content=b"12345"),
            httpx.Response(302, headers={"location": "https://example.invalid/file"}),
        ):

            def handler(request):
                if request.url.path.endswith("/internal"):
                    return httpx.Response(
                        200,
                        json={"code": 0, "tenant_access_token": "fixture-token", "expire": 7200},
                    )
                return response

            channel = self.channel(
                handler, allowed_senders=frozenset({"sender-1"}), max_attachment_bytes=4
            )
            with self.assertRaises(FeishuError):
                await channel.parse_event(
                    event("file", {"file_key": "file-1", "file_name": "large.csv"})
                )

    async def test_send_text_and_file_to_original_thread(self):
        replies = []

        def handler(request):
            if request.url.path.endswith("/internal"):
                return httpx.Response(
                    200, json={"code": 0, "tenant_access_token": "fixture-token", "expire": 7200}
                )
            if request.url.path.endswith("/files"):
                self.assertIn(b"report.csv", request.content)
                return httpx.Response(200, json={"code": 0, "data": {"file_key": "uploaded-file"}})
            self.assertTrue(request.url.path.endswith("/original-message/reply"))
            body = json.loads(request.content)
            self.assertTrue(body["reply_in_thread"])
            self.assertEqual(len(body["uuid"]), 40)
            replies.append(body)
            return httpx.Response(
                200, json={"code": 0, "data": {"message_id": f"reply-{len(replies)}"}}
            )

        channel = self.channel(handler)
        receipt = await channel.send(
            reply_source(), TaskResult("finished", (Attachment("report.csv", b"a\n1\n"),))
        )
        self.assertEqual(receipt.status, "delivered")
        self.assertEqual(receipt.message_id, "reply-2")
        self.assertEqual([item["msg_type"] for item in replies], ["text", "file"])

    async def test_failed_auth_does_not_leak_platform_payload(self):
        channel = self.channel(
            lambda request: httpx.Response(200, json={"code": 999, "msg": "fixture-secret"})
        )
        receipt = await channel.send(reply_source(), TaskResult("hello"))
        self.assertEqual(receipt.status, "failed")
        self.assertNotIn("fixture-secret", repr(receipt))

    async def test_missing_receipt_timeout_and_partial_send_remain_unknown(self):
        for failure in ("missing-code", "missing-id", "timeout", "rejection-after-first"):
            replies = []

            def handler(request):
                if request.url.path.endswith("/internal"):
                    return httpx.Response(
                        200,
                        json={"code": 0, "tenant_access_token": "fixture-token", "expire": 7200},
                    )
                if request.url.path.endswith("/files"):
                    return httpx.Response(
                        200, json={"code": 0, "data": {"file_key": "uploaded-file"}}
                    )
                replies.append(request)
                if failure == "timeout":
                    raise httpx.ReadTimeout("fixture-secret", request=request)
                if failure == "missing-code":
                    return httpx.Response(200, json={"data": {"message_id": "reply-1"}})
                if failure == "missing-id":
                    return httpx.Response(200, json={"code": 0, "data": {}})
                if len(replies) == 1:
                    return httpx.Response(200, json={"code": 0, "data": {"message_id": "reply-1"}})
                return httpx.Response(200, json={"code": 42, "msg": "fixture-secret"})

            channel = self.channel(handler)
            receipt = await channel.send(
                reply_source(), TaskResult("hello", (Attachment("a.csv", b"a"),))
            )
            self.assertEqual(receipt.status, "unknown", failure)
            self.assertLessEqual(len(replies), 2)
            self.assertNotIn("fixture-secret", repr(receipt))

    async def test_close_releases_pending_ingress_and_waiter(self):
        channel = self.channel(allowed_senders=frozenset({"sender-1"}))
        ingress = asyncio.create_task(channel.ingest(event()))
        iterator = channel.receive()
        await anext(iterator)
        await channel.aclose()
        await channel.wait()
        with self.assertRaises(asyncio.CancelledError):
            await ingress
        await iterator.aclose()

    async def test_missing_optional_sdk_fails_with_actionable_diagnostic(self):
        channel = self.channel()
        with patch("importlib.util.find_spec", return_value=None):
            with self.assertRaisesRegex(FeishuError, "Install the feishu extra"):
                await channel.start()

    async def test_duplicate_file_send_identity_uses_content_not_upload_key(self):
        uploads, uuids = [], []

        def handler(request):
            if request.url.path.endswith("/internal"):
                return httpx.Response(
                    200, json={"code": 0, "tenant_access_token": "fixture-token", "expire": 7200}
                )
            if request.url.path.endswith("/files"):
                uploads.append(request)
                return httpx.Response(
                    200, json={"code": 0, "data": {"file_key": f"upload-{len(uploads)}"}}
                )
            uuids.append(json.loads(request.content)["uuid"])
            return httpx.Response(200, json={"code": 0, "data": {"message_id": "reply-1"}})

        channel = self.channel(handler)
        result = TaskResult("", (Attachment("a.csv", b"data"),))
        await channel.send(reply_source(), result)
        await channel.send(reply_source(), result)
        self.assertEqual(uuids[0], uuids[1])

    async def test_child_stop_is_reported_and_owned_process_is_joined(self):
        from unittest.mock import MagicMock

        channel = self.channel(submit=lambda message: asyncio.sleep(0))
        channel._connection = MagicMock()
        channel._connection.poll.return_value = True
        channel._connection.recv.return_value = {"error": "Feishu connection stopped"}
        channel._process = MagicMock()
        channel._process.is_alive.return_value = True
        await channel._pump_events()
        with self.assertRaisesRegex(FeishuError, "Feishu connection stopped"):
            await channel.wait()
        await channel.aclose()
        channel._process.terminate.assert_called_once()
        channel._process.join.assert_called_once_with(2)
        channel._connection.close.assert_called_once()

    async def test_only_declared_leading_mentions_are_removed_before_commands(self):
        channel = self.channel(allowed_senders=frozenset({"sender-1"}))
        mentions = [{"key": "@_user_1"}, {"key": "@_user_2"}]
        message = await channel.parse_event(
            event(content={"text": "@_user_1 @_user_2 /merge key=id"}, mentions=mentions)
        )
        self.assertEqual(message.text, "/merge key=id")
        for text, metadata in (
            ("@_user_1 /merge", []),
            ("keep @_user_1 /merge", mentions),
            ("@_user_1 ordinary conversation", mentions),
        ):
            message = await channel.parse_event(event(content={"text": text}, mentions=metadata))
            self.assertEqual(message.text, text)
