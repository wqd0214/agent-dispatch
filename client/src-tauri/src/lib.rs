use std::fs;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::menu::{Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Emitter, Manager, Url, WebviewWindowBuilder};

mod find;
use find::{common_candidates, pick_existing, pick_from_shell_output};
use std::path::{Path, PathBuf};

/// 需要发通知的状态：作业从「运行中」变到这些状态之一
const NOTIFY_STATES: [&str; 4] = ["done", "failed", "interrupted", "stopped"];

#[derive(Clone, Serialize, Deserialize)]
struct Settings {
    server_url: String,
    /// 用户在设置里指定的 agent-dispatch 路径，留空则自动查找
    #[serde(default)]
    agent_dispatch_path: String,
    notify: bool,
}

impl Default for Settings {
    fn default() -> Self {
        Settings { server_url: "http://127.0.0.1:8765".into(), agent_dispatch_path: String::new(), notify: true }
    }
}

struct AppState {
    settings: Mutex<Settings>,
    /// 本地 index.html 的绝对地址：改完设置后把主窗口导回本地加载页
    index_url: Mutex<Option<Url>>,
    /// 线程代号：设置变化后旧线程自行退出
    generation: AtomicU64,
}

type State<'a> = tauri::State<'a, AppState>;

fn settings_path(app: &AppHandle) -> tauri::Result<std::path::PathBuf> {
    let dir = app.path().app_config_dir()?;
    Ok(dir.join("settings.json"))
}

fn load_settings(app: &AppHandle) -> Settings {
    match settings_path(app).and_then(|p| Ok(fs::read_to_string(p)?)) {
        Ok(text) => serde_json::from_str(&text).unwrap_or_default(),
        Err(_) => Settings::default(),
    }
}

fn save_settings_file(app: &AppHandle, s: &Settings) -> tauri::Result<()> {
    let path = settings_path(app)?;
    if let Some(dir) = path.parent() {
        fs::create_dir_all(dir)?;
    }
    fs::write(&path, serde_json::to_string_pretty(s)?)?;
    Ok(())
}

fn normalize_url(raw: &str) -> String {
    let mut url = raw.trim().trim_end_matches('/').to_string();
    if !url.starts_with("http://") && !url.starts_with("https://") {
        url = format!("http://{url}");
    }
    url
}

// ---------- 服务连通性与自动拉起 ----------

fn overview_reachable(url: &str) -> bool {
    ureq::AgentBuilder::new().timeout(Duration::from_secs(4)).build()
        .get(&format!("{url}/api/overview")).call().is_ok()
}

/// 文件存在且有可执行位（Windows 不自动拉起，不会走到这里）
#[cfg(unix)]
fn is_exec_file(p: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    p.is_file() && fs::metadata(p).map(|m| m.permissions().mode() & 0o111 != 0).unwrap_or(false)
}

/// 带 5 秒超时跑一条 shell 命令，返回 stdout；超时或失败返回 None
fn run_shell_with_timeout(shell: &str, flag: &str, cmd: &str) -> Option<String> {
    let mut child = std::process::Command::new(shell).arg(flag).arg(cmd)
        .stdout(std::process::Stdio::piped()).stderr(std::process::Stdio::null())
        .spawn().ok()?;
    for _ in 0..100 {
        match child.try_wait() {
            Ok(Some(status)) if status.success() => {
                use std::io::Read;
                let mut out = String::new();
                if let Some(mut s) = child.stdout.take() { let _ = s.read_to_string(&mut out); }
                return Some(out);
            }
            Ok(Some(_)) => return None,
            Ok(None) => std::thread::sleep(Duration::from_millis(50)),
            Err(_) => { let _ = child.kill(); return None; }
        }
    }
    let _ = child.kill(); // 交互 shell 可能卡住等输入，5 秒后放弃
    None
}

