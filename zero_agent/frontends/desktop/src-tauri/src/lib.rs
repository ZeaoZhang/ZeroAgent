use rand::{rngs::OsRng, RngCore};
use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};
use tauri::Manager;

#[cfg(windows)]
use std::os::windows::process::CommandExt;

const BRIDGE_HOST: &str = "127.0.0.1";
const BRIDGE_PORT: u16 = 14168;
const OCCUPIED_UNAUTHENTICATED_ERROR: &str = "Bridge port 14168 is occupied by an unauthenticated process";

static BRIDGE_PROCESS: Mutex<Option<Child>> = Mutex::new(None);
static BRIDGE_TOKEN: Mutex<Option<String>> = Mutex::new(None);

fn bridge_process_lock() -> std::sync::MutexGuard<'static, Option<Child>> {
    BRIDGE_PROCESS.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

fn bridge_token_lock() -> std::sync::MutexGuard<'static, Option<String>> {
    BRIDGE_TOKEN.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// Get project root (parent of zero_agent/)
fn project_root() -> PathBuf {
    std::env::current_exe()
        .expect("cannot get exe path")
        .parent().expect("cannot get exe dir")
        .parent().expect("cannot get project root") // project root
        .to_path_buf()
}

fn bridge_script_for_project(project_dir: &PathBuf) -> PathBuf {
    let source_layout = project_dir.join("zero_agent").join("frontends").join("desktop_bridge.py");
    if source_layout.exists() {
        return source_layout;
    }
    let packaged_layout = project_dir.join("frontends").join("desktop_bridge.py");
    if packaged_layout.exists() {
        return packaged_layout;
    }
    project_dir.join("desktop_bridge.py")
}

/// Find python executable:
/// 1. .portable/uv-python/ 下找 python.exe (Windows) 或 python3 (Unix)
/// 2. Fallback to system PATH
fn find_python() -> String {
    let root = project_root();
    let portable_python_dir = root.join(".portable").join("uv-python");

    if portable_python_dir.exists() {
        // uv installs python like: uv-python/cpython-3.12.x-windows-x86_64/python.exe
        // We need to search for python.exe inside subdirectories
        if let Ok(entries) = std::fs::read_dir(&portable_python_dir) {
            for entry in entries.flatten() {
                let path = entry.path();
                if path.is_dir() {
                    #[cfg(windows)]
                    {
                        let py = path.join("python.exe");
                        if py.exists() {
                            return py.to_string_lossy().to_string();
                        }
                    }
                    #[cfg(not(windows))]
                    {
                        let py = path.join("bin").join("python3");
                        if py.exists() {
                            return py.to_string_lossy().to_string();
                        }
                    }
                }
            }
        }
    }

    // Fallback: system PATH
    #[cfg(windows)]
    { "python".to_string() }
    #[cfg(not(windows))]
    { "python3".to_string() }
}

/// Find project directory by searching upward for ZeroAgent project markers.
fn find_project_dir() -> Option<String> {
    let exe = std::env::current_exe().ok()?;
    let mut dir = exe.parent();
    // Walk up to 8 levels from exe location
    for _ in 0..8 {
        match dir {
            Some(d) => {
                let pyproject = d.join("pyproject.toml");
                let package_dir = d.join("zero_agent");
                if package_dir.is_dir() && pyproject.exists() {
                    return Some(d.to_string_lossy().to_string());
                }
                dir = d.parent();
            }
            None => break,
        }
    }
    None
}

/// Settings file path: ~/.zero_agent_desktop_settings.json
fn settings_path() -> PathBuf {
    dirs::home_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join(".zero_agent_desktop_settings.json")
}

fn read_settings(path: &PathBuf) -> Option<(String, String)> {
    if !path.exists() {
        return None;
    }
    let content = std::fs::read_to_string(path).ok()?;
    let val = serde_json::from_str::<serde_json::Value>(&content).ok()?;
    let python = val.get("python_path")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let project = val.get("project_dir")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    if !python.is_empty() && !project.is_empty() {
        Some((python, project))
    } else {
        None
    }
}

/// Read config from settings file, or auto-discover and save
pub fn get_or_discover_config() -> (String, String) {
    let path = settings_path();

    // Try reading existing settings
    if let Some(config) = read_settings(&path) {
        return config;
    }
    // Auto-discover
    let python = find_python();
    let project = find_project_dir().unwrap_or_default();

    // Save discovered config
    if !python.is_empty() && !project.is_empty() {
        let json = serde_json::json!({
            "python_path": python,
            "project_dir": project
        });
        let _ = std::fs::write(&path, serde_json::to_string_pretty(&json).unwrap());
    }

    (python, project)
}

