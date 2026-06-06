"""MyIrc — osu! Bancho IRC client and AI referee system.

Usage:
    python main.py agent --nick NICK --password PASS --rulebook RB.json --best-of 11
    python main.py import-rulebook --rules rule.txt --mappool mappool.txt --out RB.json
    python main.py server --host 127.0.0.1 --port 8765
    python main.py chat --nick NICK --password PASS
    python main.py --origin --nick NICK --password PASS   # 原始全手动模式

If not provided, nick and password will be prompted in the terminal for IRC modes.
"""

import argparse
import curses
import getpass
import json
import locale
import re
import sys
import threading
import time
from collections import deque
from pathlib import Path

if sys.platform == "win32":
    import msvcrt

from my_osuirc.ai.client import OpenAICompatibleClient
from my_osuirc.chat.ui import ChatUI
from my_osuirc.irc.client import IrcClient
from my_osuirc.referee.agent import RefereeAgent
from my_osuirc.referee.assistant import RefereeAssistant, extract_rulebook
from my_osuirc.referee.core import (
    RefereeSession,
    SessionConfig,
    SessionState,
    Team,
    new_id,
    rulepack_from_draft,
    slugify,
)
from my_osuirc.referee.server import SQLiteRefereeStore, import_json_store, run_server

DEFAULT_RULEBOOK = "docs/sample-rulebook-otan-s1.json"

SERVER = "irc.ppy.sh"
PORT = 6667

MP_ROOM_RE = re.compile(r"https://osu\.ppy\.sh/mp/(\d+)")


def chat_main(stdscr: curses.window, nick: str, password: str, chat_log_dir: str | None = "logs/chat"):
    client = IrcClient(nick=nick, password=password, server=SERVER, port=PORT, chat_log_dir=chat_log_dir)

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


def run_chat_tui(nick: str, password: str, chat_log_dir: str | None = "logs/chat") -> None:
    try:
        curses.wrapper(lambda stdscr: chat_main(stdscr, nick, password, chat_log_dir))
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"\nError: {e}")
        input("Press Enter to exit...")


def _resolve_teams(rulepack, red: str | None, blue: str | None) -> list[Team]:
    """Build teams from --red/--blue flags ('Name=p1,p2' or just 'player') or prompts."""
    labels = (rulepack.team_template or ["red", "blue"])[:2]
    specs = [red, blue]
    teams: list[Team] = []
    for index, label in enumerate(labels):
        spec = specs[index] if index < len(specs) else None
        if not spec:
            name = input(f"{label} 队名/选手名: ").strip() or label
            players_text = input(f"{name} 选手(逗号分隔, 留空=同队名): ").strip() or name
        else:
            name, sep, players_text = spec.partition("=")
            name = name.strip() or label
            players_text = players_text if sep else name
        players = [p.strip() for p in players_text.split(",") if p.strip()]
        teams.append(Team(name=name, players=players))
    return teams


def run_agent(
    nick: str,
    password: str,
    rulebook: str = DEFAULT_RULEBOOK,
    best_of: int = 0,
    red: str | None = None,
    blue: str | None = None,
    chat_log_dir: str | None = "logs/chat",
    rules_file: str = "rule.txt",
) -> None:
    """Merged single-room operator: one process = one room. The AI auto-referees;
    /human pauses it for manual takeover, /ai resumes."""
    rulebook_path = Path(rulebook)
    if not rulebook_path.exists():
        print(f"Rulebook not found: {rulebook}")
        return
    draft = json.loads(rulebook_path.read_text(encoding="utf-8"))
    rulepack = rulepack_from_draft(name=str(draft.get("name") or "Rulebook"), draft=draft, confirmed=True)

    teams = _resolve_teams(rulepack, red, blue)
    if not any(team.players for team in teams):
        print("需要至少一名选手。")
        return
    if best_of <= 0:
        try:
            best_of = int(input("best of (e.g. 11): ").strip() or "11")
        except ValueError:
            best_of = 11

    match_name = " vs ".join(team.name for team in teams) or "Match"
    config = SessionConfig(
        id=new_id("session", match_name), name=match_name, rulepack_id=rulepack.id, teams=teams, best_of=best_of
    )
    state = SessionState(session_id=config.id, stage="scheduled", score={team.name: 0 for team in teams})
    session = RefereeSession(config=config, rulepack=rulepack, state=state)

    rules_text = ""
    if rules_file and Path(rules_file).exists():
        rules_text = Path(rules_file).read_text(encoding="utf-8", errors="replace")
    assistant = RefereeAssistant(OpenAICompatibleClient.from_env(), rules_text=rules_text)

    client = IrcClient(nick=nick, password=password, server=SERVER, port=PORT, chat_log_dir=chat_log_dir)
    print(f"Connecting to {SERVER}:{PORT} ...")
    try:
        client.connect()
    except Exception as e:
        print(f"Connection failed: {e}")
        return
    print(
        f"Connected as {nick}. Match: {match_name} (BO{best_of}). "
        f"AI assistant: {'on' if assistant.enabled else 'off'}."
    )
    agent = RefereeAgent(client, api=None, assistant=assistant)
    agent.sessions[session.id] = session

    try:
        curses.wrapper(lambda stdscr: _agent_console(stdscr, agent, client))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"\nConsole error: {exc}")
    finally:
        agent.running = False
        client.disconnect()


