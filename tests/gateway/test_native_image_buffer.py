"""Native image buffer semantics under Cue's one-main-thread session model.

Every chat of a profile is the same conversation, so there is ONE native-image buffer per
profile: a buffered image pairs with the conversation's NEXT turn (whoever sends it), and each
inbound prep resets the buffer so a stale image never attaches to an unrelated later turn.
Interleaving is serialized by the busy path (the second window's message queues behind the
image-bearing turn), so the pairing holds in practice.
"""

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key


def _make_runner() -> GatewayRunner:
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake")},
    )
    runner.adapters = {}
    runner._model = "openai/gpt-4.1-mini"
    runner._base_url = None
    runner._decide_image_input_mode = lambda **_: "native"
    return runner


def _source(chat_id: str) -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        chat_type="private",
        user_name=f"user-{chat_id}",
    )


def _image_event(source: SessionSource, path: str) -> MessageEvent:
    return MessageEvent(
        text="see image",
        message_type=MessageType.PHOTO,
        source=source,
        media_urls=[path],
        media_types=["image/png"],
    )


@pytest.mark.asyncio
async def test_native_image_buffer_is_one_per_main_thread():
    runner = _make_runner()
    source_a = _source("chat-a")
    source_b = _source("chat-b")

    await runner._prepare_inbound_message_text(
        event=_image_event(source_a, "/tmp/a.png"),
        source=source_a,
        history=[],
    )
    await runner._prepare_inbound_message_text(
        event=_image_event(source_b, "/tmp/b.png"),
        source=source_b,
        history=[],
    )

    # Both windows share the conversation's buffer: the latest prep holds it.
    assert runner._consume_pending_native_image_paths(build_session_key(source_a)) == ["/tmp/b.png"]
    assert runner._consume_pending_native_image_paths(build_session_key(source_b)) == []


@pytest.mark.asyncio
async def test_plain_text_prep_resets_the_threads_image_buffer():
    runner = _make_runner()
    source_a = _source("chat-a")
    source_b = _source("chat-b")

    await runner._prepare_inbound_message_text(
        event=_image_event(source_a, "/tmp/a.png"),
        source=source_a,
        history=[],
    )
    await runner._prepare_inbound_message_text(
        event=MessageEvent(text="plain text", source=source_b),
        source=source_b,
        history=[],
    )

    # A text turn from any window pairs with — and consumes — the buffered image slot: a stale
    # image must never attach to an unrelated later turn of the conversation.
    assert runner._consume_pending_native_image_paths(build_session_key(source_a)) == []
    assert runner._consume_pending_native_image_paths(build_session_key(source_b)) == []
