# My-OsuIrc

一个连接 osu! Bancho (`irc.ppy.sh`) 的终端 AI 裁判。主入口是 **`agent` 模式：一个进程 = 一个比赛房间**——用机器人账号登录、自动主持 ban/pick / 计时 / 计分 / 判胜 / 关房，并自带本地 `/human` `/ai` 控制台供人工随时接管。另保留可选的本机服务端（存储/审计）和 curses IRC 聊天模式。

## 功能

- 连接 osu! Bancho IRC 服务器（`irc.ppy.sh:6667`），PASS 认证、自动 PING/PONG 保活
- **单进程自动裁判**：建房、`!mp set`、邀请、计时、roll 定序、ban/pick、按图池选图设 mod、读分判胜、播报、关房
- 完整 BO 赛制：可配置 BP 模板（ban 数量随模板）、TB 赛点门控、ban/pick/准备超时、暂停、投降、掉线中止、同分重赛
- **本地人工接管**：`/human` 暂停 AI、借房主连接直接发指令；`/ai` 交回
- **AI 助理（只读，需配 key）**：`!ref ask` 答疑、规则解释、图池层报错处理、赛后摘要；算分判胜永不交给 AI
- 每房间聊天双向落盘到 `logs/chat/`，便于排查
- 可选本机 HTTP + SQLite 服务端（多房间/审计）与 curses IRC 聊天界面（`chat` 模式）

## 项目结构

```
MyIrc/
├── main.py                         # 兼容入口：python main.py ...
├── config.example.json             # 第三方 AI 模型配置模板
├── my_osuirc/
│   ├── app.py                      # CLI 参数解析和模式分发（agent / server / chat / import-json）
│   ├── ai/
│   │   └── client.py               # OpenAI-compatible 客户端：规则抽取 + 答疑/裁决
│   ├── chat/
│   │   └── ui.py                   # curses TUI：消息区、状态栏、输入栏
│   ├── irc/
│   │   └── client.py               # IRC 协议层：连接、解析、收发、每房间聊天落盘
│   └── referee/
│       ├── core.py                 # 裁判状态机、BO/BP 引擎、图池表、消息路由、dataclass 模型
│       ├── agent.py                # 单房间执行端：IRC 自动裁判 + 本地 /human /ai 控制台 + AI 助理调度
│       ├── assistant.py            # AI 助理（只读）：答疑 / 裁决 / 赛后摘要
│       ├── server.py               # 可选本机 HTTP + SQLite 服务端（存储/审计/多房间）
│       ├── client.py               # ServerRefereeCli（服务端裁判 CLI，现仅测试用）
│       ├── api.py                  # 服务端 HTTP client
│       └── legacy_cli.py           # 旧单进程 supervisor（测试/兼容用）
└── tests/                          # 单元测试和集成式 fake IRC/server 测试
```

### 架构分层

| 层 | 目录 | 责任 |
|----|------|------|
| 入口层 | `main.py`, `my_osuirc/app.py` | `python main.py ...`，分发到 agent / server / chat / import-json |
| 通信层 | `my_osuirc/irc/` | IRC 协议、收发、每房间聊天落盘 |
| 裁判核心 | `my_osuirc/referee/core.py` | 确定性引擎：BO/BP 状态机、共享图池表、消息路由、算分判胜（不含 I/O 与 AI） |
| 执行端 | `my_osuirc/referee/agent.py` | 单房间：连 Bancho、跑引擎、本地 `/human` `/ai` 控制台、调度 AI 助理 |
| AI 层 | `my_osuirc/referee/assistant.py`, `my_osuirc/ai/client.py` | 只读：规则书抽取、答疑、模糊/报错裁决、摘要；结果经引擎/白名单校验 |
| 可选服务端 | `my_osuirc/referee/server.py` | SQLite 存储、HTTP API、排期与原子 claim（多房间/审计时用） |
| 测试 | `tests/` | 引擎规则、BP、AI 路由、SQLite/API、端到端回放 |

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

完整上手指南见 [docs/how-to-autoref.md](docs/how-to-autoref.md)；给房间内选手看的简版在 [docs/player-room-readme.md](docs/player-room-readme.md)。

**一条命令开一场（一个进程 = 一个房间，无需服务端）：**

```bash
python main.py agent \
  --nick YourBotName --password YourIrcPassword \
  --rulebook docs/sample-rulebook-otan-s1.json \
  --best-of 11 --red Alice --blue Bob
```

- `--red` / `--blue`：写 `队名=选手1,选手2`，1v1 直接写选手名；省略则交互式提示。
- `--best-of`：每场各自指定（BO9/BO11/BO13），规则书里不含 best-of；省略则提示输入。
- `--rulebook`：图池/规则的 JSON（mappool + format）。没有现成 JSON？用 `import-rulebook` 让 AI 从原始规则书/图池生成（见下）。
- 其它模式：`python main.py import-rulebook ...`（AI 解析规则书）、`python main.py chat ...`（curses 聊天）、`python main.py server ...`（可选服务端）、`python main.py import-json ...`（旧 JSON 导入）、`python main.py --origin ...`（原始全手动模式）。

