# 从零自动裁判一场 BO11（基于本图池 + 规则书）

本指南用仓库自带的图池/规则书把一场比赛跑起来，AI 全自动主持 ban/pick、计时、计分、判胜、关房。
重赛 / 突发争议交给真人裁判处理（见最后「人工接管」）。

## 0. 准备

- Python 3.10+（无第三方依赖）。
- 一个 **osu! 账号给机器人用**：它会用这个号登录 Bancho 并 `!mp make` 建房，所以这个号就是房主/裁判，**人工接管也是借这个号的连接**（同一进程，无需第二个账号或 addref）。
  - IRC 密码：osu! 官网 → Settings → Legacy IRC 里拿（不是登录密码）。
- 参赛选手的 **准确 osu! 用户名**（机器人按名字 `!mp invite`）。
- 规则书/图池已经在仓库里：`docs/sample-rulebook-otan-s1.json`
  （= `mappool.txt` 的 18 张图 + `rule.txt` 的赛制参数 + `bp_order`）。
- 想用**自己的**规则书/图池：用 `import-rulebook` 让 AI 把任意格式的文本解析成这种 JSON（见下面「导入自己的规则书」）。

### 导入自己的规则书（可选，AI 解析）

```bash
python main.py import-rulebook --rules rule.txt --mappool mappool.txt --name "我的杯赛" --out my-rulebook.json
```

规则书/图池**格式随便**（md/txt/TSV/粘贴）。AI 解析出 `mappool` + `format`（含 `bp_order`、`team_mode`、`win_condition`）和计时，屏幕打印让你确认（`-y` 跳过），存成 `my-rulebook.json`，之后 `agent --rulebook my-rulebook.json`。需要配 AI key。

## 1. 一条命令开一场（一个进程 = 一个房间）

不需要单独的服务端或裁判端——`agent` 自己建房、自动裁判，并自带本地 `/human` `/ai` 控制台。

```bash
python main.py agent \
  --nick <机器人osu用户名> --password <IRC密码> \
  --rulebook docs/sample-rulebook-otan-s1.json \
  --best-of 11 --red Alice --blue Bob
```

- `--red` / `--blue` 可写 `队名=选手1,选手2`；1v1 直接写选手名（队名=选手名，比分播报就显示成 `Alice | 0 - 0 | Bob`）。不带这些参数时会**交互式提示**。
- `--best-of` 每场各自定（BO9 填 9、决赛 BO13 填 13）；不带会提示输入。规则书里**不含** best-of。
- 想换图池/赛制：换 `--rulebook` 指向别的 JSON，或编辑该文件的 `format.bp_order` / `mappool`。

连上后控制台长这样（左侧标着当前是 AI 还是人工）：

```
Connected as <bot>. Match: Alice vs Bob (BO11). AI assistant: on.
控制台：/human 接管 | /ai 交回 | /state 状态 | /quit 退出
[AI] >
```

## 2. 之后全自动

进程启动后自动执行：

```
!mp make <房名>                        # 建房（机器人是房主）
!mp set 2 3 3                          # TeamVs / ScoreV2 / 3 slots（来自规则书 format）
!mp invite Alice / !mp invite Bob      # 邀请双方
!mp timer 120 / !mp timer 90           # 入房/BP 计时
（房间内）请双方 !roll 决定 ban/pick 先后手
```

选手在房间里依次操作（机器人只认 BanchoBot 系统消息和 `!ref` 开头的指令，普通聊天忽略）：

1. 双方各自 `!roll`。
2. roll 大的一方：`!ref pick first` / `!ref pick last` / `!ref ban first` / `!ref ban last`（选自己在某环节的先后手）。
3. 另一方：对**另一个**环节 `!ref pick/ban first|last`。
4. 机器人广播「轮到谁 ban/pick」，当前方发 `!ref ban <编号>` 或 `!ref pick <编号>`（编号见图池表，如 `NM1`/`HR2`/`DT3`/`TB`）。
5. 选好图后双方按 Ready；机器人 `!mp settings` 确认后 `!mp start 7`。
6. 一局结束机器人读分判胜、播报 `Alice | x - y | Bob // Best of 11 - next ...`。
7. 某方先到 6 → 播报 `... wins! GGWP` 并 `!mp close`。

### 选手 `!ref` 指令速查