/// 自己找 agent-dispatch，不依赖登录 shell 的 PATH：
/// ① 设置里指定的路径；② 常见安装位置；③ 问 shell（-lc 再 -ic）
fn find_agent_dispatch(configured: &str) -> Option<PathBuf> {
    let configured = configured.trim();
    #[cfg(unix)]
    let check = is_exec_file;
    #[cfg(unix)]
    {
        if !configured.is_empty() {
            let p = PathBuf::from(configured);
            if check(&p) { return Some(p); }
        }
        if let Some(home) = std::env::var("HOME").ok().filter(|h| !h.trim().is_empty()) {
            if let Some(p) = pick_existing(common_candidates(&home), check) { return Some(p); }
        }
        let shell = std::env::var("SHELL").ok().filter(|s| !s.is_empty()).unwrap_or_else(|| "/bin/zsh".into());
        for flag in ["-lc", "-ic"] {
            if let Some(out) = run_shell_with_timeout(&shell, flag, "command -v agent-dispatch") {
                if let Some(p) = pick_from_shell_output(&out, check) { return Some(p); }
            }
        }
    }
    let _ = configured;
    None
}

/// 把找到的路径写回设置，下次直接用
fn remember_dispatch_path(app: &AppHandle, path: &Path) {
    let state = app.state::<AppState>();
    let mut s = state.settings.lock().unwrap();
    if s.agent_dispatch_path != path.to_string_lossy() {
        s.agent_dispatch_path = path.to_string_lossy().into_owned();
        let snapshot = s.clone();
        drop(s);
        let _ = save_settings_file(app, &snapshot);
    }
}

fn emit_service_error(app: &AppHandle, reason: String) {
    eprintln!("[agent-dispatch-client] service-error: {reason}"); // 无头验证/排障时能看到事件内容
    let _ = app.emit("service-error", reason);
}

/// 直接执行找到的路径拉起服务，不经过 shell。
/// 子进程 PATH 前面补上常见 bin 目录，脚本内部的 python3 优先用 Homebrew 的。
fn try_start_service(app: &AppHandle, server_url: &str) -> Result<(), String> {
    if cfg!(windows) {
        return Err("Windows 上不做自动拉起，请在 WSL 里手动运行 agent-dispatch web start".into());
    }
    let (host, port) = match tauri::Url::parse(server_url) {
        Ok(u) => (u.host_str().unwrap_or("127.0.0.1").to_string(), u.port_or_known_default().unwrap_or(8765)),
        Err(_) => ("127.0.0.1".to_string(), 8765),
    };
    let configured = app.state::<AppState>().settings.lock().unwrap().agent_dispatch_path.clone();
    let Some(path) = find_agent_dispatch(&configured) else {
        return Err("找不到 agent-dispatch 命令（设置里可以手动指定路径）".into());
    };
    remember_dispatch_path(app, &path);
    let extra = format!("/usr/local/bin:/opt/homebrew/bin:{}/.local/bin", std::env::var("HOME").unwrap_or_default());
    let path_env = match std::env::var("PATH") {
        Ok(p) => format!("{extra}:{p}"),
        Err(_) => extra,
    };
    match std::process::Command::new(&path)
        .args(["web", "start", "--host", host.as_str(), "--port", &port.to_string()])
        .env("PATH", path_env)
        .stderr(std::process::Stdio::piped())
        .output()
    {
        Ok(o) if o.status.success() => Ok(()),
        Ok(o) => Err(format!("agent-dispatch 报错：{}", String::from_utf8_lossy(&o.stderr).trim())),
        Err(e) => Err(format!("执行 {} 失败：{e}", path.display())),
    }
}

/// 启动时的连接流程：可达就直接通知前端；否则拉起服务后重试若干次
fn ensure_service(app: AppHandle, url: String, gen: u64) {
    if overview_reachable(&url) {
        let _ = app.emit("service-ready", &url);
        return;
    }
    let start_err = try_start_service(&app, &url).err();
    if let Some(reason) = start_err {
        if !cfg!(windows) {
            emit_service_error(&app, reason); // 拉起已确定失败，不空转重试
            return;
        }
    }
    for _ in 0..15 {
        std::thread::sleep(Duration::from_millis(1500));
        if app.state::<AppState>().generation.load(Ordering::SeqCst) != gen {
            return;
        }
        if overview_reachable(&url) {
            let _ = app.emit("service-ready", &url);
            return;
        }
    }
    if !cfg!(windows) {
        emit_service_error(&app, format!("服务启动后仍连不上 {url}"));
    }
}

// ---------- 状态监控与系统通知 ----------

/// 从 overview 里提取通知需要的字段
struct JobSnap {
    state: String,
    state_label: String,
    status: Option<String>,
    task: String,
    needs_user: bool,
}

