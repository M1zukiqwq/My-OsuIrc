"""Minimal IRC protocol handler."""

import socket
import threading
from collections import deque
from typing import Callable

# DEBUG: append raw IRC traffic to debug.log (keep history across runs)
import os
_debug_log = open(os.path.join(os.path.dirname(__file__), "debug.log"), "a", encoding="utf-8")
_debug_log.write("\n===== new session =====\n")

def _debug(msg: str):
    _debug_log.write(msg + "\n")
    _debug_log.flush()


def parse_irc_message(raw: str) -> tuple[str, str, list[str]]:
    """Parse an IRC message into (prefix, command, params).

    Returns:
        prefix: sender info (nick!user@host) or empty string
        command: IRC command (PRIVMSG, JOIN, PING, etc.)
        params: list of parameters (last one may contain spaces if prefixed with :)
    """
    prefix = ""
    trailing = ""

    if raw.startswith(":"):
        prefix, raw = raw[1:].split(" ", 1)

    if " :" in raw:
        raw, trailing = raw.split(" :", 1)

    parts = raw.split()
    command = parts[0] if parts else ""
    params = parts[1:] if len(parts) > 1 else []

    if trailing:
        params.append(trailing)

    return prefix, command, params


def extract_nick(prefix: str) -> str:
    """Extract nickname from a prefix like 'nick!user@host'."""
    return prefix.split("!")[0] if prefix else ""


class IrcClient:
    """Threaded IRC client with a simple callback interface."""

    def __init__(
        self,
        nick: str,
        password: str,
        server: str = "irc.ppy.sh",
        port: int = 6667,
    ):
        self.server = server
        self.port = port
        self.nick = nick
        self.password = password

        self._sock: socket.socket | None = None
        self._reader_thread: threading.Thread | None = None
        self._running = False
        self._recv_buffer = ""
        self._send_lock = threading.Lock()
        self._queue_lock = threading.Lock()

        # outgoing message queue consumed by the UI
        self.message_queue: deque[tuple[str, str]] = deque()
        # callback invoked (from reader thread) when a new message arrives
        self.on_message: Callable[[], None] | None = None
        self.on_join: Callable[[str], None] | None = None
        self.on_part: Callable[[str], None] | None = None

    # -- connection --

    def connect(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect((self.server, self.port))
        sock.settimeout(None)
        self._sock = sock
        self._running = True

        self._send_raw(f"PASS {self.password}")
        self._send_raw(f"NICK {self.nick}")
        self._send_raw(f"USER {self.nick} 0 * :{self.nick}")

        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def disconnect(self) -> None:
        self._running = False
        if self._sock:
            try:
                self._send_raw("QUIT :bye")
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # -- public helpers --

    def join(self, channel: str) -> None:
        if not channel.startswith("#"):
            channel = "#" + channel
        self._send_raw(f"JOIN {channel}")

    def send(self, target: str, text: str) -> None:
        self._send_raw(f"PRIVMSG {target} :{text}")

    def drain_messages(self) -> list[tuple[str, str]]:
        """Atomically flush queued UI messages."""
        with self._queue_lock:
            messages = list(self.message_queue)
            self.message_queue.clear()
        return messages

    # -- internal --

    def _send_raw(self, line: str) -> None:
        with self._send_lock:
            sock = self._sock
            if sock is None:
                return
            _debug(f">>> {line}")
            sock.sendall((line + "\r\n").encode("utf-8", errors="replace"))

    def _reader_loop(self) -> None:
        while self._running:
            try:
                sock = self._sock
                if sock is None:
                    break
                data = sock.recv(4096)
            except OSError:
                break
            if not data:
                break

            self._recv_buffer += data.decode("utf-8", errors="replace")

            # osu! sometimes batches messages with only \n between them and
            # \r\n at the very end of the chunk. Split on \n and strip any
            # trailing \r so both line endings work.
            while "\n" in self._recv_buffer:
                line, self._recv_buffer = self._recv_buffer.split("\n", 1)
                line = line.rstrip("\r")
                if not line:
                    continue
                try:
                    self._handle_line(line)
                except Exception as e:
                    # Don't let a single bad line kill the reader thread.
                    _debug(f"!!! handler error: {e!r} on line: {line!r}")
                    self._enqueue("SYSTEM", f"!! handler error: {e}")

        self._running = False
        self._enqueue("SYSTEM", "Disconnected from server.")

    def _handle_line(self, line: str) -> None:
        _debug(f"<<< {line}")
        prefix, command, params = parse_irc_message(line)

        match command:
            case "PING":
                self._send_raw(f"PONG :{params[0] if params else ''}")
                return

            case "PRIVMSG" | "NOTICE":
                sender = extract_nick(prefix)
                target = params[0] if params else ""
                text = params[1] if len(params) > 1 else ""
                # Channel messages (#channel) → tag is the channel.
                # Private messages (target == our nick) → tag is the sender.
                if target.startswith("#"):
                    tag = target
                else:
                    tag = sender
                prefix_marker = "-" if command == "NOTICE" else ""
                self._enqueue(tag, f"{prefix_marker}<{sender}>{prefix_marker} {text}")

            case "JOIN":
                sender = extract_nick(prefix)
                channel = params[0] if params else ""
                self._enqueue(channel, f"-- {sender} joined {channel}")
                if sender.lower() == self.nick.lower() and self.on_join:
                    self.on_join(channel)

            case "PART":
                sender = extract_nick(prefix)
                channel = params[0] if params else ""
                text = f" ({params[1]})" if len(params) > 1 else ""
                self._enqueue(channel, f"-- {sender} left {channel}{text}")
                if sender.lower() == self.nick.lower() and self.on_part:
                    self.on_part(channel)

            case "QUIT":
                sender = extract_nick(prefix)
                text = f" ({params[0]})" if params else ""
                self._enqueue("SYSTEM", f"-- {sender} quit{text}")

            case "NICK":
                old = extract_nick(prefix)
                new = params[0] if params else ""
                if old == self.nick:
                    self.nick = new
                self._enqueue("SYSTEM", f"-- {old} is now known as {new}")

            case _:  # numeric replies & everything else
                # Show numerics that carry useful info (001 welcome, 332 topic, etc.)
                if command.isdigit():
                    if command in ("001", "002", "003", "004", "375", "372", "376"):
                        # MOTD / welcome
                        text = params[-1] if params else line
                        self._enqueue("SYSTEM", text)
                    elif command in ("332", "333"):
                        # topic
                        text = params[-1] if params else ""
                        channel = params[1] if len(params) > 1 else ""
                        self._enqueue(channel or "SYSTEM", f"-- Topic: {text}")
                    elif command in ("353",):
                        # Name list. Dropped entirely — for channels like
                        # #osu it's hundreds of lines and is never useful.
                        pass
                    elif command == "366":
                        # End of NAMES — give the channel one short marker.
                        channel = params[1] if len(params) > 1 else ""
                        self._enqueue(channel or "SYSTEM", "-- (joined, ready)")
                    elif command in ("431", "432", "433"):
                        # nick errors
                        text = params[-1] if params else line
                        self._enqueue("SYSTEM", f"!! {text}")
                else:
                    # Unknown command — show in system for debugging
                    self._enqueue("SYSTEM", line)

    def _enqueue(self, tag: str, text: str) -> None:
        with self._queue_lock:
            self.message_queue.append((tag, text))
        if self.on_message:
            self.on_message()
