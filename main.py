"""MyIrc — osu! Bancho IRC client and AI referee system.

Usage:
    python main.py server --host 127.0.0.1 --port 8765
    python main.py agent --nick NICK --password PASS --server-url http://127.0.0.1:8765
    python main.py referee --server-url http://127.0.0.1:8765
    python main.py chat --nick NICK --password PASS

If not provided, nick and password will be prompted in the terminal for IRC modes.
"""

import argparse
import curses
import getpass
import locale
import re
import sys
import threading
import time
from collections import deque

if sys.platform == "win32":
    import msvcrt

from irc import IrcClient
from referee_agent import RefereeAgent
from referee_api import RefereeApiClient
from referee_client import ServerRefereeCli
from referee_server import SQLiteRefereeStore, import_json_store, run_server
from ui import ChatUI

SERVER = "irc.ppy.sh"
PORT = 6667

MP_ROOM_RE = re.compile(r"https://osu\.ppy\.sh/mp/(\d+)")


def chat_main(stdscr: curses.window, nick: str, password: str):
    client = IrcClient(nick=nick, password=password, server=SERVER, port=PORT)

    ui = ChatUI(stdscr)

    current_channel = ""
    joined_channels = []
    pending_mp_until = 0.0  # epoch; while > now we're waiting on a BanchoBot reply
    wake_event = threading.Event()

    def on_new_message():
        wake_event.set()

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
        for tag, text in client.drain_messages():
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

    input_queue: deque[str | int] = deque()
    input_lock = threading.Lock()
    windows_key_map = {
        "G": curses.KEY_HOME,
        "H": curses.KEY_UP,
        "O": curses.KEY_END,
        "P": curses.KEY_DOWN,
        "I": curses.KEY_PPAGE,
        "Q": curses.KEY_NPAGE,
        "S": curses.KEY_DC,
    }

    def _windows_input_thread() -> None:
        while client._running:
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                mapped = windows_key_map.get(msvcrt.getwch())
                if mapped is None:
                    continue
                ch = mapped
            with input_lock:
                input_queue.append(ch)
            wake_event.set()

    if sys.platform == "win32":
        # curses.get_wch() on Windows (PDCurses) cannot receive IME-composed
        # characters. msvcrt.getwch() does, so keep that path for CJK input.
        t = threading.Thread(target=_windows_input_thread, daemon=True)
        t.start()

    def pending_input() -> list[str | int]:
        if sys.platform == "win32":
            with input_lock:
                chars = list(input_queue)
                input_queue.clear()
            return chars

        chars: list[str | int] = []
        while True:
            try:
                ch = stdscr.get_wch()
            except curses.error:
                break
            chars.append(ch)
        return chars

    def submit_input() -> None:
        line = ui.input_buffer
        ui.input_buffer = ""
        ui._draw_messages()
        handle_command(line)

    def handle_key(ch: str | int) -> None:
        if isinstance(ch, str):
            if ch in ("\r", "\n"):
                submit_input()
            elif ch in ("\x7f", "\x08", "\b"):
                if ui.input_buffer:
                    ui.input_buffer = ui.input_buffer[:-1]
                    ui._draw_messages()
            elif sys.platform == "win32" and ch in ("\x00", "\xe0"):
                # Function-key prefix emitted by msvcrt; the next wchar carries
                # the key code and is intentionally ignored by this text input.
                return
            elif ch.isprintable():
                ui.input_buffer += ch
                ui._draw_messages()
            return

        if ch in (curses.KEY_ENTER,):
            submit_input()
        elif ch in (curses.KEY_BACKSPACE, curses.KEY_DC):
            if ui.input_buffer:
                ui.input_buffer = ui.input_buffer[:-1]
                ui._draw_messages()
        elif ch == curses.KEY_UP:
            ui.scroll_up()
        elif ch == curses.KEY_DOWN:
            ui.scroll_down()
        elif ch == curses.KEY_PPAGE:
            ui.scroll_page_up()
        elif ch == curses.KEY_NPAGE:
            ui.scroll_page_down()
        elif ch == curses.KEY_HOME:
            ui.scroll_to_top()
        elif ch == curses.KEY_END:
            ui.scroll_to_bottom()
        elif ch == curses.KEY_RESIZE:
            ui._draw_messages()

    # Main loop: drain message queue + read input
    while client._running:
        drain_queue()

        view_name = f"#{current_channel}" if current_channel else "SYSTEM"
        remaining = pending_mp_until - time.time()
        if remaining > 0:
            view_name += f"  [waiting BanchoBot {remaining:.0f}s — DO NOT QUIT]"
        ui.set_status(view_name, client.nick)

        for ch in pending_input():
            handle_key(ch)

        drain_queue()
        wake_event.wait(0.05)
        wake_event.clear()

    drain_queue()

    ui.add_message("SYSTEM", "Press Enter to exit.")
    if sys.platform == "win32":
        while True:
            ch = msvcrt.getwch()
            if ch in ("\r", "\n"):
                break
    else:
        stdscr.nodelay(False)
        stdscr.timeout(-1)
        while True:
            try:
                ch = stdscr.get_wch()
            except curses.error:
                break
            if ch in ("\r", "\n", curses.KEY_ENTER):
                break