fn fetch_snapshot(url: &str) -> Option<Vec<(String, JobSnap)>> {
    let body = ureq::AgentBuilder::new().timeout(Duration::from_secs(4)).build()
        .get(&format!("{url}/api/overview")).call().ok()?
        .into_string().ok()?;
    let json: serde_json::Value = serde_json::from_str(&body).ok()?;
    let jobs = json.get("jobs")?.as_array()?;
    Some(jobs.iter().filter_map(|j| {
        let name = j.get("name")?.as_str()?.to_string();
        // 接口里是问题原文（字符串）；兼容早先的 {question, at} 对象
        let needs_user = match j.get("needs_user") {
            Some(serde_json::Value::String(s)) => !s.trim().is_empty(),
            Some(serde_json::Value::Object(_)) => true,
            _ => false,
        };
        Some((name, JobSnap {
            state: j.get("state")?.as_str()?.to_string(),
            state_label: j.get("state_label").and_then(|v| v.as_str()).unwrap_or("").to_string(),
            status: j.get("status").and_then(|v| v.as_str()).map(String::from),
            task: j.get("task").and_then(|v| v.as_str()).unwrap_or("").to_string(),
            needs_user,
        }))
    }).collect())
}

fn notify(app: &AppHandle, title: String, body: String) {
    use tauri_plugin_notification::NotificationExt;
    let _ = app.notification().builder().title(title).body(body).show();
}

fn task_snippet(task: &str) -> String {
    let one_line: String = task.split_whitespace().collect::<Vec<_>>().join(" ");
    let mut s: String = one_line.chars().take(120).collect();
    if one_line.chars().count() > 120 {
        s.push('…');
    }
    s
}

fn monitor_loop(app: AppHandle, gen: u64) {
    let mut prev: Option<std::collections::HashMap<String, JobSnap>> = None; // 第一次只记基线
    loop {
        std::thread::sleep(Duration::from_secs(5));
        if app.state::<AppState>().generation.load(Ordering::SeqCst) != gen {
            return;
        }
        let settings = app.state::<AppState>().settings.lock().unwrap().clone();
        let Some(curr_list) = fetch_snapshot(&settings.server_url) else {
            continue; // 连接断开不刷屏，恢复后继续
        };
        let curr: std::collections::HashMap<String, JobSnap> = curr_list.into_iter().collect();
        if let (Some(prev), true) = (&prev, settings.notify) {
            for (name, now) in &curr {
                let before = prev.get(name);
                if let Some(before) = before {
                    if before.state == "running" && NOTIFY_STATES.contains(&now.state.as_str()) {
                        notify(&app, format!("作业 {name} {}", now.state_label),
                               format!("STATUS={}\n{}", now.status.clone().unwrap_or_else(|| "-".into()), task_snippet(&now.task)));
                    }
                    if !before.needs_user && now.needs_user {
                        notify(&app, format!("作业 {name} 待你决定"),
                               format!("STATUS={}\n{}", now.status.clone().unwrap_or_else(|| "-".into()), task_snippet(&now.task)));
                    }
                } else if now.needs_user {
                    // 新出现的作业带着「待你决定」标记，也提示
                    notify(&app, format!("作业 {name} 待你决定"),
                           format!("STATUS={}\n{}", now.status.clone().unwrap_or_else(|| "-".into()), task_snippet(&now.task)));
                }
            }
        }
        prev = Some(curr);
    }
}

// ---------- 命令 ----------

#[tauri::command]
fn get_settings(state: State) -> Settings {
    state.settings.lock().unwrap().clone()
}

#[tauri::command]
fn get_server_url(state: State) -> String {
    state.settings.lock().unwrap().server_url.clone()
}

#[tauri::command]
fn try_start_service_cmd(app: AppHandle, state: State) -> bool {
    let url = state.settings.lock().unwrap().server_url.clone();
    let started = try_start_service(&app, &url);
    if let Err(reason) = started {
        if !cfg!(windows) {
            emit_service_error(&app, reason);
        }
    }
    true
}

#[tauri::command]
fn find_dispatch_path_cmd(state: State) -> Option<String> {
    find_agent_dispatch(&state.settings.lock().unwrap().agent_dispatch_path)
        .map(|p| p.to_string_lossy().into_owned())
}