### 导入规则书/图池（AI 解析，格式随便）

把人写的规则书 + 图池（**任意格式**：md / txt / TSV / 复制粘贴都行）交给 AI 解析成引擎能懂的结构化 JSON，人工确认后保存：

```bash
python main.py import-rulebook --rules rule.txt --mappool mappool.txt --name "o!TA:N S1" --out my-rulebook.json
```

AI 会抽出 `mappool`（每张图的 `code`/`beatmap_id`/`mods`/`map_command`/`mod_command`）和 `format`（`team_mode`/`win_condition`/`bp_order`）以及计时字段；屏幕打印结果让你确认（`-y` 跳过确认）。确认后存成 JSON，直接 `agent --rulebook my-rulebook.json` 用。需要配 AI key（见 `config.json` 段）。

### 原始全手动模式（`--origin`）

不想要任何自动化、像最初那样自己登录、自己 `!mp make`、自己一条条敲指令裁判：

```bash
python main.py --origin --nick YourName --password YourIrcPassword
```

这就是 curses IRC 客户端（等同 `chat` 模式）：`/join #channel`、直接打字发言、手动 `!mp ...`。

IRC 密码获取：登录 [osu!](https://osu.ppy.sh) → Settings → Legacy IRC。

### 开比赛流程

| 步骤 | 操作 |
|------|------|
| 1 | 准备一份图池/规则 JSON（示例 `docs/sample-rulebook-otan-s1.json`，含 `mappool` + `format.bp_order`） |
| 2 | `python main.py agent --nick ... --password ... --rulebook ... --best-of 11 --red ... --blue ...` |
| 3 | 进程自动 `!mp make` 建房 → `!mp set` → 邀请双方 → 计时 → 提示 `!roll` |
| 4 | 房间内选手 `!roll` 定序 → `!ref pick/ban first|last` 选先后手 → 轮流 `!ref pick/ban <编号>` |
| 5 | 双方 ready → 机器人 `!mp settings` + `!mp start`；读分判胜、播报；先到 `first_to` → `!mp close` |
| 6 | 需要人工介入时，在 agent 终端 `/human` 接管（直接发指令），`/ai` 交回 |

### 本地控制台命令（agent 终端）

| 命令 | 说明 |
|------|------|
| `/human` | 人工接管：AI 立即停止自动发送；之后你打字作为房主直接发进房间 |
| `/ai` | 交回 AI 自动裁判 |
| `/state` | 查看当前阶段 / 比分 / 轮到谁 / 已 ban / TB 是否解锁 |
| `/quit` | 退出（断开连接） |

控制台是带**可滚动历史**的界面（房间消息与机器人动作实时显示）：↑/↓ 滚一行、PgUp/PgDn 翻页、Home/End 到顶/底；向上回看时新消息不会打断。
人工与 AI 共用同一个 IRC 连接（房主本人），接管无需第二个账号、无需 `!mp addref`。

### 可选服务端 HTTP API

`python main.py server` 提供 SQLite 存储与 HTTP API（多房间/审计场景），默认监听 `http://127.0.0.1:8765`：

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

### Best-of 赛制(BO / ban-pick)

**best-of 在开房时指定**（`agent` 的 `--best-of`，或服务端 `POST /api/sessions` 传 `best_of`）。
同一份规则书/图池可以用在 BO9 / BO11 / BO13 不同场次——赛制是「这场比赛」的属性，不写进规则书。
指定了 `best_of` 时，AI 进入完整 BO 赛制模式（开房/邀请/计时与默认规则一致，之后接管选图、比分、判胜、关房）：

- 队长用 `!ref pick <code>` / `!ref ban <code>` 选图/ban 图，`code` 对应规则书 `mappool` 里的图编号（NM1/HD2/DT3/TB…）；AI 直接发图池里写好的 `map_command` 和 `mod_command`。
- 玩家 ready（或系统 `All players are ready`）后，AI 执行 `!mp settings` 再 `!mp start`。
- 每张图打完，AI 读 BanchoBot 的 `... finished playing (Score: N)` 与 `The match has finished!`，按 ScoreV2 比分判该图胜方并累计比分，播报 `Red | r - b | Blue // Best of N - next pick/ban`。
- 任一队先到 `first_to`（默认 `best_of // 2 + 1`，BO11 即 6）时，播报 `... wins! GGWP` 并自动 `!mp close`。
- 不在 `mappool` 的图（例如警身图）不计分；同分图不计分、提示重赛。

**规则书/图池是人写的纯文本（任意格式），由 AI 解析成结构化 rulepack**——用 `python main.py import-rulebook --rules ... --mappool ...`：
`OpenAICompatibleClient.extract_rulepack` 把文本抽成 `mappool`（每条含 `code` / `beatmap_id` / `mods` / `map_command` / `mod_command`）和 `format`（`team_mode`/`win_condition`/`bp_order`），
人工确认后保存为 JSON，`agent --rulebook` 直接加载（AI 抽取结果必须人工确认）。

`docs/rulebook-121270745.md` 是一份人类可读的示例规则书（含 Markdown 图池表）；
`tests/fixtures/mappool_121270745.txt` 是对应的 TSV 图池源；
`docs/sample-rulebook-otan-s1.json` 是确认后可直接 `--rulebook` 加载的结构化图池。

`tests/test_bo11_replay.py` 用一场真实 BO11 录像（`tests/fixtures/match_121270745.log`）做端到端回放：
导入图池（缓存的确认结果 `tests/fixtures/rulebook_121270745.golden.json`）、开房时指定 `best_of=11` 后，引擎复现整场比分 0-0 → 6-2、判胜并关房。
该测试不调用真实模型——抽取链路另有一个 stub 测试覆盖。

### 受管 ban/pick + 突发处理（rule.txt 第三/四/五/六章）

当规则书 `format` 含 **`bp_order`** 时，引擎进入受管 BP 模式（覆盖完整赛制；不配 `bp_order` 则保持上面的宽松 BO 流程）。
`bp_order` 是一个模板序列，例如 `["pick","ban","pick","ban"]`（pick/ban 交替）或 `["pick","pick","ban","ban"]`（先各 pick 再各 ban）；
**ban 数量随模板走**——有 2-ban 的图池就放两个 `"ban"`，无 ban 就不放。模板走完后剩余局继续按 pick 先后手交替，直到判胜。

流程与命令（均为 `!ref` 前缀，仅参赛队员有效）：

- **Roll 定序**：双方在房间内 `!roll`，引擎读 BanchoBot 的 `rolls N point(s)`；平局自动要求重 roll。
  roll 胜方用 `!ref pick first|last` 或 `!ref ban first|last` 选自己在某一环节的先后手，负方选另一环节。
- **Ban / Pick**：引擎按解析出的先后手广播「轮到谁」，只接受**当前该方**的 `!ref ban <code>` / `!ref pick <code>`；
  已 ban / 已打过的图不可再选，**不可 ban TB**，**TB 仅双方均到赛点后可 pick**（之前 pick 会被拒）。
- **时限**（可在规则书覆盖）：`pick_ban_timer` 60s（ban 超时＝弃权，pick 超时＝裁判按图池顺序代选）、
  `prep_timer` 120s / `tb_prep_timer` 180s（准备超时走 `!mp start`）。
- **暂停** `!ref pause`：每人 `pause_per_player`（默认 2）次、每次 `pause_timer`（默认 120s）；
  仅 ban/pick 或准备阶段可用、同一小局同一方不可重复；到点自动恢复。
- **投降** `!ref ff`：该方判负、对手赢下整场，自动播报并 `!mp close`。
- **掉线中止** `!ref abort`：开图 `abort_window`（默认 30s）内可中止，引擎发 `!mp abort` 并重开同一张图。
- **同分重赛**：某图双方同分自动不计分、重开该图。

时限/暂停/中止依赖墙钟，由 agent 主循环每轮调用 `RefereeEngine.tick_timeouts(session)` 驱动（测试用注入的 `now` 保证确定性）。
完整规则书正文见 `rule.txt`，图池见 `mappool.txt`；受管 BP 引擎的单元测试在 `tests/test_bp_engine.py`。

> 暂未实现：rule.txt 第六章 6 的「单图重赛权（每人 1 次）」与 Bracket Reset 的首 ban/pick 特殊约束。

### AI 用在哪

AI 是一个**只读的理解/判断层**，绝不参与算分判胜、ban/pick 合法性或发 `!mp`（这些由确定性引擎保证）：

1. **规则书/图池解析**：`extract_rulepack` 把人写的 md/TSV 解析成结构化 rulepack（人工确认后入库）。
2. **房间内答疑**：选手 `!ref ask <问题>`，AI 拿「引擎算好的状态快照 + 规则书原文」回答；`!ref ?` 给确定性状态行。
3. **AI 裁判控制器（受限自动）**：选手用 `!ref <自由描述>` 提出规则书没覆盖/模糊/突发的情况，AI 依「状态 + 近期房间记录 + 规则书」裁决并产出一条 `!mp` 指令；白名单指令（`timer/settings/map/mods/start/invite`）自动发，破坏性/影响比分的（`abort/close/踢人`）扣住等人工确认。
4. **赛后摘要**：一场结束后把房间聊天日志总结到 `logs/chat/mp_<房号>.summary.md`。

模式：**LLM 提议/裁决 → 确定性引擎与白名单闸门校验 → 没配 key / 调用失败时回落到确定性行为**。运行时 AI 调用都在后台线程，不阻塞裁判循环；算分、判胜、合法性永不交给 AI。

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
python3 main.py agent --help
python3 main.py server --help
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
