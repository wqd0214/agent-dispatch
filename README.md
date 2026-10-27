# agent-dispatch

纯命令行的多智能体派工：一个**主管**智能体负责派工和裁决，若干**执行者**智能体负责干活。
主管和执行者都可以是任意命令行智能体（pi、OpenCode、Codex、Claude Code）。

全部状态是 `~/.agent-dispatch/` 下的文件。Codex 和 pi 主管的作业另外启动一个本地通知等待进程，等待期间不调用模型；本轮结束后自动把结果回传到原会话。

## Codex 原会话自动回传

在交互式 Codex 中照常派工即可：工具从 `CODEX_THREAD_ID`（兼容 `CODEX_SESSION_ID`）识别并绑定原会话。
`start`、`reply` 和 `follow` 成功后自动启动本地等待器；本轮结束后，以 `codex queue` 向原会话排入结果。
用户可以继续留在同一会话聊天，无需模型循环调用 `wait`，也不启动另一个 Codex 主管。
执行者正常工作和主管处理结果仍消耗各自的 Token。

- Claude Code、其它主管和手动 `wait` 的用法不变。执行者仍可选 headless 或 tmux。
- 需要本机 Codex 支持 `queue`，并能连接承载原会话的服务；本次已验证 Codex 0.163.0 的本地共享服务。
  不支持跨机器、独立 `--no-daemon` 会话或任意远程端点自动发现，不能把通知发给另一个服务里的同名会话。
- `status <作业>` / `status <作业> --json` 可查每轮 `codex_notify` 状态、目标、错误和 `notify.log` 路径。
  “已入队”仅表示 Codex 接受消息，不代表主管已消费，更不代表作业已验收。
- 明确投递失败后可执行 `agent-dispatch notify-retry <作业> [--turn N]`；每轮最多 3 次投递。
  超时或投递进程中断可能已入队，状态为“结果不明”，不自动重发；先在原会话核对。
- 人工持有、叫停、已裁决或过时回合不发送结果。正在投递时的人工操作与外部消息接受之间存在短暂竞态，
  主管消费通知后仍须读取最新状态，不直接重复派工或裁决。
- 设置 `AGENT_DISPATCH_CODEX_NOTIFY=0` 可关闭自动回传。自定义安装可设置 `AGENT_DISPATCH_CODEX_BIN`。
  等待器不是系统常驻服务；机器重启或等待器被杀后，检查作业状态，再使用 `notify-retry` 补投已结束作业。

通知只传作业、回合、状态和本地结果路径，不上传完整日志。现有 Codex 认证与配置保持不变。

## pi 原会话自动回传

在交互式 pi 里照常派工即可：工具从 `PI_SESSION_ID`（pi 会话里嵌套运行时也带着宿主的
Claude/Codex 变量，pi 身份在它们之前识别）识别并绑定原会话，`kind` 记为 `pi`。
`start`、`reply` 和 `follow` 成功后自动启动本地等待器；本轮结束后，把结果事件原子写入
`~/.agent-dispatch/notify/pi/<会话>/<事件>.json`，由仓库里 `.pi/extensions/agent-dispatch-supervisor.ts`
扩展送入原会话。扩展统一以 followUp 方式请求投递：pi 空闲时立即触发新回合，忙时排队，
不打断在飞回合。用户可以继续留在同一会话聊天，无需模型循环调用 `wait`；
监听与等待只用文件系统，不调用模型。

- 加载方式：在仓库目录启动 pi（工程扩展自动加载，改过扩展后 `/reload` 生效），或在任意目录用
  `pi -e <仓库>/.pi/extensions/agent-dispatch-supervisor.ts` 显式加载。执行者进程与非交互单轮模式不监听。
- 扩展送入前会按与 `wait`/`status` 一致的规则重查台账：过时回合、人工持有、叫停、已验收/驳回的一律抑制；
  台账写着 running 但运行器进程已不在的，按意外中断送入（与 Python 的 effective_state 同一规则）。
  消费结果留在同名 `.sent.json` / `.suppressed.json` 标记里，`status <作业>` 可查。
  通知到达只代表「该去复核了」：同一事件可能因崩溃恢复被重送一次，消费时先读最新 status，
  按最新状态处理，不要仅凭通知本身重复派工或裁决。
