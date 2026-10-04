#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")] // 发布版不弹控制台窗口

fn main() {
    agent_dispatch_client_lib::run()
}
