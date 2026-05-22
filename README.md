# My-OsuIrc

一个连接 osu! Bancho (`irc.ppy.sh`) 的极简 IRC 终端客户端，使用 Python 标准库实现，无第三方依赖。

## 功能

- 连接 osu! Bancho IRC 服务器（`irc.ppy.sh:6667`）
- 服务器密码认证（PASS）
- 加入/离开频道
- 收发消息、私聊
- 自动 PING/PONG 保活
- 终端 TUI 界面（curses）

## 项目结构

```
MyIrc/
├── irc.py     # IRC 协议层：连接、消息解析、命令发送
├── ui.py      # curses TUI：消息区 + 状态栏 + 输入栏
├── main.py    # 入口：参数解析、事件循环、命令路由
└── start.bat  # Windows 快捷启动脚本
```

## 环境要求

- Python 3.10+
- 无第三方依赖（仅使用标准库 `socket`, `threading`, `curses`, `getpass`）

### Mac / Linux

macOS 和绝大多数 Linux 发行版自带 Python 3 和 curses 支持，无需额外配置。

```bash
# 确认 Python 版本
python3 --version   # 需要 >= 3.10

# 确认 curses 可用
python3 -c "import curses; print('OK')"
```

如果系统没有 Python 3：

```bash
# macOS (Homebrew)
brew install python3

# Ubuntu / Debian
sudo apt install python3

# Arch
sudo pacman -S python
```

### Windows

**Windows 的 Python 不自带 curses 模块**，需要额外安装 `windows-curses` 包。

**步骤：**

1. 从 [python.org](https://www.python.org/downloads/) 安装 Python 3.10+（**安装时勾选 "Add Python to PATH"**）

2. 安装 `windows-curses`：

```cmd
pip install windows-curses
```

3. 确认可用：

```cmd
python -c "import curses; print('OK')"
```

**注意：** 如果系统同时存在 Microsoft Store 版本的 Python（`WindowsApps\python.exe`），它可能优先于你安装的版本被调用，导致 `windows-curses` 找不到。解决方法：

- 方法一：调整 PATH 环境变量，把 Python311 的路径移到 `WindowsApps` 前面
- 方法二：使用完整路径运行，或直接双击 `start.bat`

```cmd
"C:\Users\<你的用户名>\AppData\Local\Programs\Python\Python311\python.exe" main.py --nick 你的用户名 --password 你的密码
```

## 使用方法

```bash
# 交互式输入用户名密码
python main.py

# 或直接传参
python main.py --nick YourOsuName --password YourIrcPassword
```

IRC 密码获取：登录 [osu!](https://osu.ppy.sh) → Settings → IRC 密码

### TUI 内命令

| 命令 | 说明 |
|------|------|
| `/join #channel` | 加入频道 |
| `/msg 用户名 内容` | 私聊 |
| `/nick 新昵称` | 改名 |
| `/quit` | 退出 |

在已加入频道的状态下，直接输入文字即可发送到当前频道。

## License

MIT