| 指令 | 作用 |
|------|------|
| `!ref pick <编号>` | 选图（轮到你时） |
| `!ref ban <编号>` | ban 图（轮到你时） |
| `!ref ban skip` | 放弃本次 ban |
| `!ref pick first/last`、`!ref ban first/last` | roll 后选先后手 |
| `!ref pause` | 暂停（每人 2 次、每次 120s，仅 ban/pick 或准备阶段） |
| `!ref ff` | 投降（对手赢下整场） |
| `!ref abort` | 开图 30s 内中止当前图、重开 |
| `!ref ready` | 触发机器人 `!mp settings` 自查 |
| `!ref ?` | 查当前状态（轮到谁/比分/已 ban/TB 是否解锁），始终可用 |
| `!ref ask <问题>` | 问 AI 助理（如「还能 ban 几张」「TB 啥时候能选」「这条规则啥意思」），需配 AI key |

### 赛制约束（已内置，来自 rule.txt）

- ban/pick 按 roll 定的先后手轮流；已 ban / 已打过的图不能再选。
- **不能 ban TB**；**TB 只有双方都到赛点（5-5）后才能 pick**，且此时只能 pick TB。
- 超时：ban/pick 60s（ban 超时＝弃权；pick 超时＝机器人按图池顺序代选）；准备 120s、TB 180s。
- 某图双方同分：机器人自动不计分、重开该图。
- BP 模板（`["pick","ban","pick","ban"]` = 1st Pick→1st Ban→2nd Pick→2nd Ban→剩余 Pick）可在规则书 `format.bp_order` 改；
  有 2-ban 的图池就放两个 `"ban"`，没有就删掉。

## 3. 人工接管（重赛 / 争议 / 突发）

就在**同一个 agent 终端**里输入：

| 命令 | 作用 |
|------|------|
| `/human` | 人工接管：**AI 立即停止一切自动发送**；之后你直接打字（如 `!mp map 5223058 0`、`!mp abort`、普通说明）会**作为房主发进房间** |
| `/ai` | 交回 AI 继续自动裁判 |
| `/state` | 看当前阶段 / 比分 / 轮到谁 / 已 ban / TB 是否解锁 |
| `/quit` | 退出（断开连接） |

控制台是一个带**可滚动历史**的界面:房间所有消息和机器人动作都实时显示;用 **↑/↓** 滚一行、**PgUp/PgDn** 翻页、**Home/End** 到顶/底回看历史(向上滚动时新消息不会把你拽回底部)。

因为人工和 AI 共用**同一个 IRC 连接（房主本人）**，接管时无需第二个账号、无需 `!mp addref`、无需 `#join` 别的房间——直接借这条连接发指令即可。需要重赛某图、处理掉线纠纷时 `/human` 接手，处理完 `/ai` 交回。

> 注意：人工模式下你手动 `!mp map/start` 改变了进程，引擎的 BP 选图状态不会感知到你选了哪张（比分仍会从 BanchoBot 结果正确累计）。如果手动改动了选图进程，交回 AI 后用 `/state` 核对一下。

## 4. AI 助理（答疑 / 规则解释 / 赛后摘要）

机器人内置 AI 助理。**红线**：算分、判胜、ban/pick 合法性永远由确定性引擎负责，AI 绝不碰、也不会改比分。

- **房间答疑**：选手发 `!ref ask <问题>`，助理拿「引擎算好的当前状态快照 + 规则书原文」回答（轮到谁、还能 ban 几张、TB 何时解锁、某条规则怎么解释…）。
- **状态速查**：`!ref ?` 永远可用，直接给一行确定性状态（不依赖 AI）。
- **AI 裁判控制器（受限自动）**：选手用 `!ref <自由描述>` 提出**规则书没覆盖/模糊/突发**的情况（如 `!ref 对面一直不ready怎么办`、`!ref 我刚掉线了能重开吗`、`!ref 他pick超时了`），AI 拿「状态快照 + 近期房间记录 + 规则书」做出裁决：
  - 给出一句房间说明，并在需要时产出一条 `!mp` 指令；
  - **白名单指令**（`!mp timer/settings/map/mods/start/invite`）→ **自动发**给 BanchoBot 推进比赛；
  - **破坏性/影响比分**的（`!mp abort`/`!mp close`/踢人等）→ **不自动发**，只在房间打出 `[需人工确认] 建议指令：…`，等你 `/human` 接管后执行。
  - 普通聊天、以及引擎已覆盖的 `!ref pick/ban/pause/ff/abort/ready` 等不会触发控制器。
- **赛后摘要**：一场打完（`finished`），助理读该房间聊天日志 + 最终状态生成复盘，写到 `logs/chat/mp_<房号>.summary.md`。

**共享图池表**：在引擎和 AI 之上有一张权威的图池状态表（`core.mappool_status`），逐张图标记 `available / banned / played / current`，由唯一的真相（图池 + 已 ban/已打/当前图）推导。引擎判合法、路由判对错、AI 做判断，**都读这同一张表**；这张表也随状态快照喂给 AI。