def prompt_credentials(args: argparse.Namespace) -> tuple[str, str]:
    nick = args.nick
    password = args.password

    if not nick:
        nick = input("Username: ").strip()
    if not password:
        password = getpass.getpass("IRC Password: ")

    if not nick or not password:
        print("Username and password are required.")
        sys.exit(1)
    return nick, password


def run_chat_tui(nick: str, password: str) -> None:
    try:
        curses.wrapper(lambda stdscr: chat_main(stdscr, nick, password))
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"\nError: {e}")
        input("Press Enter to exit...")


def run_agent(nick: str, password: str, server_url: str) -> None:
    client = IrcClient(nick=nick, password=password, server=SERVER, port=PORT)
    print(f"Connecting to {SERVER}:{PORT} ...")
    try:
        client.connect()
    except Exception as e:
        print(f"Connection failed: {e}")
        return
    print(f"Connected as {nick}.")
    agent = RefereeAgent(client, RefereeApiClient(server_url))
    try:
        agent.run()
    except KeyboardInterrupt:
        client.disconnect()


def run_server_referee_cli(server_url: str) -> None:
    cli = ServerRefereeCli(RefereeApiClient(server_url))
    cli.run()


def run_import_json(root: str, db_path: str) -> None:
    store = SQLiteRefereeStore(db_path)
    try:
        counts = import_json_store(root, store)
    finally:
        store.close()
    print(
        "Imported "
        f"{counts['rulepacks']} rulepacks, {counts['sessions']} sessions, {counts['events']} events "
        f"into {db_path}."
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="MyIrc — osu! AI referee and Bancho IRC client")
    subparsers = p.add_subparsers(dest="mode")

    server = subparsers.add_parser("server", help="run local HTTP + SQLite referee server")
    server.add_argument("--host", default="127.0.0.1", help="bind host")
    server.add_argument("--port", type=int, default=8765, help="bind port")
    server.add_argument("--db", default="referee.db", help="SQLite database path")
    server.add_argument("--import-json", default="", help="import existing rulepacks/sessions/logs before serving")

    agent = subparsers.add_parser("agent", help="run Bancho IRC referee agent")
    agent.add_argument("--nick", default=None, help="Your osu! username")
    agent.add_argument("--password", default=None, help="Your IRC server password")
    agent.add_argument("--server-url", default="http://127.0.0.1:8765", help="local referee server URL")

    referee = subparsers.add_parser("referee", help="run human referee CLI")
    referee.add_argument("--server-url", default="http://127.0.0.1:8765", help="local referee server URL")

    chat = subparsers.add_parser("chat", help="run legacy curses IRC chat client")
    chat.add_argument("--nick", default=None, help="Your osu! username")
    chat.add_argument("--password", default=None, help="Your IRC server password")

    importer = subparsers.add_parser("import-json", help="one-time import from JSON dirs into SQLite")
    importer.add_argument("--root", default=".", help="directory containing rulepacks/, sessions/, logs/")
    importer.add_argument("--db", default="referee.db", help="SQLite database path")

    p.set_defaults(mode="referee", server_url="http://127.0.0.1:8765")
    return p


def main() -> None:
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass

    args = build_parser().parse_args()
    if args.mode == "server":
        if args.import_json:
            store = SQLiteRefereeStore(args.db)
            try:
                counts = import_json_store(args.import_json, store)
            finally:
                store.close()
            print(
                "Imported "
                f"{counts['rulepacks']} rulepacks, {counts['sessions']} sessions, {counts['events']} events."
            )
        run_server(args.host, args.port, args.db)
    elif args.mode == "agent":
        nick, password = prompt_credentials(args)
        run_agent(nick, password, args.server_url)
    elif args.mode == "chat":
        nick, password = prompt_credentials(args)
        run_chat_tui(nick, password)
    elif args.mode == "import-json":
        run_import_json(args.root, args.db)
    else:
        run_server_referee_cli(args.server_url)


if __name__ == "__main__":
    main()