fn generate_bridge_token() -> String {
    let mut bytes = [0u8; 32];
    OsRng.fill_bytes(&mut bytes);

    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut token = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        token.push(HEX[(byte >> 4) as usize] as char);
        token.push(HEX[(byte & 0x0f) as usize] as char);
    }
    token
}

fn configured_bridge_token() -> (String, bool) {
    if let Ok(token) = std::env::var("ZA_DESKTOP_BRIDGE_TOKEN") {
        let token = token.trim().to_string();
        if !token.is_empty() {
            *bridge_token_lock() = Some(token.clone());
            return (token, true);
        }
    }

    let mut guard = bridge_token_lock();
    if let Some(token) = guard.as_ref() {
        return (token.clone(), false);
    }

    let token = generate_bridge_token();
    *guard = Some(token.clone());
    (token, false)
}

fn bridge_url_with_token(token: &str) -> tauri::Url {
    let encoded = urlencoding::encode(token);
    tauri::Url::parse(&format!("http://{}:{}/#token={}", BRIDGE_HOST, BRIDGE_PORT, encoded)).unwrap()
}

fn is_bridge_running() -> bool {
    TcpStream::connect((BRIDGE_HOST, BRIDGE_PORT)).is_ok()
}

fn owned_bridge_running() -> bool {
    let mut guard = bridge_process_lock();
    if let Some(child) = guard.as_mut() {
        if matches!(child.try_wait(), Ok(None)) {
            return true;
        }
    }
    *guard = None;
    false
}

fn bridge_config_accepts_token(token: &str) -> bool {
    if token.is_empty() {
        return false;
    }

    let mut stream = match TcpStream::connect((BRIDGE_HOST, BRIDGE_PORT)) {
        Ok(stream) => stream,
        Err(_) => return false,
    };
    let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(2)));

    let request = format!(
        "GET /config HTTP/1.1\r\nHost: {}:{}\r\nAuthorization: Bearer {}\r\nConnection: close\r\n\r\n",
        BRIDGE_HOST, BRIDGE_PORT, token
    );
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }

    let mut response = String::new();
    if stream.read_to_string(&mut response).is_err() {
        return false;
    }
    response.starts_with("HTTP/1.1 200") || response.starts_with("HTTP/1.0 200")
}

fn wait_for_authenticated_bridge(token: &str, timeout: Duration) -> bool {
    let start = Instant::now();
    while start.elapsed() < timeout {
        if bridge_config_accepts_token(token) {
            return true;
        }
        thread::sleep(Duration::from_millis(100));
    }
    false
}

fn wait_for_bridge_port_to_clear(timeout: Duration) -> bool {
    let start = Instant::now();
    while is_bridge_running() {
        if start.elapsed() >= timeout {
            return false;
        }
        thread::sleep(Duration::from_millis(50));
    }
    true
}

fn start_bridge_process(python_path: &str, project_dir: &PathBuf, token: &str) -> Result<(), String> {
    let py = PathBuf::from(python_path);
    let script = bridge_script_for_project(project_dir);
    if !script.exists() {
        return Err(format!("desktop_bridge.py not found at {:?}", script));
    }

    let mut cmd = Command::new(&py);
    cmd.arg(&script)
        .current_dir(script.parent().unwrap_or(project_dir))
        .env("ZA_DESKTOP_BRIDGE_NO_BROWSER", "1")
        .env("ZA_DESKTOP_BRIDGE_TOKEN", token)
        .env("ZA_DESKTOP_PARENT_PID", std::process::id().to_string());
    #[cfg(windows)]
    cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
    let child = cmd.spawn().map_err(|e| format!("Failed to spawn: {}", e))?;
    *bridge_process_lock() = Some(child);
    Ok(())
}

fn stop_owned_bridge() {
    let mut guard = bridge_process_lock();
    if let Some(mut child) = guard.take() {
        let _ = child.kill();
        let _ = child.wait();
    }
    *bridge_token_lock() = None;
}

fn ensure_bridge_ready(python_path: &str, project_dir: &str, timeout: Duration) -> Result<(), String> {
    let (token, token_explicit) = configured_bridge_token();

    if owned_bridge_running() {
        if wait_for_authenticated_bridge(&token, timeout) {
            return Ok(());
        }
        return Err("Bridge did not become ready within 20s".into());
    }

    if is_bridge_running() {
        if token_explicit && bridge_config_accepts_token(&token) {
            return Ok(());
        }
        if !wait_for_bridge_port_to_clear(Duration::from_secs(2)) {
            return Err(OCCUPIED_UNAUTHENTICATED_ERROR.into());
        }
    }

    let dir = PathBuf::from(project_dir);
    start_bridge_process(python_path, &dir, &token)?;
    if wait_for_authenticated_bridge(&token, timeout) {
        Ok(())
    } else {
        Err("Bridge did not become ready within 20s".into())
    }
}

