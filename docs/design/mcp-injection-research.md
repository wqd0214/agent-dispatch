# 四期前置调研：按作业注入 MCP 服务与技能（不修改工程目录文件）

调研日期：2026-10-04。环境：pi 1.0.2（managed install，`~/.pi/agent/install/releases/1.0.2`）、opencode v2.0.22。
本次为重派（上次 client-p4r-mcp-research 中断，/tmp 下无遗留实验目录，全部实验重做；所有结论均来自本次证据）。
实验目录：`/tmp/ad-mcp-test/`（临时 agent 目录 `piagent/`、临时工程 `proj/`、`ocproj/`、技能 `skills/`、扩展 `ext.js`）。
全程未修改 `~/.pi/agent/`、`~/.config/opencode/`、`~/.agent-dispatch/` 下任何文件（仅只读 `cp` 了 models.json/auth.json 到 /tmp 副本）。
未改动本仓库任何代码，故未跑测试套件、无提交。

---

## 1. pi

### 1.1 MCP 配置读取位置与逻辑（源码证据）

包路径：`/Users/wqd1994/.pi/agent/install/releases/1.0.2/node_modules/@earendil-works/pi-coding-agent/`

- `dist/bundle/chunks/chunk-ZSI3BL5Q.js` 中 `loadMcpConfig(options)`：
  - 先读 `join(options.agentDir, "mcp.json")`（scope=global）；
  - 若 `options.projectTrusted`，再读 `join(options.cwd, ".pi", "mcp.json")`（scope=project，CONFIG_DIR_NAME 默认 `.pi`）。
  - 工程条目可整体替换同名全局条目；**无 command/url/type 的工程条目只能覆盖全局同名服务的 `enabled` / `exposure` / `toolExposure`**（源码 `OVERRIDE_KEYS=["enabled","exposure","toolExposure"]`、`isOverride()`、`readConfigFile()`）。
- `dist/config.js` 第 436-447 行：
  - `ENV_AGENT_DIR = APP_NAME.toUpperCase() + "_CODING_AGENT_DIR"`，即 **`PI_CODING_AGENT_DIR`**；
  - `getAgentDir()`：若设置了该环境变量则完全用它替代 `~/.pi/agent`。另有 `PI_CODING_AGENT_SESSION_DIR` 可改会话目录。
- 没有指定「额外 mcp.json 文件路径」的参数；`pi mcp add -l` 写的是当前工程的 `.pi/mcp.json`（会改工程文件，不符合本次需求）。
- **`mcp-project-approvals.json` 在当前版本（1.0.2）源码中完全不存在**：对 dist、docs、CHANGELOG 及全部历史 release（0.87.1～1.0.2）grep `mcp-project-approvals` 均无命中，是旧版本遗留文件。当前版本工程级 MCP 的门禁是 **project trust**：`docs/security.md`「Understand project trust」一节；批准记录存在 `~/.pi/agent/trust.json`（本机存在，含项目决策）。print/JSON 模式无内置信任提示，可用 `--approve`（本次一次性信任）或 `--no-approve`（本次忽略工程文件），见 security.md 77-81 行。

### 1.2 实验验证（pi）

实验 1：`PI_CODING_AGENT_DIR` 换全局 MCP 来源
```
$ cat /tmp/ad-mcp-test/piagent/mcp.json   # 只含 ad-global-test（npx everything）
$ cd /tmp/ad-mcp-test/proj && PI_CODING_AGENT_DIR=/tmp/ad-mcp-test/piagent pi mcp list
ad-global-test: connected, 14 tools (codemode, global)
$ cd /tmp/ad-mcp-test/proj && pi mcp list --json | head -5   # 对照：默认目录
{"servers": [{"name": "DB_144_23306_WMS_DB", "scope": "global", ...
```
结论：环境变量整体替换全局 mcp.json 来源，用户自己的 MCP 服务不再加载（顺带实现了「只给这个作业指定服务」的隔离）。