- 未加载扩展时事件只写盘、不会被谎报已消费；`status <作业>` 的「待扩展消费」即提示手动
  `agent-dispatch wait <作业>`。重载或切回原会话时会补投遗留的未消费事件；切换到别的会话不会收到旧会话的通知。
- 设置 `AGENT_DISPATCH_PI_NOTIFY=0` 可关闭 pi 自动回传（与 `AGENT_DISPATCH_CODEX_NOTIFY` 相互独立）。
- 明确投递失败（通知目录写不进等）可用 `agent-dispatch notify-retry <作业> [--turn N]`；每轮最多 3 次。
  已写入通知文件的作业不重复投递；结果是否到达原会话以扩展标记为准。
- 跨机器不可用：通知走本机文件，扩展与派工命令必须读同一个 `AGENT_DISPATCH_HOME`。

```
主管（任意智能体会话，或你本人）
   │ 调命令
   ▼
agent-dispatch ──读写──▶ ~/.agent-dispatch/jobs/…  ◀──只读── agent-dispatch web
   │ 启动
   ▼
执行者（pi / OpenCode：无头进程，或 tmux 里的交互界面）
```

## 安装

需要 Python 3.9+（只用标准库）；tmux 方式需要 tmux。

```bash
ln -s "$PWD/bin/agent-dispatch" ~/.local/bin/agent-dispatch
```

## 项目、工程、角色

- **项目**：一条产品线或一个工程群（如 C2）。
- **工程**：项目下的一个代码仓库目录（前端、后端、测试工程……），可以带自己的资源，如浏览器通道。
- **角色**：项目里的一个岗位（后端开发工程师、前端开发工程师、测试工程师……）。角色定义了：负责哪些工程、
  用哪个后端和模型、运行方式、默认占哪些通道、派工单模板（岗位职责和边界）、闸门。

```bash
agent-dispatch projects add c2 --name WMS_ZC_C2 \
    --repo backend=~/WmsProjects/WMS_ZC_C2/WMS_V3_backend --repo test=~/WmsProjects/WMS_ZC_C2/Zc_Wms_Test_V2
agent-dispatch projects add-repo c2 frontend=~/WmsProjects/WMS_ZC_C2/Zc_Wms_Frontend
agent-dispatch roles add backend-dev -P c2 --repos backend      # 沿用全局的「后端开发工程师」，只指定它在本项目负责的工程
agent-dispatch roles add dba -P c2 --title 数据库管理员 --repos backend   # 全新角色：生成定义和派工单模板，再去改模板

agent-dispatch projects            # 项目列表；projects show c2 看工程和角色
agent-dispatch roles -P c2         # 这个项目可派的角色；roles show tester -P c2 看完整定义和派工单模板
```

定义都是文件，直接编辑即可：

```
~/.agent-dispatch/projects/<项目ID>/
├── project.json            名称、工程清单（目录、通道）、defaults（对所有角色生效的默认值）
├── supervisor.md           给主管的项目专属规则（`agent-dispatch role` 会附上）
└── roles/<角色ID>.json      项目的角色；与全局角色同名时只写要覆盖的项
    roles/<角色ID>.brief.md  派工单模板，可用 {{task}} {{job}} {{role}} {{project}} {{repo}} {{cwd}} {{channels}}
~/.agent-dispatch/roles/     本机的全局角色
<本工程>/roles/              随工具发布的全局角色：backend-dev、frontend-dev、tester
```

解析顺序：全局角色 → 项目的 `defaults` → 项目角色，后者逐项覆盖前者。同一个工程的通道由该工程的所有角色共用。

## 模型、模型池与派工工具

派工只认角色，模型由角色的**模型池**按优先级和比例自动选择，主管不用也不能指定。