fn navigate_main_window(app_handle: &tauri::AppHandle) {
    if let Some(main_win) = app_handle.get_webview_window("main") {
        let (token, _) = configured_bridge_token();
        let _ = main_win.navigate(bridge_url_with_token(&token));
        let _ = main_win.show();
        let _ = main_win.set_focus();
    }
}

fn show_setup_error(app_handle: &tauri::AppHandle, message: &str) {
    if let Some(setup_win) = app_handle.get_webview_window("setup") {
        let script = format!(
            "const s=document.getElementById('status');if(s){{s.className='err';s.textContent={};}}",
            serde_json::to_string(message).unwrap_or_else(|_| "\"\"".to_string())
        );
        let _ = setup_win.eval(&script);
    }
}

#[tauri::command(rename_all = "snake_case")]
fn start_bridge_with_config(app_handle: tauri::AppHandle, python_path: String, project_dir: String) -> Result<(), String> {
    // Save to settings without persisting any bridge token.
    let path = settings_path();
    let obj = serde_json::json!({
        "python_path": python_path,
        "project_dir": project_dir
    });
    std::fs::write(&path, serde_json::to_string_pretty(&obj).unwrap())
        .map_err(|e| format!("Failed to write settings: {}", e))?;

    ensure_bridge_ready(&python_path, &project_dir, Duration::from_secs(20))?;
    navigate_main_window(&app_handle);
    if let Some(setup_win) = app_handle.get_webview_window("setup") {
        let _ = setup_win.hide();
    }

    Ok(())
}

#[tauri::command]
fn get_config() -> (String, String) {
    get_or_discover_config()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let args: Vec<String> = std::env::args().collect();
    let no_autostart = args.iter().any(|a| a == "--no-autostart");
    let dev_mode = args.iter().any(|a| a == "--dev");
    let (bridge_token, token_explicit) = configured_bridge_token();

    let mut spawned_bridge = false;
    let mut startup_error: Option<String> = None;

    if !no_autostart {
        let (py_str, dir_str) = get_or_discover_config();
        let dir = PathBuf::from(&dir_str);
        let script = bridge_script_for_project(&dir);
        if script.exists() {
            if is_bridge_running() {
                if !(token_explicit && bridge_config_accepts_token(&bridge_token))
                    && !wait_for_bridge_port_to_clear(Duration::from_secs(2))
                {
                    startup_error = Some(OCCUPIED_UNAUTHENTICATED_ERROR.to_string());
                }
            }
            if startup_error.is_none() && !is_bridge_running() {
                match start_bridge_process(&py_str, &dir, &bridge_token) {
                    Ok(()) => spawned_bridge = true,
                    Err(err) => startup_error = Some(err),
                }
            }
        }
    }

    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.unminimize();
                let _ = w.show();
                let _ = w.set_focus();
            }
        }))
        .invoke_handler(tauri::generate_handler![start_bridge_with_config, get_config])
        .setup(move |app| {
            let bridge_wait = if spawned_bridge {
                Duration::from_secs(20)
            } else {
                Duration::from_secs(2)
            };
            let bridge_ready = startup_error.is_none() && wait_for_authenticated_bridge(&bridge_token, bridge_wait);
            if bridge_ready {
                // Navigate to bridge HTTP only after authenticated /config succeeds; the window starts on loading.html
                // so WebView never caches an early "connection refused" error page.
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.navigate(bridge_url_with_token(&bridge_token));
                    if dev_mode {
                        w.open_devtools();
                    } else {
                        // Disable F5/F12/Ctrl+R/right-click in production
                        let _ = w.eval(r#"
                            document.addEventListener('keydown', function(e) {
                                if (e.key === 'F12' || e.key === 'F5' ||
                                    (e.ctrlKey && e.key === 'r') ||
                                    (e.ctrlKey && e.shiftKey && e.key === 'I')) {
                                    e.preventDefault();
                                }
                            });
                            document.addEventListener('contextmenu', function(e) {
                                e.preventDefault();
                            });
                        "#);
                    }
                    let _ = w.show();
                }
            } else {
                // Show setup window
                if let Some(w) = app.get_webview_window("setup") {
                    if dev_mode {
                        w.open_devtools();
                    }
                    let _ = w.show();
                }
                if let Some(err) = startup_error.as_deref() {
                    show_setup_error(app.handle(), err);
                }
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                let label = window.label();
                if label == "main" {
                    stop_owned_bridge();
                    // Main closed -> exit app
                    window.app_handle().exit(0);
                } else if label == "setup" {
                    // Setup closed -> exit if main is not visible
                    if let Some(main_win) = window.app_handle().get_webview_window("main") {
                        if !main_win.is_visible().unwrap_or(false) {
                            stop_owned_bridge();
                            window.app_handle().exit(0);
                        }
                    } else {
                        stop_owned_bridge();
                        window.app_handle().exit(0);
                    }
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
