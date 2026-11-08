"""Line-based transports between the Marlin driver and firmware.

``SerialTransport`` talks to a real serial port (or a pseudo-terminal) with the operating
system's terminal API, so no extra dependency is needed. It is POSIX only, which matches
the platforms PicnicPlace supports (spec section 9). ``memory_pair`` connects two ends in
memory, for tests.
"""

import asyncio
import contextlib
import os
import termios
import tty
from typing import Protocol

BAUD_RATES = {
    9600: termios.B9600,
    19200: termios.B19200,
    38400: termios.B38400,
    57600: termios.B57600,
    115200: termios.B115200,
    230400: termios.B230400,
}


class TransportClosedError(ConnectionError):
    """The other end went away."""


class LineTransport(Protocol):
    async def write_line(self, line: str) -> None: ...

    async def read_line(self) -> str:
        """The next line, without its line ending. Raises TransportClosedError at the end."""
        ...

    async def close(self) -> None: ...


class SerialTransport:
    """A serial device opened in raw mode, read and written without blocking."""

    def __init__(self, fd: int) -> None:
        self._fd: int | None = fd
        self._loop = asyncio.get_running_loop()
        self._lines: asyncio.Queue[str | None] = asyncio.Queue()
        self._buffer = b""
        self._loop.add_reader(fd, self._on_readable)

    @classmethod
    async def open(cls, path: str, baud: int = 115200) -> "SerialTransport":
        speed = BAUD_RATES.get(baud)
        if speed is None:
            raise ValueError(f"unsupported baud rate {baud}")
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            tty.setraw(fd)
            attrs = termios.tcgetattr(fd)
            attrs[4] = attrs[5] = speed  # input and output speed
            attrs[2] |= termios.CLOCAL | termios.CREAD
            attrs[2] &= ~termios.CRTSCTS if hasattr(termios, "CRTSCTS") else ~0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            termios.tcflush(fd, termios.TCIOFLUSH)
        except Exception:
            os.close(fd)
            raise
        return cls(fd)

    def _on_readable(self) -> None:
        assert self._fd is not None
        try:
            chunk = os.read(self._fd, 4096)
        except BlockingIOError:
            return
        except OSError:
            chunk = b""
        if not chunk:
            self._shutdown_reader()
            return
        self._buffer += chunk
        while b"\n" in self._buffer:
            raw, self._buffer = self._buffer.split(b"\n", 1)
            self._lines.put_nowait(raw.rstrip(b"\r").decode("ascii", errors="replace"))

    def _shutdown_reader(self) -> None:
        if self._fd is not None:
            self._loop.remove_reader(self._fd)
        self._lines.put_nowait(None)

    async def write_line(self, line: str) -> None:
        if self._fd is None:
            raise TransportClosedError("the serial port is closed")
        data = (line + "\n").encode("ascii")
        while data:
            try:
                written = os.write(self._fd, data)
            except BlockingIOError:
                await self._wait_writable()
                continue
            except OSError as e:
                raise TransportClosedError(f"serial write failed: {e}") from e
            data = data[written:]

    async def _wait_writable(self) -> None:
        assert self._fd is not None
        ready: asyncio.Future[None] = self._loop.create_future()
        self._loop.add_writer(self._fd, ready.set_result, None)
        try:
            await ready
        finally:
            self._loop.remove_writer(self._fd)

    async def read_line(self) -> str:
        line = await self._lines.get()
        if line is None:
            self._lines.put_nowait(None)  # stay closed for later readers
            raise TransportClosedError("the serial port closed")
        return line

    async def close(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        self._loop.remove_reader(fd)
        self._lines.put_nowait(None)
        with contextlib.suppress(OSError):
            os.close(fd)


class MemoryTransport:
    """One end of an in-memory line link."""

    def __init__(self, incoming: "asyncio.Queue[str | None]") -> None:
        self._incoming = incoming
        self.peer: MemoryTransport | None = None
        self.closed = False

    async def write_line(self, line: str) -> None:
        if self.closed or self.peer is None or self.peer.closed:
            raise TransportClosedError("the link is closed")
        self.peer._incoming.put_nowait(line)

    async def read_line(self) -> str:
        line = await self._incoming.get()
        if line is None:
            self._incoming.put_nowait(None)
            raise TransportClosedError("the link closed")
        return line

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._incoming.put_nowait(None)
        if self.peer is not None:
            self.peer._incoming.put_nowait(None)


def memory_pair() -> tuple[MemoryTransport, MemoryTransport]:
    a = MemoryTransport(asyncio.Queue())
    b = MemoryTransport(asyncio.Queue())
    a.peer, b.peer = b, a
    return a, b