```bash
agent-dispatch models import --list        # 先看工具里有哪些模型、哪些已登记；按名导入：models import <模型ID …>，全量：models import [--backend pi]
agent-dispatch models                           # 登记表：状态、并发（占用 / 上限）、用在哪些角色
agent-dispatch models limit pi:glm-5.3 3       # 单个模型的并发上限；default 恢复默认；--default N 改全局默认值
agent-dispatch roles pool tester -P c2 pi:glm-5.3=70 oc:glm-5=30   # 项目角色的模型池：按 7:3
agent-dispatch roles pool tester --global pi:glm-5.3=1             # 全局角色的模型池：项目里没单独配置的都沿用它
agent-dispatch roles pool tester -P c2 pi:glm-5.3=1 oc:glm-5=1 oc:glm-4=1@2   # @2 是优先级：前两个均衡着用，都不可用才用第三个
agent-dispatch models disable oc:glm-5          # 停用某个模型：所有模型池自动跳过它
agent-dispatch backends disable opencode        # 停用某个派工工具：新派工不再用它
```

同一个模型（按 工具+模型ID 计）同时最多承接的派工单数有并发上限（全局默认 5，可按模型覆盖）；满了不报错，
新单进入「排队中」，有空位后按排队先后自动开始。

- **优先级**：模型池里每个模型可以带优先级（`<短名>=<比例>@<优先级>`，1 最优先，不写就是 1）。派工只在「现在能用的模型里
  优先级最高的那一档」里选；这一档全都停用、暂停或并发已满，才落到下一档；所有档都只剩并发已满的，新单排队。
- **同档均衡**：同一优先级的模型按当前在跑的作业数均衡，选「在跑数 ÷ 比例」最小的那个，两个模型都能用、都没满时会轮着用，
  某个模型暂停后恢复也不会被连续补派。在跑数打平时再看「项目 + 角色」的派工历史，选「目标占比 × (已派数 + 1) − 已派数」
  最大的那个，所以逐张派工时长期仍贴合比例。
- **配置了模型池的角色**，派工时传 `--model` 或 `--backend` 会被拒绝；没配模型池的角色仍按角色里写的模型或后端默认模型。
- **服务商报错**（额度用尽、限流、鉴权、计费）时，该模型自动暂停一段时间（默认 30 分钟，`models.json` 的 `cooldown_min`）。
  如果是作业第 1 轮、还没执行过任何操作，自动换池里另一个模型重新派出；已经动过手的不自动重派，以免重复操作，交给主管决定。
- **派工工具停用后**：不走模型池的派工直接被拒；模型池里属于它的模型被跳过；进行中的作业不受影响，续派仍可进行。
- 以上都可以在网页的「模型与角色」页操作：派工工具开关、各角色（全局与各项目）的模型池和实际配比、模型登记表与使用统计。
  网页的写操作只接受本机页面发出的请求。

配置文件：`~/.agent-dispatch/models.json`（登记表）、`model-health.json`（暂停记录）、`settings.json`（派工工具开关）。
全局角色的模型池写在 `~/.agent-dispatch/roles/<角色>.json`，不改随工具发布的文件。

## MCP 服务与技能

MCP 服务和技能各有一张全局登记表，键是唯一的**登记 ID**；条目里的 `name` 是下发给执行者时的服务/技能名（缺省等于登记 ID）。
同一个服务名在不同工程里内容不同是常态（pi 全局、OpenCode 全局、各工程的配置里同名不同参）：
导入时内容相同的来源合并成一条（记下全部来源），内容不同的各自成一条，登记 ID 为 `<名>@<来源简称>`（如 `playwright@a04-test`、`playwright@pi-global`），`name` 保持原名。
角色的 `mcp` / `skills` 字段存登记 ID；同一个角色选中的条目里不允许两个相同的 name（执行者按名字调用）。
登记表可以从现有的 pi / OpenCode 配置导入（只读那些文件，绝不修改）：

```bash
agent-dispatch mcp import             # 按服务名列出候选：同名冲突的展示各变体与差异参数
agent-dispatch mcp import --all       # 全部导入（同名不同内容的各成一条）
agent-dispatch mcp import playwright@a04-test                          # 按登记 ID 选
agent-dispatch mcp import playwright --source "项目 a04/test 的 .pi/mcp.json"   # 或按来源消歧
agent-dispatch mcp                     # 登记表（敏感信息已遮罩）：登记 ID、name、被哪些角色用、全部来源
agent-dispatch mcp add mydb --command npx --args "-y,@bytebase/dbhub" --env DSN=mysql://...   # 手动登记（--name 可改下发名）
agent-dispatch mcp add mydb2 --command npx --args "-y,@bytebase/dbhub" --exposure direct   # pi：工具直接声明给模型
agent-dispatch mcp remove mydb
agent-dispatch skills import           # 技能同理：同名目录先按真实路径（软链接）去重，再按 SKILL.md 内容合并
agent-dispatch skills add my-skill ~/skills/my-skill
```

