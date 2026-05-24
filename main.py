"""MyIrc — a minimal IRC client for osu! Bancho (irc.ppy.sh).

Usage:
    python main.py [--nick NICK] [--password PASS]

If not provided, nick and password will be prompted in the terminal.
"""

import argparse
import curses
import getpass
import re
import sys
import time
import threading
from collections import deque

if sys.platform == "win32":
    import msvcrt

from irc import IrcClient
from ui import ChatUI

SERVER = "irc.ppy.sh"
PORT = 6667

MP_ROOM_RE = re.compile(r"https://osu\.ppy\.sh/mp/(\d+)")


def main(stdscr: curses.window, nick: str, password: str):
    client = IrcClient(nick=nick, password=password, server=SERVER, port=PORT)

    ui = ChatUI(stdscr)

    current_channel = ""
    joined_channels = []
    pending_mp_until = 0.0  # epoch; while > now we're waiting on a BanchoBot reply

    def on_new_message():
        pass

    client.on_message = on_new_message

    def handle_join(channel: str):
        nonlocal current_channel
        chan = channel.lstrip("#")
        if chan not in joined_channels:
            joined_channels.append(chan)
        current_channel = chan

    client.on_join = handle_join

    def handle_part(channel: str):
        nonlocal current_channel
        chan = channel.lstrip("#")
        if chan in joined_channels:
            joined_channels.remove(chan)
        if current_channel == chan:
            if joined_channels:
                current_channel = joined_channels[-1]
            else:
                current_channel = ""

    client.on_part = handle_part

    ui.add_message("SYSTEM", f"Connecting to {SERVER}:{PORT} ...")
    try:
        client.connect()
    except Exception as e:
        ui.add_message("SYSTEM", f"Connection failed: {e}")
        ui.read_input()
        return

    ui.add_message("SYSTEM", f"Connected as {nick}. Type /join #channel to join.")

    def handle_command(line: str):
        nonlocal current_channel

        if not line:
            return

        if line.startswith("/"):
            parts = line.split(" ", 1)
            cmd = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else ""

            match cmd:
                case "/join":
                    channel = arg.strip()
                    if channel:
                        client.join(channel)
                        chan = channel.lstrip("#")
                        if chan not in joined_channels:
                            joined_channels.append(chan)
                        current_channel = chan
                        ui.add_message("SYSTEM", f"Joining {channel} ...")
                    else:
                        ui.add_message("SYSTEM", "Usage: /join #channel")

                case "/nick":
                    new_nick = arg.strip()
                    if new_nick:
                        client._send_raw(f"NICK {new_nick}")
                    else:
                        ui.add_message("SYSTEM", "Usage: /nick newname")

                case "/quit":
                    force = arg.strip().lower() == "force"
                    remaining = pending_mp_until - time.time()
                    if remaining > 0 and not force:
                        ui.add_message(
                            current_view(),
                            f"!! waiting {remaining:.0f}s for BanchoBot reply to recent !mp. "
                            "Use '/quit force' to abort anyway.",
                        )
                    else:
                        client.disconnect()
                        raise SystemExit

                case "/msg":
                    parts2 = arg.split(" ", 1)
                    if len(parts2) == 2:
                        target, text = parts2
                        client.send(target, text)
                        ui.add_message(target, f"<{client.nick}> {text}")
                        # Confirm in current view so the user knows it sent.
                        view = current_view()
                        if view.lstrip("#").lower() != target.lower():
                            ui.add_message(view, f"-- PM to {target}: {text}")
                    else:
                        ui.add_message("SYSTEM", "Usage: /msg target text")

                case "/switch" | "/window" | "/w":
                    name = arg.strip()
                    if not name:
                        ui.add_message(
                            current_view(),
                            f"-- joined: {', '.join(joined_channels) if joined_channels else '(none)'}. "
                            "Usage: /switch <channel|SYSTEM|nick>",
                        )
                    elif name.upper() == "SYSTEM":
                        current_channel = ""
                    else:
                        current_channel = name.lstrip("#")

                case _:
                    ui.add_message("SYSTEM", f"Unknown command: {cmd}")

        elif current_channel:
            low = line.lower().lstrip()
            # Only "!mp make" / "!mp makeprivate" need to be PM'd to BanchoBot.
            # Other !mp commands (close, settings, invite, ...) must stay in
            # the channel — they operate on the room you're currently in.
            if low.startswith("!mp make"):
                client.send("BanchoBot", line)
                ui.add_message("BanchoBot", f"<{client.nick}> {line}")
                pending_mp_until = time.time() + 20
                ui.add_message(
                    f"#{current_channel}",
                    f"-- sent to BanchoBot (as PM): {line}  *** DO NOT QUIT *** waiting up to 20s for reply.",
                )
            else:
                client.send(f"#{current_channel}", line)
                ui.add_message(f"#{current_channel}", f"<{client.nick}> {line}")
        else:
            ui.add_message("SYSTEM", "Join a channel first with /join #channel")

    def current_view() -> str:
        return f"#{current_channel}" if current_channel else "SYSTEM"

    def drain_queue():
        nonlocal current_channel, pending_mp_until
        any_drained = False
        while client.message_queue:
            tag, text = client.message_queue.popleft()
            ui.add_message(tag, text, redraw=False)
            any_drained = True
            # Mirror any BanchoBot PM into the user's current view.
            if tag == "BanchoBot":
                pending_mp_until = 0.0
                view = current_view()
                if view.lstrip("#").lower() != tag.lower():
                    ui.add_message(view, f"-- [BanchoBot PM] {text}", redraw=False)
                # Auto-join the MP room once BanchoBot confirms creation.
                m = MP_ROOM_RE.search(text)
                if m:
                    mp_channel = f"#mp_{m.group(1)}"
                    client.join(mp_channel)
                    chan = mp_channel.lstrip("#")
                    if chan not in joined_channels:
                        joined_channels.append(chan)
                    prev = current_channel
                    current_channel = chan
                    ui.add_message("SYSTEM", f"Auto-joining {mp_channel} ...", redraw=False)
                    if prev and prev != chan:
                        ui.add_message(f"#{prev}", f"-- room created: {mp_channel}, switched.", redraw=False)
        if any_drained:
            ui._draw_messages()

    # -- Windows input thread for IME/CJK support --
    # curses.get_wch() on Windows (PDCurses) cannot receive IME-composed characters.
    # We use a background thread with msvcrt.getwch() which works with IME.
    input_queue: deque = deque()
    input_lock = threading.Lock()

    def _input_thread():
        while client._running:
            ch = msvcrt.getwch()
            with input_lock:
                input_queue.append(ch)

    t = threading.Thread(target=_input_thread, daemon=True)
    t.start()

    # Main loop: drain message queue + read input
    while client._running:
        drain_queue()

        view_name = f"#{current_channel}" if current_channel else "SYSTEM"
        remaining = pending_mp_until - time.time()
        if remaining > 0:
            view_name += f"  [waiting BanchoBot {remaining:.0f}s — DO NOT QUIT]"
        ui.set_status(view_name, client.nick)

        # Process all queued input characters
        with input_lock:
            chars = list(input_queue)
            input_queue.clear()

        for ch in chars:
            if ch in ("\r", "\n"):
                line = ui.input_buffer
                ui.input_buffer = ""
                ui._draw_messages()
                handle_command(line)
            elif ch in ("\x7f", "\x08", "\b"):
                if len(ui.input_buffer) > 0:
                    ui.input_buffer = ui.input_buffer[:-1]
                    ui._draw_messages()
            # \x00 or \xe0 = function key prefix on Windows; discard
            elif ch in ("\x00", "\xe0"):
                continue
            elif ord(ch) >= 32:
                ui.input_buffer += ch
                ui._draw_messages()

        drain_queue()
        time.sleep(0.05)  # avoid busy-loop when no input

    drain_queue()

    ui.add_message("SYSTEM", "Press Enter to exit.")
    while True:
        ch = msvcrt.getwch()
        if ch in ("\r", "\n"):
            break


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="MyIrc — osu! Bancho IRC client")
    p.add_argument("--nick", default=None, help="Your osu! username")
    p.add_argument("--password", default=None, help="Your IRC server password")
    args = p.parse_args()

    nick = args.nick
    password = args.password

    if not nick:
        nick = input("Username: ").strip()
    if not password:
        password = getpass.getpass("IRC Password: ")

    if not nick or not password:
        print("Username and password are required.")
        sys.exit(1)

    try:
        curses.wrapper(lambda stdscr: main(stdscr, nick, password))
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"\nError: {e}")
        input("Press Enter to exit...")
