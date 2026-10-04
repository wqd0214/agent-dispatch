# 派工总览 · 桌面客户端（Tauri 2）

现有网页总览的外壳：窗口里加载本机的 `agent-dispatch web` 服务（默认 `http://127.0.0.1:8765/`，可配置），
并在后台每 5 秒对比一次总览数据，作业有需要你处理的变化时发系统通知。
客户端只是又一个前端，所有数据和操作都走网页服务的接口，不直接读写 `~/.agent-dispatch/`；
关闭窗口不会停止网页服务，也不影响任何作业。

## 功能

- **自动拉起服务**：启动时检查 `<地址>/api/overview`，连不上就自动执行
  `agent-dispatch web start` 拉起服务（端口跟配置的地址走），然后重试若干次；仍失败则显示具体原因和「重试」按钮。
  因为从图形界面启动的应用拿不到 shell 的 PATH（登录 shell 不读 `~/.zshrc`），客户端自己找命令：
  ① 设置里指定的路径（存在且可执行才用）；② 常见位置（`$HOME/.local/bin`、`/usr/local/bin`、
  `/opt/homebrew/bin`、`$HOME/bin`）；③ 问 shell（`$SHELL -lc` 再 `-ic`，各 5 秒超时）。
  找到后写回设置，下次直接用；执行时不再经过 shell，子进程 PATH 会补上常见 bin 目录。
  拉起失败（找不到命令 / 命令报错 / 启动后仍连不上）时本地加载页会显示具体原因，可重试或在设置里指定路径。
  Windows 上不做自动拉起，只提示手动启动（见下）。
- **系统通知**（Rust 侧轮询，与网页脚本无关）：作业从「运行中」变为
  待审查 / 运行失败 / 意外中断 / 已叫停，或出现「待你决定」标记时，弹系统通知
  （标题如「作业 xxx 待审查」，正文带 STATUS 和任务摘要）。启动后第一次拉取只记基线不发通知；
  连接断开时不刷屏，恢复后继续。
- **设置**：服务地址、agent-dispatch 路径（留空则自动查找，页面会显示当前自动找到的路径）、是否开启通知，
  存在应用配置目录的 `settings.json`
  （macOS：`~/Library/Application Support/dev.agentdispatch.client/`）。托盘菜单 →「设置…」。
- **菜单 / 托盘**：重新加载、在浏览器中打开、设置…、退出。

## 前置条件

- Rust（stable，`rustup` 安装即可）
- Node.js 18+ 与 pnpm
- Linux 还需要 webkit2gtk 等系统库，见
  [Tauri 官方文档](https://tauri.app/start/prerequisites/)，Debian/Ubuntu 大致是：
  `sudo apt install libwebkit2gtk-4.1-dev build-essential curl wget file libxdo-dev libssl-dev libayatana-appindicator3-dev librsvg2-dev`

## 开发运行

```bash
cd client
pnpm install
pnpm tauri dev
```

前端只有 `src/` 下的两个原生 HTML 页面（本地加载页 + 设置页），没有构建步骤、没有前端框架；
正常情况下主窗口会直接跳到服务地址。

## 打包

```bash
cd client
pnpm tauri build          # 发布版
# 或只要调试版（更快）：pnpm tauri build --debug
```

- macOS 产物：`src-tauri/target/release/bundle/macos/派工总览.app`、
  `dmg/派工总览_<版本>_aarch64.dmg`（`--debug` 时在 `target/debug/bundle/` 下）
- Windows 产物：`src-tauri/target/release/bundle/nsis/*.exe`、`msi/*.msi`
- Linux 产物：`src-tauri/target/release/bundle/{deb,appimage,rpm}/`

## 各平台注意事项

- **macOS**：无特殊要求；通知首次会请求系统授权。
- **Linux**：需先装 webkit2gtk 等依赖（见上）；AppImage 里的通知在部分桌面环境需要
  `org.freedesktop.Notification` 服务（一般桌面自带）。
- **Windows**：agent-dispatch 核心目前不支持 Windows，需要在 **WSL** 里运行服务
  （`agent-dispatch web start`），客户端连 WSL 的地址（WSL2 下从 Windows 访问通常用
  `http://<WSL主机名或IP>:8765`，可在 WSL 里 `agent-dispatch web start --host 0.0.0.0` 后用
  `hostname -I` 查 IP）。客户端本身不做自动拉起，请在 WSL 里手动启动服务。

## 目录结构

```
client/
├── src/                 前端本地页面（加载页 index.html、设置页 settings.html）
├── src-tauri/           Rust 工程（主程序、托盘、菜单、通知、设置）
├── app-icon.png         图标源图（pnpm tauri icon 用）
└── package.json         只有 @tauri-apps/cli 一个依赖
```

构建产物与依赖目录（`node_modules/`、`src-tauri/target/`）已在 `client/.gitignore` 里忽略。