- 导入来源：MCP 看 `~/.pi/agent/mcp.json`、`~/.config/opencode/opencode.json` 以及各项目工程里的 `.pi/mcp.json`、`opencode.json`；
  技能看 `~/.claude/skills`、`~/.cc-switch/skills`、`~/.pi/agent/skills`、`~/.agents/skills` 和各工程的 `.agents/skills`、`.pi/skills`、`.claude/skills`（目录里有 SKILL.md 才算技能；内容相同的目录合成一条）。
- **敏感信息只存本机**：登记表存在 `~/.agent-dispatch/mcp.json`、`skills.json`；
  env 里键名含 password / passwd / secret / token / key / dsn / credential 等字样的值、值本身是带凭据 URL 的，
  以及 headers 的值和 args 里形如密码令牌的内容，在命令行与网页接口里一律显示为 `••••`（`NODE_ENV` 这类不是秘密的原样显示）；
  修改登记时提交遮罩占位则保留原值。
- **角色字段的语义**：`mcp` / `skills` 字段**不存在 = 不管理**，派工时不注入任何东西，执行者照旧用自己的配置（即现在的行为）；
  **字段存在（哪怕是空列表）= 由 agent-dispatch 管理**，派工时只带这些。空列表表示「管理，但一个都不带」。
  字段里存登记 ID，必须已登记；同一个角色里两个同名的会被拒；登记表里的 ID 被角色引用时不能删除。
- 网页同样提供 `/api/mcp`、`/api/skills` 一套接口（候选按名称分组、导入、增删改，写操作要求本机页面请求头），
  角色的 mcp / skills 在配置中心里和其它字段一样分层配置、可恢复继承。

### 按作业下发（派工时真正生效）

角色管了 MCP / 技能时，派工命令会附带下发参数；临时文件统一放在该作业自己的目录下
（`~/.agent-dispatch/jobs/<作业名>/inject/`，权限 600，含凭据），续派（reply）复用同一套、每轮重新生成，
作业结束（验收 / 驳回 / 叫停）后删除；`status <作业名>` 里显示的「接手」命令自带同样的环境变量与参数。
不修改任何工程目录，也不修改用户的 pi / OpenCode 配置。

- **pi**：技能用 `--no-skills` 加每个选中目录一个 `--skill`，只加载选中的；MCP 用 `PI_CODING_AGENT_DIR`
  指向作业私有的临时 agent 目录——`mcp.json` 从登记表生成，`auth.json` / `models.json` / `sessions` 符号链接
  回 `~/.pi/agent`（绝不复制或改写），用户全局的服务不再加载。角色同时管了技能时是严格模式：加 `--no-approve`
  不读工程 `.pi` 下的任何资源，`~/.pi/agent/settings.json` 里配置的扩展包（及其技能）也不加载。
  只管 MCP、不管技能时，设置、扩展包、技能和项目信任记录照常链接进来，工程自己的 `.pi` 资源
  （扩展、技能、`.pi/mcp.json`）照常加载——换掉的只是用户全局那一层的服务。
- **OpenCode**：`OPENCODE_CONFIG_CONTENT` 注入——`mcp` 段写选中服务的完整定义，工程 `opencode.json` 与
  全局配置里有但本次没选中的服务写「同名完整定义 + enabled:false」关掉（只读这些文件）；技能写 `skills.paths`；
  无头命令加 `--standalone`（env 配置只在独立进程里生效）。局限：无法排除执行者自己的全局技能，只能追加。
- 派工单预览（`/api/jobs/preview`、`start --dry-run`）和 `status <作业名>` 会列出「本次下发」：管理了哪些
  MCP（name 与登记 ID）、哪些技能、各自的局限说明，凭据一律遮罩；未管理时显示「未管理：执行者使用自己的配置」。

## 环境、角色继承与角色并发

这一节的字段都可以不写，不写就和原先完全一样。项目的两项直接写在 `project.json` 里，角色的写在角色定义里。