#[tauri::command]
fn save_settings(app: AppHandle, state: State, url: String, agent_dispatch_path: String, notify_enabled: bool) -> Result<(), String> {
    let url = normalize_url(&url);
    let settings = Settings { server_url: url.clone(), agent_dispatch_path: agent_dispatch_path.trim().to_string(), notify: notify_enabled };
    save_settings_file(&app, &settings).map_err(|e| e.to_string())?;
    *state.settings.lock().unwrap() = settings.clone();
    // 换一代线程：新的连接流程 + 新的监控基线
    let gen = state.generation.fetch_add(1, Ordering::SeqCst) + 1;
    std::thread::spawn({
        let handle = app.clone();
        move || ensure_service(handle, url.clone(), gen)
    });
    if let Some(main) = app.get_webview_window("main") {
        // 回到本地加载页重新走连接流程（remote 页面没有 IPC，只能靠 eval）
        let index = app.state::<AppState>().index_url.lock().unwrap().clone();
        if let Some(index) = index {
            let js = format!("location.replace('{}')", index.as_str().replace('\'', "\\'"));
            let _ = main.eval(&js);
        }
    }
    Ok(())
}

// ---------- 菜单 / 托盘 / 窗口 ----------

fn open_settings_window(app: &AppHandle) -> tauri::Result<()> {
    if let Some(win) = app.get_webview_window("settings") {
        win.set_focus()?;
        return Ok(());
    }
    WebviewWindowBuilder::new(app, "settings", tauri::WebviewUrl::App("settings.html".into()))
        .title("设置 · 派工总览").inner_size(480.0, 320.0).resizable(false)
        .build()?;
    Ok(())
}

fn build_menu(app: &AppHandle) -> tauri::Result<Menu<tauri::Wry>> {
    let reload = MenuItem::with_id(app, "reload", "重新加载", true, None::<&str>)?;
    let browser = MenuItem::with_id(app, "browser", "在浏览器中打开", true, None::<&str>)?;
    let settings = MenuItem::with_id(app, "settings", "设置…", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
    Menu::with_items(app, &[&reload, &browser, &settings, &quit])
}

fn handle_menu(app: &AppHandle, id: &str) {
    match id {
        "reload" => {
            if let Some(main) = app.get_webview_window("main") {
                let _ = main.eval("location.reload()");
            }
        }
        "browser" => {
            let url = app.state::<AppState>().settings.lock().unwrap().server_url.clone();
            use tauri_plugin_opener::OpenerExt;
            let _ = app.opener().open_url(url, None::<&str>);
        }
        "settings" => {
            let _ = open_settings_window(app);
        }
        "quit" => app.exit(0),
        _ => {}
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            let handle = app.handle().clone();
            let settings = load_settings(&handle);
            *app.state::<AppState>().settings.lock().unwrap() = settings.clone();
            // 记下本地加载页的绝对地址，之后主窗口可能已经跳到远程页面
            if let Some(main) = app.get_webview_window("main") {
                if let Ok(url) = main.url() {
                    *app.state::<AppState>().index_url.lock().unwrap() = Some(url);
                }
            }
            let gen = app.state::<AppState>().generation.load(Ordering::SeqCst);
            std::thread::spawn({
                let (handle, url) = (handle.clone(), settings.server_url.clone());
                move || ensure_service(handle, url, gen)
            });
            std::thread::spawn({
                let handle = handle.clone();
                move || monitor_loop(handle, gen)
            });

            let menu = build_menu(app.handle())?;
            app.set_menu(menu)?;
            let tray_menu = build_menu(app.handle())?;
            TrayIconBuilder::with_id("main")
                .icon(app.default_window_icon().unwrap().clone())
                .menu(&tray_menu)
                .show_menu_on_left_click(true)
                .on_menu_event(|app, event| handle_menu(app, &event.id().0))
                .build(app.handle())?;
            Ok(())
        })
        .on_menu_event(|app, event| handle_menu(app, &event.id().0))
        .manage(AppState {
            settings: Mutex::new(Settings::default()),
            index_url: Mutex::new(None),
            generation: AtomicU64::new(0),
        })
        .invoke_handler(tauri::generate_handler![get_settings, get_server_url, save_settings, try_start_service_cmd, find_dispatch_path_cmd])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