实验 2：工程级 `enabled:false` 关掉全局服务 + 工程新增服务（配合 `--approve`）
```
$ cat proj/.pi/mcp.json  # ad-global-test:{"enabled":false}; ad-proj-new: everything
$ PI_CODING_AGENT_DIR=... pi mcp list          # 不带 --approve
ad-global-test: connected ... （并提示 .pi/mcp.json is ignored because the project is not trusted）
$ PI_CODING_AGENT_DIR=... pi -p --no-session --approve "列出你可用的 MCP 工具所属服务器名，不要调用"
mcp__ad_proj_new            # ad-global-test 已被关掉，未出现
```
结论：在受信任工程里，`.pi/mcp.json` 的 `{"名": {"enabled": false}}` 能关掉全局同名服务。注意 `pi mcp list` 子命令不接受 `--approve`，print 模式会话才接受。

实验 3：`--extension` 注册 MCP（文档 docs/extensions.md 194-203 行：`pi.registerMcpServer(name, config)`，注册不落盘，同名 mcp.json 条目优先）
```
$ cat /tmp/ad-mcp-test/ext.js   # export default (pi)=>pi.registerMcpServer("ad-ext-server",{command:"npx",args:["-y","@modelcontextprotocol/server-everything"],...})
$ PI_CODING_AGENT_DIR=... pi -p --no-session -e /tmp/ad-mcp-test/ext.js --approve "…列出服务器名…"
mcp__ad_ext_server, mcp__ad_global_test
```
结论：`-e <扩展文件>` 可为单次运行注入 MCP 服务，不写任何配置文件。**这是 pi 侧最优注入方式**。

实验 4：技能
- `--skill` 可重复、`--no-skills` 只关「发现与配置」的技能、显式 `--skill` 仍加载（docs/cli.md 188-191 行）。
```
$ PI_CODING_AGENT_DIR=... pi -p --no-session --no-skills --skill /tmp/ad-mcp-test/skills --approve "列出加载的技能名"
ad-demo-skill
# 对照（无 --no-skills、无 --skill，本目录无 .pi/skills）：输出 ego-browser
```
- 附加发现：即使换了 `PI_CODING_AGENT_DIR`，`~/.agents/skills/`（用户级 Agent Skills 位置）仍被发现（对照实验里 ego-browser 出现；docs/skills.md 61 行）。要完全屏蔽须配 `--no-skills` 再显式 `--skill` 注入。

### 1.3 pi 推荐注入方式

- MCP + 技能一起按作业注入，最干净的组合：
  ```
  PI_CODING_AGENT_DIR=/tmp/ad-job-<id>/agent pi -p --no-session \
      -e /tmp/ad-job-<id>/mcp-extension.js \
      --no-skills --skill /tmp/ad-job-<id>/skills ... "任务提示"
  ```
  临时 agent 目录里放从模板生成的 `mcp.json`（可选）＋复制 `models.json`/`auth.json`（换目录后默认凭据不可见，本机实测必须复制 models.json 才能拿到模型，见实验 2 前的报错 `No API key found`；若用环境变量 API key 则不必）。作业结束 `rm -rf /tmp/ad-job-<id>` 即可，不污染用户目录。
- 若不想换 agent 目录：`-e 扩展` 注入 MCP（实验 3）＋ `--no-skills --skill` 注入技能，零临时文件；但此时用户全局 mcp.json 里的服务仍会加载，无法「只给这个作业」隔离（除非在工程 `.pi/mcp.json` 写 enabled:false，但那会改工程文件，不符合前提）。
- 做不到的：不换 agent 目录、不写工程文件、又想关掉用户全局某个 MCP 服务——pi 无此能力（无 CLI 覆盖项）。替代方案就是 `PI_CODING_AGENT_DIR`。

## 2. OpenCode

配置文件：全局 `~/.config/opencode/opencode.json`、工程 `opencode.json`（本机实测均被读取）。
`OPENCODE_CONFIG_CONTENT` 是环境变量形式的配置（v2.x），配合 `--standalone` 私有服务进程生效；注意 **`opencode mcp list` 连接的是后台常驻服务，不读该环境变量**（实测传了也不出现 env 注入的服务），验证必须用 `opencode run --standalone`。

