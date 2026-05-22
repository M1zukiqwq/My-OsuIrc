"""Minimal IRC protocol handler."""

import socket
import threading
from collections import deque
from typing import Callable


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

        # outgoing message queue consumed by the UI
        self.message_queue: deque[tuple[str, str]] = deque()
        # callback invoked (from reader thread) when a new message arrives
        self.on_message: Callable[[], None] | None = None

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

    # -- internal --

    def _send_raw(self, line: str) -> None:
        if self._sock is None:
            return
        self._sock.sendall((line + "\r\n").encode("utf-8", errors="replace"))

    def _reader_loop(self) -> None:
        while self._running:
            try:
                data = self._sock.recv(4096)
            except OSError:
                break
            if not data:
                break

            self._recv_buffer += data.decode("utf-8", errors="replace")

            while "\r\n" in self._recv_buffer:
                line, self._recv_buffer = self._recv_buffer.split("\r\n", 1)
                if not line:
                    continue
                self._handle_line(line)

        self._running = False
        self._enqueue("SYSTEM", "Disconnected from server.")

    def _handle_line(self, line: str) -> None:
        prefix, command, params = parse_irc_message(line)

        match command:
            case "PING":
                self._send_raw(f"PONG :{params[0] if params else ''}")
                return

            case "PRIVMSG":
                sender = extract_nick(prefix)
                target = params[0] if params else ""
                text = params[1] if len(params) > 1 else ""
                tag = target if target == self.nick else sender
                self._enqueue(tag, f"<{sender}> {text}")

            case "JOIN":
                sender = extract_nick(prefix)
                channel = params[0] if params else ""
                self._enqueue(channel, f"-- {sender} joined {channel}")

            case "PART":
                sender = extract_nick(prefix)
                channel = params[0] if params else ""
                text = f" ({params[1]})" if len(params) > 1 else ""
                self._enqueue(channel, f"-- {sender} left {channel}{text}")

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
                        # name list
                        text = params[-1] if params else ""
                        channel = params[2] if len(params) > 2 else ""
                        self._enqueue(channel or "SYSTEM", f"-- Users: {text}")
                    elif command == "366":
                        pass  # end of names, skip
                    elif command in ("431", "432", "433"):
                        # nick errors
                        text = params[-1] if params else line
                        self._enqueue("SYSTEM", f"!! {text}")
                else:
                    # Unknown command — show in system for debugging
                    self._enqueue("SYSTEM", line)

    def _enqueue(self, tag: str, text: str) -> None:
        self.message_queue.append((tag, text))
        if self.on_message:
            self.on_message()
