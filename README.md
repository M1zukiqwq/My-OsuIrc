# My-OsuIrc

一个连接 osu! Bancho (`irc.ppy.sh`) 的终端工具。现在拆成三端：本机服务端、Bancho 裁判 Agent、人工裁判 CLI；同时保留原来的 curses IRC 聊天模式。

## 功能

- 连接 osu! Bancho IRC 服务器（`irc.ppy.sh:6667`）
- 服务器密码认证（PASS）
- 加入/离开频道
- 收发消息、私聊
- 自动 PING/PONG 保活
- 本机 HTTP + SQLite 服务端：规则模板、比赛排期、session 状态、事件日志的唯一事实源
- Bancho 裁判 Agent：轮询服务端，到点自动建房、邀请、timer、settings、start
- 人工裁判 CLI：创建配置、排期、查看状态、人工接管/交回 AI
- 终端 TUI 聊天界面（curses，`chat` 模式）

## 项目结构

```
MyIrc/
├── main.py                         # 兼容入口：python main.py ...
├── config.example.json             # 第三方 AI 模型配置模板
├── my_osuirc/
│   ├── app.py                      # CLI 参数解析和四种模式分发
│   ├── ai/
│   │   └── client.py               # OpenAI-compatible 规则抽取客户端
│   ├── chat/
│   │   └── ui.py                   # curses TUI：消息区、状态栏、输入栏
│   ├── irc/
│   │   └── client.py               # IRC 协议层：连接、解析、收发
│   └── referee/
│       ├── core.py                 # 裁判状态机、默认规则、dataclass 模型
│       ├── server.py               # 本机 HTTP + SQLite 服务端
│       ├── agent.py                # Bancho IRC 裁判 Agent
│       ├── client.py               # 人工裁判 CLI（调用服务端 API）
│       ├── api.py                  # 服务端 HTTP client
│       └── legacy_cli.py           # 旧单进程 supervisor（测试/兼容用）
└── tests/                          # 单元测试和集成式 fake IRC/server 测试
```

### 架构分层

| 层 | 目录 | 责任 |
|----|------|------|
| 入口层 | `main.py`, `my_osuirc/app.py` | 保持 `python main.py ...` 使用方式，并分发到 server / agent / referee / chat |
| 通信层 | `my_osuirc/irc/`, `my_osuirc/referee/api.py` | IRC 协议和本机 HTTP API client |
| 裁判核心 | `my_osuirc/referee/core.py` | 默认规则、状态机、session/rulepack 模型、低风险命令生成 |
| 服务端 | `my_osuirc/referee/server.py` | SQLite 唯一事实源、HTTP API、排期和原子 claim |
| 执行端 | `my_osuirc/referee/agent.py` | 轮询服务端，连接 Bancho，执行自动裁判动作 |
| 人工端 | `my_osuirc/referee/client.py` | 创建规则/比赛、查看状态、人工接管和交回 AI |
| AI 辅助 | `my_osuirc/ai/client.py` | 规则书/图池抽取；所有结果仍需人工确认 |
| 测试 | `tests/` | 核心规则、UI/IRC、SQLite/API/Agent/CLI 流程验证 |

## 环境要求

- Python 3.10+
- 无第三方依赖（SQLite、HTTP、IRC、curses 均使用 Python 标准库）

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
# 1. 启动本机服务端（默认 127.0.0.1:8765，数据库 referee.db）
python main.py server --host 127.0.0.1 --port 8765

# 2. 启动 Bancho 裁判 Agent
python main.py agent --nick YourOsuName --password YourIrcPassword --server-url http://127.0.0.1:8765

# 3. 启动人工裁判 CLI
python main.py referee --server-url http://127.0.0.1:8765

# 旧 IRC/TUI 聊天模式
python main.py chat --nick YourOsuName --password YourIrcPassword

# 一次性把旧 JSON 目录导入 SQLite
python main.py import-json --root . --db referee.db
```

IRC 密码获取：登录 [osu!](https://osu.ppy.sh) → Settings → IRC 密码

### 开比赛流程

| 步骤 | 入口 | 操作 |
|------|------|------|
| 1 | `server` | 先启动 `python main.py server`，它保存规则、比赛、状态和日志 |
| 2 | `agent` | 启动 `python main.py agent ...`，Agent 会轮询服务端并等待可领取比赛 |
| 3 | `referee` | 用 `add-config` 添加规则模板；规则草案必须人工确认 |
| 4 | `referee` | 用 `new` 创建比赛，填写规则模板、队伍、队员、比赛时间 |
| 5 | `server/agent` | 默认比赛前 10 分钟，Agent 自动领取 session 并开 mp 房 |
| 6 | `agent` | Agent 自动执行 `!mp make`、`!mp invite`、上人 timer、BP timer |
| 7 | 房间内 | 玩家 `!ref ready` 只触发 `!mp settings`；系统 all-ready 或上人 timer 到期后 `!mp start 7` |
| 8 | `referee` | 需要人工介入时 `#join <session_id|#mp_room>`；输入 `/ai` 或 `/leave` 交回 AI |

### 人工裁判 CLI 命令

| 命令 | 说明 |
|------|------|
| `list` | 列出服务端中的 scheduled / active / paused / human_controlled / finished session |
| `#join <session_id|#mp_room>` | 人工接管某场比赛；该 session 的 Agent 自动动作暂停 |
| `new` | 基于已有规则模板创建新的小比赛 |
| `add-config` | 添加新规则模板，支持 AI 从规则书/图池链接抽取 |
| `resume <session_id>` | 恢复 AI 接管 |
| `state <session_id>` | 查看比赛状态、比分、频道、Agent 分配 |
| `quit` | 退出人工 CLI，不会关闭服务端或 Agent |

### HTTP API

服务端默认监听 `http://127.0.0.1:8765`：

- `GET /api/health`
- `GET /api/rulepacks`
- `POST /api/rulepacks/draft`
- `POST /api/rulepacks/{id}/confirm`
- `GET /api/sessions`
- `POST /api/sessions`
- `GET /api/sessions/{id}`
- `POST /api/sessions/{id}/control`
- `POST /api/sessions/{id}/events`
- `POST /api/agent/heartbeat`
- `POST /api/agent/claim`
- `GET /api/agent/{agent_id}/tasks`

如果规则书未导入或缺少字段，会使用默认规则：

- BP timer：90 秒
- 上人/入房 timer：120 秒
- 玩家发送 `!ref ready` 后，AI 只会执行 `!mp settings` 检查房间配置
- BanchoBot / SYSTEM 消息确认所有玩家已 ready 后：`!mp start 7`
- 上人 timer 结束且没有玩家发送 `!ref pause` 后：`!mp start 7`

AI 裁判只识别 BanchoBot / SYSTEM 等系统消息，以及玩家以 `!ref` 开头的命令；普通玩家聊天不会触发 AI 判断或自动回复。

第三方 AI 模型配置放在 `config.json`：

```json
{
  "ai": {
    "base_url": "https://api.openai.com",
    "api_key": "YOUR_API_KEY",
    "model": "gpt-4.1-mini",
    "thinking": {
      "type": "disabled"
    }
  }
}
```

仓库内提供了 `config.example.json` 模板；真实 `config.json` 已加入 `.gitignore`，避免提交 API key。

也可以用环境变量覆盖配置文件：

```bash
export AI_API_KEY=...
export AI_BASE_URL=https://api.openai.com
export AI_MODEL=gpt-4.1-mini
```

### 开发验证

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q main.py my_osuirc tests
python3 main.py server --help
python3 main.py agent --help
python3 main.py referee --help
```

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