**环境**：项目登记有哪些环境，派工时用 `--env` 选一个。

```json
// ~/.agent-dispatch/projects/c2/project.json
{ "envs": {"382": {"name": "C2-382"}, "225": {"name": "C2-225"}},
  "limit_groups": {"test": 4} }
```

- 角色写 `"needs_env": true`：派工不带 `--env` 就拒绝。
- 角色可以停用：`agent-dispatch roles disable <角色> -P <项目>`（定义里写成 `"enabled": false`），停用后不接新单，
  已派出的作业照常续派和裁决；`roles enable` 恢复。停用不随 `extends` 继承。某个环境暂时不测时，把它的角色停掉即可。
- 角色写 `"env": "382"`：这个角色固定在 382 环境，派工不用带 `--env`（带了别的环境会被拒绝），其余和带了
  `--env 382` 完全一样。配合 `extends` 给每个环境建一个角色，选角色就定了环境：
  `{"extends": "tester-pallet", "title": "托盘主测试工程师（382）", "env": "382"}`。
- 角色写 `"repo_by_env": {"382": "t382-pallet", "225": "t225-pallet"}`：不同环境在不同工程（工作树）里干活，
  不用再靠 `--repo` 记哪个目录对应哪个环境。
- 角色 `mcp` 里的登记 ID 可以写 `{{env}}`，派工时换成所选环境：`"mcp": ["wms-db@{{env}}", "pw-pc", "pw-pda"]`。
  登记时每个环境各登记一条、`--name` 取同一个服务名，执行者看到的名字就不随环境变。
  派工没带 `--env` 时这类条目不下发（不针对某个环境的任务就不挂环境的服务）；要强制带环境用 `needs_env`。
- 环境可以带 `vars`：`"envs": {"281": {"name": "A04-281", "vars": {"AT_ENV": "test281"}}}`，
  选中这个环境时这些变量一并传给执行者，用来对接工程自己认的环境名。
- 登记条目的 `command` / `args` / `env` / `url` / `headers` 里可以写占位符
  `{{env}}` `{{job}}` `{{role}}` `{{project}}` `{{repo}}` `{{cwd}}`，下发时按这张单填。例如浏览器目录写成
  `/tmp/pw-{{project}}-{{env}}-{{role}}-pc`，一条登记就能让每个角色在每个环境各用一个独立目录。
- 所选环境以环境变量 `AGENT_DISPATCH_ENV` 传给执行者，派工单模板里可用 `{{env}}`。
  工程里的工具读这个变量决定连哪套环境，工作树本身不用为换环境改文件。

**角色继承**：角色写 `"extends": "<另一个角色ID>"`，只写要改的项；派工单模板、闸门、模型池等没写的都沿用
那个角色的。一条业务线一个角色时用它，免得几份几乎一样的定义各改各的。

**角色并发上限**：

- 角色写 `"max_parallel": 1`：同一角色在同一环境里最多同时 1 张单，不同环境互不占用。不写就不限制。
- 角色写 `"limit_group": "test"`，项目 `limit_groups` 里给这个分组一个上限：分组内的角色合计不超过它
  （例如所有测试角色合计最多 4 张，用来封住同时开着的浏览器数量）。
- 满了不报错，新单进入排队，有空位后按排队先后自动开始；`status` 里写明在等谁。
- 「占着位置」的作业：运行中、人工持有，或 tmux 界面还开着的。

**回合结束即释放**：角色写 `"release_on_done": true`，tmux 方式下回合一结束就关界面，它拉起的 MCP 服务和
浏览器跟着退出，通道与并发位置随即空出来；续派（reply）时用同一个会话 ID 重开界面，对话上下文还在，
浏览器里的页面状态不保留。不写则维持原先的行为：界面留到验收、驳回或叫停。

```bash
agent-dispatch start smoke-1 -P c2 -r tester-pallet --env 382 -f 任务.md
```

## 快速开始

