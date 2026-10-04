use std::fs;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::menu::{Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Emitter, Manager, Url, WebviewWindowBuilder};

/// 需要发通知的状态：作业从「运行中」变到这些状态之一
const NOTIFY_STATES: [&str; 4] = ["done", "failed", "interrupted", "stopped"];

#[derive(Clone, Serialize, Deserialize)]
struct Settings {
    server_url: String,
    notify: bool,
}

impl Default for Settings {
    fn default() -> Self {
        Settings { server_url: "http://127.0.0.1:8765".into(), notify: true }
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

/// 用登录 shell 拉起后台服务：图形界面启动的应用拿不到 shell 的 PATH；端口跟着配置的地址走
fn try_start_service(server_url: &str) -> bool {
    if cfg!(windows) {
        return false; // Windows 上提示用户手动启动
    }
    let (host, port) = match tauri::Url::parse(server_url) {
        Ok(u) => (u.host_str().unwrap_or("127.0.0.1").to_string(), u.port_or_known_default().unwrap_or(8765)),
        Err(_) => ("127.0.0.1".to_string(), 8765),
    };
    let shells = [std::env::var("SHELL").ok().filter(|s| !s.is_empty()), Some("/bin/zsh".into()), Some("/bin/bash".into())];
    for shell in shells.into_iter().flatten() {
        let ok = std::process::Command::new(shell)
            .arg("-lc").arg(format!("agent-dispatch web start --host {host} --port {port}"))
            .output()
            .map(|o| o.status.success())
            .unwrap_or(false);
        if ok {
            return true;
        }
    }
    false
}

/// 启动时的连接流程：可达就直接通知前端；否则拉起服务后重试若干次
fn ensure_service(app: AppHandle, url: String, gen: u64) {
    if overview_reachable(&url) {
        let _ = app.emit("service-ready", &url);
        return;
    }
    try_start_service(&url);
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
        let needs_user = j.get("needs_user").and_then(|v| v.as_object()).is_some();
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
fn try_start_service_cmd(state: State) -> bool {
    try_start_service(&state.settings.lock().unwrap().server_url)
}

#[tauri::command]
fn save_settings(app: AppHandle, state: State, url: String, notify_enabled: bool) -> Result<(), String> {
    let url = normalize_url(&url);
    let settings = Settings { server_url: url.clone(), notify: notify_enabled };
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
        .invoke_handler(tauri::generate_handler![get_settings, get_server_url, save_settings, try_start_service_cmd])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
