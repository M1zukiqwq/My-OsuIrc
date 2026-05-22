"""MyIrc — a minimal IRC client for osu! Bancho (irc.ppy.sh).

Usage:
    python main.py [--nick NICK] [--password PASS]

If not provided, nick and password will be prompted in the terminal.
"""

import argparse
import curses
import getpass
import sys

from irc import IrcClient
from ui import ChatUI

SERVER = "irc.ppy.sh"
PORT = 6667


def main(stdscr: curses.window, nick: str, password: str):
    client = IrcClient(nick=nick, password=password, server=SERVER, port=PORT)

    ui = ChatUI(stdscr)

    def on_new_message():
        pass

    client.on_message = on_new_message

    ui.add_message("SYSTEM", f"Connecting to {SERVER}:{PORT} ...")
    try:
        client.connect()
    except Exception as e:
        ui.add_message("SYSTEM", f"Connection failed: {e}")
        ui.read_input()
        return

    ui.add_message("SYSTEM", f"Connected as {nick}. Type /join #channel to join.")

    current_channel = ""

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
                        current_channel = channel.lstrip("#")
                        ui.set_status(f"#{current_channel}", client.nick)
                        ui.add_message("SYSTEM", f"Joining #{current_channel} ...")
                    else:
                        ui.add_message("SYSTEM", "Usage: /join #channel")

                case "/nick":
                    new_nick = arg.strip()
                    if new_nick:
                        client._send_raw(f"NICK {new_nick}")
                    else:
                        ui.add_message("SYSTEM", "Usage: /nick newname")

                case "/quit":
                    client.disconnect()
                    raise SystemExit

                case "/msg":
                    parts2 = arg.split(" ", 1)
                    if len(parts2) == 2:
                        target, text = parts2
                        client.send(target, text)
                        ui.add_message(target, f"<{client.nick}> {text}")
                    else:
                        ui.add_message("SYSTEM", "Usage: /msg target text")

                case _:
                    ui.add_message("SYSTEM", f"Unknown command: {cmd}")

        elif current_channel:
            client.send(f"#{current_channel}", line)
            ui.add_message(f"#{current_channel}", f"<{client.nick}> {line}")
        else:
            ui.add_message("SYSTEM", "Join a channel first with /join #channel")

    # Main loop: drain message queue + read input
    while client._running:
        while client.message_queue:
            tag, text = client.message_queue.popleft()
            ui.add_message(tag, text)

        ui.set_status(f"#{current_channel}" if current_channel else "SYSTEM", client.nick)

        stdscr.nodelay(True)
        stdscr.timeout(100)

        try:
            curses.echo()
            stdscr.move(ui.input_y, 2)
            stdscr.clrtoeol()
            raw = stdscr.getstr(ui.input_y, 2, ui.width - 3)
            curses.noecho()
            line = raw.decode("utf-8", errors="replace")
            handle_command(line)
        except curses.error:
            curses.noecho()

        while client.message_queue:
            tag, text = client.message_queue.popleft()
            ui.add_message(tag, text)

    while client.message_queue:
        tag, text = client.message_queue.popleft()
        ui.add_message(tag, text)

    ui.add_message("SYSTEM", "Press Enter to exit.")
    stdscr.nodelay(False)
    stdscr.timeout(-1)
    try:
        stdscr.getstr()
    except curses.error:
        pass


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