```bash
# 按角色派工。在项目的工程目录里执行时 --project 可省略
agent-dispatch start sanTai-01 --project c2 --role tester --batch 回归-1002 \
    --batch-title "出库回归" --batch-reason "上线前回归出库主流程" "跑 托盘库出库-整托三态判定"
agent-dispatch start fix-5619 -P c2 -r backend-dev --batch 回归-1002 --follows sanTai-01 "修复：移入收货重复点击产生两条记录……"
agent-dispatch start pda-ui -P c2 -r frontend-dev --repo pda "……"      # 角色负责多个工程时指明工程

agent-dispatch wait sanTai-01            # 限时等；返回码 3 = 还在跑
agent-dispatch status                    # 总览；--project <项目名> --repo <工程名> 筛选
agent-dispatch status sanTai-01          # 单个作业：结论、STATUS、QUESTION、接入方式、派工者
agent-dispatch sessions                  # 按派工会话汇总：每个会话派了几张单、各是什么状态
agent-dispatch batches                   # 按事项汇总：每个批次的标题、原因、谁派的、各状态数量
agent-dispatch batch 回归-1002           # 看一个事项；--title / --reason 事后补标题和原因
agent-dispatch gate sanTai-01            # 机械闸门
agent-dispatch accept sanTai-01 --note "核对了 report.md 的三态断言"
```

不用角色也能派（临时的活）：

```bash
agent-dispatch start demo --cwd ~/some/repo -b pi "把 README 里的错别字改掉，最后写 STATUS: PASS"
```

`start` 会自动识别是谁在派工（执行者嵌套派工、`AGENT_DISPATCH_SESSION` 自设会话、Claude Code 会话、终端），
记进作业的 `dispatcher` 字段并登记到 `sessions/` 目录；也可以用 `--session-title "一句话说明这次会话在办什么事"` 给会话起名，
方便用户在总览里按会话查看。裁决（reply / accept / reject / stop 等）也会记下是哪个会话做的。

每个批次是一个「事项」：`--batch-title` / `--batch-reason` 说明要办什么事、为什么派这批单（登记在 `batches/<批次>.json`，
忘了带时命令会提醒事后用 `agent-dispatch batch <批次> --title … --reason …` 补上）；`--follows <作业名>` 标明这张单
是因为哪张单才派的（重派、按复核意见修复、接着上一步做），网页上会挂在那张单下面。

## 让谁当主管

在本目录打开任意智能体即可，它会读 `AGENTS.md`（Claude Code 读 `SKILL.md`），再读 `SUPERVISOR.md`。
`SUPERVISOR.md` 是主管角色的唯一来源：职责、工作循环、裁决表（哪些能自己定、哪些必须问你、哪些禁止）。

主管不要在被测工程目录里启动：那里的工具配置和执行约定是给执行者的。

## 两种运行方式

| | `headless`（默认） | `tmux` |
|---|---|---|
| 执行者是什么 | 无头进程，一轮结束就退出 | 跑在 tmux 会话里的交互界面，两轮之间不退出 |
| 看过程 | `log -f`、`history`、网页 | 同左，外加 `attach` 直接看界面 |
| 中途插话 | 不能，只能 `stop` 后 `reply` | `attach` 进去直接打字 |
| 两轮之间的浏览器和页面状态 | 取决于智能体 | 保留 |
| 通道何时释放 | 每轮结束 | `accept` / `reject` / `stop` 之后 |
| 模型 | `--model` 指定 | `--model` 指定（OpenCode 以独立服务模式启动界面来让模型生效）；实际用的模型若与指定的不同，会在日志和总览里标出 |

用 `--mode` 或角色定义里的 `mode` 切换。tmux 方式下回合是否结束，靠读智能体自己的会话存储判断，不依赖执行者自觉。

## 看和介入

```bash
agent-dispatch log <作业> -f          # 实时输出（工具调用、回复）
agent-dispatch history <作业> --full  # 逐步的工具入参与返回
agent-dispatch attach <作业>          # tmux 方式：接进界面，可直接打字；Ctrl-b d 退出，不影响作业
agent-dispatch hub                    # tmux 方式：接进汇总会话，每个作业一个窗口，新派的自动出现；Ctrl-b n / p 切换
agent-dispatch web start --open       # 网页总览，后台运行并打开浏览器；web stop 停止，web status 查看
agent-dispatch web                    # 同上，但在前台运行（Ctrl-C 关闭）
agent-dispatch status --watch         # 终端里的总览
agent-dispatch sessions                # 按派工会话汇总（--project <项目ID> 筛选）；配合 start --session-title 使用

agent-dispatch stop <作业>            # 叫停（已结束的 tmux 作业：只关界面、释放通道）
agent-dispatch reply <作业> "…"       # 在同一会话里续派
agent-dispatch take <作业>            # 人工接手：主管不再往里派；用完 release
```