def _agent_console(stdscr: curses.window, agent: RefereeAgent, client: IrcClient) -> None:
    """Scrollable curses console for the merged agent: a history pane you can
    scroll (↑/↓/PgUp/PgDn/Home/End) plus an input line for /human /ai /state ..."""
    ui = ChatUI(stdscr)
    view = "REF"
    ui.set_status(view, client.nick, force_redraw=True)
    agent.output = lambda line: ui.add_message(view, line, redraw=False)
    ui.add_message(view, "控制台：/human 接管 | /ai 交回 | /state 状态 | /quit 退出", redraw=False)
    ui.add_message(view, "滚动历史：↑/↓ 行 · PgUp/PgDn 翻页 · Home/End 顶/底（向上滚动时新消息不会打断）", redraw=False)
    agent.running = True

    def submit() -> None:
        line = ui.input_buffer
        ui.input_buffer = ""
        if line.strip():
            agent.handle_console(line)

    def handle_key(ch) -> None:
        if isinstance(ch, str):
            if ch in ("\r", "\n"):
                submit()
            elif ch in ("\x7f", "\x08", "\b"):
                ui.input_buffer = ui.input_buffer[:-1]
            elif ch.isprintable():
                ui.input_buffer += ch
            return
        if ch == curses.KEY_ENTER:
            submit()
        elif ch in (curses.KEY_BACKSPACE, curses.KEY_DC):
            ui.input_buffer = ui.input_buffer[:-1]
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

    ui._draw_messages()
    while agent.running and client._running:
        before = len(ui.messages)
        agent.step()
        keyed = False
        while True:
            try:
                ch = stdscr.get_wch()
            except curses.error:
                break
            handle_key(ch)
            keyed = True
        if keyed or len(ui.messages) != before:
            mode = "AI 自动" if agent.auto else "人工接管"
            ui.set_status(f"{view}  {mode}", client.nick)
            ui._draw_messages()
        time.sleep(0.05)


