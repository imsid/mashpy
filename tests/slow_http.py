"""A raw HTTP server that answers in two parts, to tell a stream from a buffer.

``GET /slow`` sends ``first``, waits for ``release``, then sends ``second``.
A proxy that buffers the response delivers nothing until ``release`` is set.

``POST /upload`` sets ``got_first`` as soon as the body's first chunk arrives,
then reads the rest and answers with its length. A proxy that buffers the
request forwards nothing until the client has sent all of it.

``GET /gzip`` answers ``compressed page`` gzip-encoded.
"""

from __future__ import annotations

import asyncio
import gzip


class SlowHTTPServer:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.got_first = asyncio.Event()
        self.port = 0
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            request_line, *header_lines = head.decode("latin-1").split("\r\n")
            method, path, _ = request_line.split(" ", 2)
            headers = {
                name.strip().lower(): value.strip()
                for name, _, value in (line.partition(":") for line in header_lines if line)
            }
            if method == "GET" and path.startswith("/slow"):
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                    b"Transfer-Encoding: chunked\r\n\r\n5\r\nfirst\r\n"
                )
                await writer.drain()
                await self.release.wait()
                writer.write(b"6\r\nsecond\r\n0\r\n\r\n")
            elif method == "GET" and path.startswith("/gzip"):
                body = gzip.compress(b"compressed page")
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                    b"Content-Encoding: gzip\r\n"
                    + f"Content-Length: {len(body)}\r\n\r\n".encode()
                    + body
                )
            elif method == "POST" and path.startswith("/upload"):
                size = await self._read_body(reader, headers)
                body = str(size).encode()
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                    + f"Content-Length: {len(body)}\r\n\r\n".encode()
                    + body
                )
            else:
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        finally:
            writer.close()

    async def _read_body(self, reader: asyncio.StreamReader, headers: dict[str, str]) -> int:
        if "content-length" in headers:
            remaining = int(headers["content-length"])
            size = 0
            while remaining:
                chunk = await reader.read(min(remaining, 65536))
                if not chunk:
                    break
                size += len(chunk)
                remaining -= len(chunk)
                self.got_first.set()
            return size
        size = 0
        while True:
            length = int((await reader.readline()).strip(), 16)
            if length == 0:
                await reader.readline()
                return size
            size += len(await reader.readexactly(length))
            await reader.readline()
            self.got_first.set()
