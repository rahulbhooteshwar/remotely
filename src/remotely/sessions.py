"""In-app session registry.

Replaces the old tmux backend. A session is a transport plus the terminal
emulator being fed by it; tabs are a UI concern layered on top, so nothing here
imports Textual.

The trade-off against the tmux design is explicit: sessions live inside this
process, so quitting Remotely closes them. In exchange there is nothing to
install and the whole thing ships as one binary.
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterator, Literal

from .models import Credential, Host
from .terminal import TerminalEmulator
from .themes import Theme
from .transport import Transport, TransportError, create_transport

Status = Literal["connecting", "connected", "closed", "error"]

_ids = itertools.count(1)


class SessionError(RuntimeError):
    """Raised when a session cannot be created."""


@dataclass(slots=True)
class Session:
    """One open connection and its screen."""

    id: str
    host_name: str
    theme: Theme
    transport: Transport
    emulator: TerminalEmulator
    status: Status = "connecting"
    error: str | None = None
    notes: list[str] = field(default_factory=list)
    _buffer: list[bytes] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _reader: threading.Thread | None = None
    _stop: threading.Event = field(default_factory=threading.Event)

    @property
    def title(self) -> str:
        return self.theme.tab_title(self.host_name)

    @property
    def is_live(self) -> bool:
        return self.status in ("connecting", "connected")

    # ------------------------------------------------------------------ bytes

    def push(self, data: bytes) -> None:
        """Called from the reader thread."""
        with self._lock:
            self._buffer.append(data)

    def drain(self) -> bytes:
        """Called from the UI thread; returns everything received since last time."""
        with self._lock:
            if not self._buffer:
                return b""
            data = b"".join(self._buffer)
            self._buffer.clear()
        return data

    # --------------------------------------------------------------- lifecycle

    def start_reader(self, poll_interval: float = 0.02) -> None:
        """Pump the transport in the background.

        Reads are non-blocking, so this polls. 50Hz is well under the rate at
        which a human notices latency and costs almost nothing when idle.
        """
        if self._reader is not None:
            return

        def pump() -> None:
            while not self._stop.is_set():
                try:
                    data = self.transport.read()
                except Exception as exc:  # transport already closed, usually
                    self.error = str(exc)
                    break
                if data:
                    self.push(data)
                    continue
                if self.transport.closed:
                    break
                self._stop.wait(poll_interval)
            if self.status != "error":
                self.status = "closed"

        self._reader = threading.Thread(target=pump, daemon=True, name=f"rx-{self.id}")
        self._reader.start()

    def write(self, data: bytes) -> None:
        if self.is_live:
            self.transport.write(data)

    def resize(self, cols: int, rows: int) -> None:
        self.emulator.resize(cols, rows)
        if self.is_live:
            self.transport.resize(cols, rows)

    def close(self) -> None:
        self._stop.set()
        try:
            self.transport.close()
        finally:
            self.status = "closed"

    @property
    def exit_status(self) -> int | None:
        return self.transport.exit_status


class SessionManager:
    """Owns every open session."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._order: list[str] = []

    def __len__(self) -> int:
        return len(self._sessions)

    def __iter__(self) -> Iterator[Session]:
        return iter(self.list())

    def list(self) -> list[Session]:
        return [self._sessions[i] for i in self._order if i in self._sessions]

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def live(self) -> list[Session]:
        return [s for s in self.list() if s.is_live]

    def for_host(self, host_name: str) -> list[Session]:
        key = host_name.lower()
        return [s for s in self.list() if s.host_name.lower() == key]

    def open(
        self,
        host: Host,
        credential: Credential | None,
        theme: Theme,
        *,
        cols: int = 80,
        rows: int = 24,
        on_ready: Callable[[Session], None] | None = None,
    ) -> Session:
        """Create a session and connect it on a background thread.

        Connecting can block for seconds (DNS, TCP, auth), so it never happens
        on the UI thread. The session appears immediately in "connecting" state
        and updates itself when the handshake finishes.
        """
        transport = create_transport(host, credential)
        emulator = TerminalEmulator(cols, rows)
        session = Session(
            id=f"s{next(_ids)}",
            host_name=host.name,
            theme=theme,
            transport=transport,
            emulator=emulator,
            notes=list(getattr(transport, "spec", None).notes if getattr(transport, "spec", None) else []),
        )
        self._sessions[session.id] = session
        self._order.append(session.id)

        def connect() -> None:
            try:
                transport.connect(cols, rows)
            except TransportError as exc:
                session.status = "error"
                session.error = str(exc)
            except Exception as exc:
                session.status = "error"
                session.error = f"Unexpected error: {exc}"
            else:
                session.status = "connected"
                session.start_reader()
            finally:
                if on_ready is not None:
                    on_ready(session)

        threading.Thread(target=connect, daemon=True, name=f"connect-{session.id}").start()
        return session

    def close(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionError(f"No session {session_id!r}.")
        session.close()
        return session

    def remove(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        if session_id in self._order:
            self._order.remove(session_id)

    def close_all(self) -> None:
        for session in self.list():
            try:
                session.close()
            except Exception:
                continue
