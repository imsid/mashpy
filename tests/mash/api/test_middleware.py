"""The API logging middleware buffers a request body without changing the request."""

from __future__ import annotations

import asyncio

from mash.api.middleware import _capture_receive_body


def test_replay_hands_over_to_the_server_once_the_body_is_replayed() -> None:
    """After the buffered body, receive waits on the server again.

    A streaming response listens for the disconnect with ``await receive()`` in
    a loop. Answering that with empty bodies forever would never yield, and the
    listener would spin the event loop.
    """
    sent = [
        {"type": "http.request", "body": b"ab", "more_body": True},
        {"type": "http.request", "body": b"c", "more_body": False},
        {"type": "http.disconnect"},
    ]

    async def receive() -> dict:
        return sent.pop(0)

    async def run() -> None:
        body, replay = await _capture_receive_body(receive)
        assert body == b"abc"
        assert await replay() == {"type": "http.request", "body": b"ab", "more_body": True}
        assert await replay() == {"type": "http.request", "body": b"c", "more_body": False}
        assert await replay() == {"type": "http.disconnect"}

    asyncio.run(run())