实验 5：`OPENCODE_CONFIG_CONTENT` 新增 MCP 服务（实验目录 `/tmp/ad-mcp-test/ocproj`，工程 opencode.json 定义 oc-proj-server）
```
$ OPENCODE_CONFIG_CONTENT='{"mcp":{"oc-env-server":{"type":"local","command":["npx","-y","@modelcontextprotocol/server-everything"],"enabled":true},"oc-proj-server":{"enabled":false}}}' \
  opencode run --standalone --print-logs --model alibaba-coding-plan-cn/glm-5 "回答一个字：好"
日志：level=WARN "configuration normalization diagnostic" source=OPENCODE_CONFIG_CONTENT
      path=$.mcp.oc-proj-server kind=unsupported action="omitted enabled-only legacy MCP entry"
      "mcp connected" server=oc-env-server tools=16
      "mcp connected" server=oc-proj-server tools=16   ← 工程服务仍连上了
```
结论 A：**env 配置能新增 MCP 服务**（oc-env-server 连接成功）。
结论 B：`{"服务名":{"enabled":false}}` 这种「仅 enabled」条目被 OpenCode v2.0.22 判为不支持的 legacy 条目并丢弃，**关不掉工程配置里已有的服务**。

实验 6：env 里对同名服务给完整定义 + `enabled:false`
```
$ OPENCODE_CONFIG_CONTENT='{"mcp":{"oc-proj-server":{"type":"local","command":["npx","-y","@modelcontextprotocol/server-everything"],"enabled":false}}}' \
  opencode run --standalone --print-logs --model …
（全部日志中无 server=oc-proj-server 的 "mcp connected"；其余服务器正常连接）
```
结论 C：**用完整定义 + enabled:false 可以在 env 配置里禁用工程同名服务**（env 定义项生效、服务未启动）。局限：必须重复写出该服务的完整定义（command 等），dispatcher 需先知道工程配置内容；如果只想关而拿不到完整定义，则做不到（需要 NEEDS_DECISION 的话在此给出：目前无更优办法）。

实验 7：`skills.paths`
- 错误键名 `"skill"` 静默无效；正确键名是 **`"skills": {"paths": [...]}`**：
```
$ OPENCODE_CONFIG_CONTENT='{"skills":{"paths":["/tmp/ad-mcp-test/skills"]}}' \
  opencode run --standalone --model … "技能列表中是否有 ad-demo-skill？输出 是/否"  → 是
$ 同上 "输出 ad-demo-skill 的 description 原文"  → 调研用示例技能，仅用于验证注入
```
结论 D：`skills.paths` 可注入技能目录（注意键名是 `skills` 不是 `skill`）。未发现「排除已有技能目录」的 env 配置项（本机全局 `~/.config/opencode` 与 `~/.agents/skills` 的技能仍全部可用，实验 7 输出列表含几十个全局技能）。

### OpenCode 推荐注入方式

```
OPENCODE_CONFIG_CONTENT='{"mcp":{...新增/禁用...},"skills":{"paths":["<临时技能目录>"]}}' \
  opencode run --standalone --print-logs --model <m> "任务提示"
```
- 新增服务：直接写完整条目。禁用工程已有服务：同名 + 完整定义 + `enabled:false`。
- 技能：临时目录 + `skills.paths`。想只留注入技能：做不到排除全局技能（未找到反选项）。
- 清理：全部走环境变量与 /tmp 临时目录，作业结束删除临时目录即可；`--standalone` 保证不碰后台常驻服务。
- 局限：`opencode mcp list` 不能用来验证 env 注入（走后台服务）；模型自报的工具列表不可靠（实测模型先拒绝列出、后列错），验证以服务端日志（`--print-logs` 的 `mcp connected` 行）为准。

## 3. 总结对照

| 需求 | pi (1.0.2) | OpenCode (2.0.22) |
|---|---|---|
| 按作业新增 MCP | `-e 扩展` 调 registerMcpServer，或 PI_CODING_AGENT_DIR + 临时 mcp.json | OPENCODE_CONFIG_CONTENT 的 mcp 段 + --standalone |
| 按作业关掉已有服务 | 临时 agent 目录（隔离全局）；工程内覆盖需写 .pi/mcp.json 且需 project trust（--approve） | env 配置同名完整定义 + enabled:false（仅 enabled 的简写被丢弃） |
| 按作业注入技能 | --no-skills --skill <路径>（可重复） | skills.paths（键名是 skills） |
| 完全隔离用户全局技能 | --no-skills 可做到 | 未找到办法 |
| 工程级配置门禁 | project trust（trust.json；print 模式用 --approve/--no-approve） | 无门禁，工程 opencode.json 直接生效 |
