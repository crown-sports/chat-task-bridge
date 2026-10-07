"""Check the actual optional SDK model boundary without opening a connection."""

import json

import httpx
import pytest

from chatbridge.channels.feishu import FeishuChannel


@pytest.mark.asyncio
async def test_official_sdk_event_roundtrip_preserves_routing_and_command():
    lark = pytest.importorskip("lark_oapi")
    payload = {
        "schema": "2.0",
        "header": {
            "event_type": "im.message.receive_v1",
            "event_id": "fixture-event",
            "app_id": "fixture-app",
        },
        "event": {
            "sender": {
                "sender_type": "user",
                "sender_id": {"open_id": "fixture-sender"},
            },
            "message": {
                "message_id": "fixture-message",
                "chat_id": "fixture-chat",
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": "@_user_1 /merge key=id"}),
                "mentions": [{"key": "@_user_1", "name": "Task bot"}],
                "thread_id": "fixture-thread",
            },
        },
    }
    normalized = []
    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(
            lambda event: normalized.append(json.loads(lark.JSON.marshal(event)))
        )
        .build()
    )
    handler._do_without_validation(json.dumps(payload).encode())

    def no_requests(request):
        raise AssertionError("SDK fixture test must not use the network")

    async with httpx.AsyncClient(transport=httpx.MockTransport(no_requests)) as client:
        channel = FeishuChannel(
            "fixture-app",
            "fixture-secret",
            allowed_senders=frozenset({"fixture-sender"}),
            client=client,
        )
        try:
            message = await channel.parse_event(normalized[0])
            assert message is not None
            assert message.event_id == "fixture-event"
            assert message.channel == "feishu"
            assert message.account_id == "fixture-app"
            assert message.conversation_id == "fixture-chat"
            assert message.sender_id == "fixture-sender"
            assert message.reply_token == "fixture-message"
            assert message.thread_id == "fixture-thread"
            assert message.text == "/merge key=id"
        finally:
            await channel.aclose()