def run_import_rulebook(
    rules: str | None,
    mappool: str | None,
    name: str,
    out: str | None,
    assume_yes: bool = False,
    client=None,
    input_func=input,
    output_func=print,
) -> str | None:
    """Feed raw rulebook/mappool text (any format) → AI parses it into a structured
    rulepack JSON the engine + AI understand → human confirms → save for --rulebook."""
    rules_text = Path(rules).read_text(encoding="utf-8", errors="replace") if rules and Path(rules).exists() else ""
    mappool_text = (
        Path(mappool).read_text(encoding="utf-8", errors="replace") if mappool and Path(mappool).exists() else ""
    )
    if rules and not rules_text:
        output_func(f"Rules file not found: {rules}")
        return None
    if mappool and not mappool_text:
        output_func(f"Mappool file not found: {mappool}")
        return None
    if not rules_text and not mappool_text:
        output_func("请用 --rules 和/或 --mappool 指定规则书/图池文件（任意格式）。")
        return None

    client = client or OpenAICompatibleClient.from_env()
    if client is None:
        output_func("需要配置 AI（config.json 或 AI_API_KEY）才能解析规则书；见 README 的 config.json 段。")
        return None

    output_func("AI 正在把规则书/图池解析成结构化格式...")
    try:
        data = extract_rulebook(client, name=name, rules_text=rules_text, mappool_text=mappool_text)
    except Exception as exc:
        output_func(f"解析失败：{exc}")
        return None

    mappool_list = data.get("mappool") or []
    fmt = data.get("format") or {}
    output_func(f"解析结果：{len(mappool_list)} 张图 | bp_order={fmt.get('bp_order')} | "
                f"{fmt.get('team_mode')}/{fmt.get('win_condition')}")
    for entry in mappool_list:
        output_func(f"  {entry.get('code')}: {entry.get('map_command')}  {entry.get('mod_command')}")

    if not assume_yes:
        answer = input_func("确认无误并保存？[y/N]: ").strip().lower()
        if answer != "y":
            output_func("已取消，未保存。")
            return None

    data["confirmed"] = True
    out = out or f"rulebook-{slugify(name)}.json"
    Path(out).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output_func(f"已保存 {out}。开赛：python main.py agent --nick ... --password ... --rulebook {out} --best-of 11")
    return out


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
    # Original (main-branch) fully-manual mode: log in and drive the room yourself
    # with the curses IRC client — no automation. `python main.py --origin ...`
    p.add_argument("--origin", action="store_true", help="原始手动模式：登录后用 curses 客户端自己开房、自己裁（无任何自动化）")
    p.add_argument("--nick", default=None, help="osu! username (for --origin)")
    p.add_argument("--password", default=None, help="IRC password (for --origin)")
    p.add_argument("--chat-log-dir", default="logs/chat", help="per-room chat log dir (for --origin; empty to disable)")
    subparsers = p.add_subparsers(dest="mode")

    server = subparsers.add_parser("server", help="run local HTTP + SQLite referee server")
    server.add_argument("--host", default="127.0.0.1", help="bind host")
    server.add_argument("--port", type=int, default=8765, help="bind port")
    server.add_argument("--db", default="referee.db", help="SQLite database path")
    server.add_argument("--import-json", default="", help="import existing rulepacks/sessions/logs before serving")

    agent = subparsers.add_parser("agent", help="run one room: AI auto-referee + local /human /ai console")
    agent.add_argument("--nick", default=None, help="Your osu! username (this account hosts the room)")
    agent.add_argument("--password", default=None, help="Your IRC server password")
    agent.add_argument("--rulebook", default=DEFAULT_RULEBOOK, help="rulepack JSON (mappool + format)")
    agent.add_argument("--best-of", type=int, default=0, help="best-of for this match (prompted if omitted)")
    agent.add_argument("--red", default=None, help="red team as 'Name=p1,p2' or just a player name")
    agent.add_argument("--blue", default=None, help="blue team as 'Name=p1,p2' or just a player name")
    agent.add_argument("--chat-log-dir", default="logs/chat", help="per-room chat log dir (empty to disable)")
    agent.add_argument("--rules-file", default="rule.txt", help="rulebook text for the AI assistant (Q&A grounding)")

    chat = subparsers.add_parser("chat", help="run legacy curses IRC chat client")
    chat.add_argument("--nick", default=None, help="Your osu! username")
    chat.add_argument("--password", default=None, help="Your IRC server password")
    chat.add_argument("--chat-log-dir", default="logs/chat", help="per-room chat log dir (empty to disable)")

    rb = subparsers.add_parser("import-rulebook", help="AI-parse a raw rulebook/mappool (any format) into a --rulebook JSON")
    rb.add_argument("--rules", default=None, help="rules text file (any format; e.g. rule.txt)")
    rb.add_argument("--mappool", default=None, help="mappool text file (any format; e.g. mappool.txt)")
    rb.add_argument("--name", default="Imported Rulebook", help="rulepack name")
    rb.add_argument("--out", default=None, help="output JSON path (default rulebook-<name>.json)")
    rb.add_argument("-y", "--yes", action="store_true", help="skip the confirm prompt")

    importer = subparsers.add_parser("import-json", help="one-time import from JSON dirs into SQLite")
    importer.add_argument("--root", default=".", help="directory containing rulepacks/, sessions/, logs/")
    importer.add_argument("--db", default="referee.db", help="SQLite database path")

    return p


def main() -> None:
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass

    args = build_parser().parse_args()
    if getattr(args, "origin", False):
        nick, password = prompt_credentials(args)
        run_chat_tui(nick, password, args.chat_log_dir or None)
        return
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
        run_agent(
            nick,
            password,
            rulebook=args.rulebook,
            best_of=args.best_of,
            red=args.red,
            blue=args.blue,
            chat_log_dir=args.chat_log_dir or None,
            rules_file=args.rules_file,
        )
    elif args.mode == "chat":
        nick, password = prompt_credentials(args)
        run_chat_tui(nick, password, args.chat_log_dir or None)
    elif args.mode == "import-rulebook":
        run_import_rulebook(args.rules, args.mappool, args.name, args.out, assume_yes=args.yes)
    elif args.mode == "import-json":
        run_import_json(args.root, args.db)
    else:
        build_parser().print_help()


if __name__ == "__main__":
    main()