**路由规则（每条房间消息走哪条路，由 `core.classify_ref_message` 统一裁定）**：

| 消息 | 去向 |
|------|------|
| 不是 `!ref`，或发言者不是参赛选手 | 不处理（只记日志） |
| `!ref pick/ban <图>` 且图池表**没问题** | **确定性引擎** |
| `!ref pick/ban <图>` 且**图池层出错**（图不存在 / 已被 ban / 已打过） | **转给 AI**（当作引擎层错误来处理：解释、引导重选） |
| `!ref pause/ready/ff/abort/roll/first/last/skip …` | **确定性引擎** |
| `!ref ?` / `!ref status` / `!ref ask <问题>` / 空 `!ref` | **AI 只读答疑** |
| 其它 `!ref <自由文字>`（引擎不认识的） | **AI 控制器**（裁决 + 白名单指令） |

核心：**pick/ban 先查共享图池表——没错走引擎，有错才转 AI**。所以 `!ref pick nm1`（合法）一定走引擎；`!ref ban 一张不存在/已ban的图` 才会交给 AI 去解释处理。
注意：图池层之外的不合法（**没轮到你**、TB 未解锁等）仍由引擎按规则**静默忽略**；想知道原因用 `!ref ?` 或 `!ref <问题>`。

**引擎识别的 BanchoBot 消息（白名单，`core.banchobot_relevant`）**：只认这 5 类，其余（进/离房、换 slot、`Countdown ends`、`!mp settings` 那一坨、glhf…）一律在入口丢弃：

| BanchoBot 消息 | 用途 |
|------|------|
| `Created the tournament match …/mp/<id>` | 建房回执 → 绑定频道 |
| `<player> rolls N point(s)` | roll 点（**仅 roll 点环节** `roll_phase` 为真时才认；环节外的 roll 忽略） |
| `<player> finished playing (Score: N, …)` | 累计本图分数 |
| `The match has finished` | 结算本图、判胜 |
| `All players are ready` | 触发开赛检查 |

要增减引擎认的 BanchoBot 消息，改 `banchobot_relevant` 一处即可。
- **开启**：需在 `config.json` 配 AI（见主 README 的 `config.json` 段）或设 `AI_API_KEY` 环境变量。
  规则书原文默认读 `rule.txt`，可用 `--rules-file <路径>` 指定。没配 key 时 `!ref ?` 仍可用，`!ref ask` 回落到状态行。
- 所有 AI 调用在**后台线程**进行，不会卡住裁判主循环；调用失败只记日志、不影响比赛。

## 5. 聊天记录（排查用）

`agent` / `chat` 运行时会把每个房间的**全部聊天 + 机器人发出的每条指令**(双向、带时间戳)写到本地文件，方便事后排查：

```
logs/chat/mp_<房号>.log     # 每个 mp 房间一个文件，如 logs/chat/mp_121270745.log
logs/chat/BanchoBot.log     # 与 BanchoBot 的私聊（建房回执等）
```

每行形如 `[2026-06-06 20:47:10] <fate80016> NM6`、`[...] <机器人名> !mp start 7`。

- 目录默认 `logs/chat`（已在 `.gitignore`）。换目录或关闭：`--chat-log-dir <路径>`，传空 `--chat-log-dir ""` 关闭。
- 另有 `debug.log` 记录原始 IRC 协议流量（更底层、更吵）。

## 6. 排查

- **机器人不开房**：确认 nick/IRC 密码正确、能连上 Bancho、`--rulebook` 路径存在、至少填了一名选手。
- **没邀请到人**：`--red`/`--blue` 里的选手名必须是准确 osu! 用户名。
- **队伍/计分不对**：确认房间显示 `TeamVs` + `ScoreV2`（机器人会 `!mp set 2 3 3`，来自规则书 `format`）。
- **改了图池/赛制没生效**：换 `--rulebook` 指向新 JSON，或编辑该文件后重启进程。
- **AI 不应答 `!ref ask`**：没配 AI key（看启动行 `AI assistant: off`）；`!ref ?` 不依赖 AI、始终可用。
- 机器人**只**响应 BanchoBot 系统消息和 `!ref` 指令，普通聊天不会触发。

## 7. 不用真人也想先验证

```bash
python3 -m unittest tests.test_bp_engine -v      # ban/pick/roll/超时/暂停/FF/abort 全流程
python3 -m unittest tests.test_bo11_replay -v    # 真实 BO11 录像端到端回放到 6-2 + 关房
```
