"""Open event streams never take the connections control calls need."""

from __future__ import annotations

import asyncio
import json
import unittest

from mash.runtime.client import AgentClient, CONTROL_LIMITS


class _Server:
    """A minimal HTTP/1.1 server: streams stay open, status answers at once."""

    def __init__(self) -> None:
        self.open_streams = 0
        self._release = asyncio.Event()
        self._server: asyncio.AbstractServer | None = None
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    def release(self) -> None:
        self._release.set()

    async def stop(self) -> None:
        self._release.set()
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                request_line = await reader.readline()
                if not request_line:
                    return
                while (await reader.readline()) not in (b"\r\n", b"\n", b""):
                    pass
                path = request_line.split()[1].decode()
                if path.endswith("/status"):
                    body = json.dumps({"request_id": "r", "status": "running"}).encode()
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                        + f"Content-Length: {len(body)}\r\n\r\n".encode()
                        + body
                    )
                    await writer.drain()
                    continue
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                    b"Transfer-Encoding: chunked\r\n\r\n"
                )
                await writer.drain()
                self.open_streams += 1
                await self._release.wait()
                writer.write(b"0\r\n\r\n")
                await writer.drain()
                return
        except (ConnectionError, asyncio.CancelledError):
            return
        finally:
            writer.close()


class AgentClientPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_answers_while_more_streams_are_open_than_control_connections(
        self,
    ) -> None:
        server = _Server()
        await server.start()
        client = AgentClient(f"http://127.0.0.1:{server.port}", "agent-a")
        streams = CONTROL_LIMITS.max_connections + 60

        async def _watch(index: int) -> None:
            async for _ in client.stream_response(f"r-{index}"):
                pass

        watchers = [asyncio.create_task(_watch(i)) for i in range(streams)]
        try:
            for _ in range(200):
                if server.open_streams == streams:
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(server.open_streams, streams)

            statuses = await asyncio.wait_for(
                asyncio.gather(
                    *(client.get_request_status("r", timeout=2.0) for _ in range(20))
                ),
                timeout=5.0,
            )
            self.assertEqual({s["status"] for s in statuses}, {"running"})
        finally:
            # The server waits for every connection to close, keep-alive ones
            # included, so the client closes first.
            server.release()
            await asyncio.gather(*watchers, return_exceptions=True)
            await client.close()
            await server.stop()