汇总会话是一个名为 `agent-dispatch` 的 tmux 会话：tmux 方式的作业派出时，界面窗口会自动挂进去并以作业名命名，
首个窗口是作业总览。在任意终端（如 Zed 的 Terminal Thread）里执行一次 `agent-dispatch hub` 并保持开着，
之后新派的作业就会自己出现在里面。关掉它不影响作业；环境变量 `AGENT_DISPATCH_HUB` 可改名，设为空串则不启用。

网页的作业部分是只读的旁观者，「模型与角色」页可以修改配置；关掉网页不影响任何作业。工作台按事项（批次）分组列出
要你处理的单子（可切回按状态），顶部有通道占用提示；「事项」页按派出顺序列出每个事项的全部单子，带 follows 的缩进挂靠；
作业详情里有用例判定和「因 X 而派 / 后续：Y」的关联。
没有登记进项目的目录也能派工，归类按目录推断：工程是目录名（worktree 归到主工程），项目是它的上级目录名。

## 状态目录

```
~/.agent-dispatch/
├── jobs/<作业>/
│   ├── job.json            台账：状态、会话 ID、通道、轮次、闸门、裁决记录
│   ├── brief.md            首轮派工单
│   ├── run.log             可读日志
│   ├── events.jsonl        归一化的工具调用与回复
│   ├── result.md           最近一轮的结论
│   └── turns/<n>.task.md / <n>.result.md / <n>.raw.jsonl
├── channels/<工程>/        通道占用（一个通道一个文件）
├── sessions/               派工会话登记：谁派的、标题、最近活动
├── batches/                事项登记：批次的标题、原因、派工者
├── projects/               项目、工程、角色的定义
└── roles/                  本机的全局角色
```

作业状态：`运行中` → `待审查` / `运行失败` / `已叫停` / `意外中断` → `已验收` / `已驳回`；另有 `人工持有`。
执行者自报的 `STATUS`（PASS / FAIL / BLOCKED / NEEDS_DECISION）是另一个维度。

## 扩展

**加一个后端**：在 `backends.json` 加一项。要提供启动与续聊的命令模板；事件流格式若与现有的不同，
在 `agent_dispatch/backends.py` 加一个归一化函数（无头方式）和一个会话存储读取函数（tmux 方式）。

**加一个角色**：`agent-dispatch roles add`，然后编辑生成的 json 和派工单模板。可配：`title`、`description`、`repos`、
`backend`、`model`、`mode`、`channels`（默认占哪些通道：`all` / `none` / 列表）、`timeout_min`、`statuses`、`gates`、`reply_suffix`。

闸门类型：`exit_ok`、`status_reported`、`run_dir`、`file`、`verdicts`。每项可设 `"level": "warn"` 降为提示；
设 `"when": "run_dir"` 则只在任务产生了 run 目录时才检查（同一个角色既跑测试又写用例时用）。

早先的 `profiles/*.json`（`-p wms-test`）仍然可用，但已被项目和角色取代。

## 已知限制

- 只限本机：主管、执行者和状态目录在同一台机器上。
- 通道锁只管经 agent-dispatch 派出的作业。你自己手动开的智能体会话在用哪个浏览器，它不知道。
- OpenCode 无头方式走它的后台服务，MCP 服务（含浏览器）由后台服务持有、同一工程的会话共用；
  tmux 方式以独立服务模式启动，MCP 服务挂在执行者自己的进程下，执行者结束时一起结束。
- 无头方式的 pi 必须关闭标准输入才不会挂起；OpenCode 不指定模型时会落到一个工具调用有问题的默认模型，
  并且按环境变量 `PWD`（而不是进程实际目录）认工作目录。三者都已在运行器和命令模板里处理。

## 开发

```bash
python3 -m unittest discover -s tests     # 用假智能体跑端到端流程，tmux 方式的用例需要本机有 tmux
```

另有一个 Tauri 2 桌面客户端（网页外壳 + 系统通知），见 [`client/README.md`](./client/README.md)。

