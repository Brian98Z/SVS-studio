//! Tauri desktop host. It owns the local HTTP server sidecar for the full
//! lifetime of the application; the Vue frontend uses the sidecar origin.

use std::io::ErrorKind;
use std::path::PathBuf;
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};
use tauri::{Manager, RunEvent};

#[cfg(windows)]
use std::sync::atomic::{AtomicIsize, Ordering};
#[cfg(windows)]
use windows_sys::Win32::Foundation::{HWND, LPARAM, RECT};
#[cfg(windows)]
use windows_sys::core::BOOL;
#[cfg(windows)]
use windows_sys::Win32::UI::WindowsAndMessaging::{EnumWindows, GetClassNameW, GetClientRect, GetParent, GetWindow, GetWindowRect, GetWindowThreadProcessId, IsWindow, IsWindowVisible, MoveWindow, SetParent, SetWindowLongPtrW, ShowWindow, GW_CHILD, GW_HWNDNEXT, GWL_STYLE, SW_HIDE, SW_SHOW, WS_CHILD, WS_CLIPSIBLINGS, WS_VISIBLE};

#[cfg(windows)]
#[derive(Default)]
struct EmbeddedVstWindow(Mutex<Option<EmbeddedWindow>>);
#[cfg(windows)]
struct EmbeddedWindow { hwnd: isize, host_hwnd: isize, container_hwnd: isize, session_id: String, worker_pid: u32, revision: u64, previous_parent: isize, previous_style: isize, previous_region: isize, host_rect: RECT, content_rect: RECT }

#[cfg(windows)]
impl Drop for EmbeddedVstWindow {
    fn drop(&mut self) {
        if let Ok(mut guard) = self.0.lock() {
            if let Some(item) = guard.take() {
                if item.previous_region != 0 {
                    unsafe { windows_sys::Win32::Graphics::Gdi::DeleteObject(item.previous_region as _); }
                }
            }
        }
    }
}
#[cfg(windows)]
struct OwnedServerPid(Mutex<Option<u32>>);
#[cfg(windows)]
static ENUM_TARGET_PID: AtomicIsize = AtomicIsize::new(0);

#[cfg(windows)]
unsafe extern "system" fn find_worker_window(hwnd: HWND, _: LPARAM) -> BOOL {
    let pid = ENUM_TARGET_PID.load(Ordering::Relaxed) as u32;
    let mut owner = 0;
    GetWindowThreadProcessId(hwnd, &mut owner);
    if owner != pid || IsWindowVisible(hwnd) == 0 { return 1; }
    let mut class = [0u16; 256];
    let len = GetClassNameW(hwnd, class.as_mut_ptr(), class.len() as i32);
    let class_name = String::from_utf16_lossy(&class[..len.max(0) as usize]);
    if class_name == "JUCE_1" || class_name.starts_with("JUCE_") {
        EMBED_FOUND.store(hwnd as isize, Ordering::Relaxed);
        return 0;
    }
    1
}
#[cfg(windows)]
static EMBED_FOUND: AtomicIsize = AtomicIsize::new(0);
#[cfg(windows)]
fn is_descendant_process(root: u32, candidate: u32) -> bool {
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, PROCESSENTRY32W, TH32CS_SNAPPROCESS};
    use windows_sys::Win32::Foundation::CloseHandle;
    unsafe {
        let snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if snapshot.is_null() || snapshot == -1isize as _ { return false; }
        let mut entries = std::collections::HashMap::new();
        let mut entry: PROCESSENTRY32W = std::mem::zeroed();
        entry.dwSize = std::mem::size_of::<PROCESSENTRY32W>() as u32;
        let mut ok = Process32FirstW(snapshot, &mut entry) != 0;
        while ok { entries.insert(entry.th32ProcessID, entry.th32ParentProcessID); ok = Process32NextW(snapshot, &mut entry) != 0; }
        CloseHandle(snapshot);
        let mut pid = candidate;
        for _ in 0..64 { if pid == root { return true; } match entries.get(&pid) { Some(parent) if *parent != pid => pid = *parent, _ => return false } }
        false
    }
}

#[cfg(windows)]
fn editor_clip(parent: HWND, x: i32, y: i32, width: u32, height: u32, clip_x: i32, clip_y: i32, clip_width: u32, clip_height: u32) -> Result<(windows_sys::Win32::Graphics::Gdi::HRGN, bool), String> {
    if width == 0 || height == 0 || width > i32::MAX as u32 || height > i32::MAX as u32 || clip_x < 0 || clip_y < 0
        || i64::from(clip_x) + i64::from(clip_width) > i64::from(width)
        || i64::from(clip_y) + i64::from(clip_height) > i64::from(height) {
        return Err("invalid editor geometry or clip bounds".into());
    }
    let mut bounds: windows_sys::Win32::Foundation::RECT = unsafe { std::mem::zeroed() };
    if unsafe { GetClientRect(parent, &mut bounds) } == 0 { return Err("cannot read editor parent geometry".into()); }
    let left = i64::from(clip_x).max(-i64::from(x)).max(0);
    let top = i64::from(clip_y).max(-i64::from(y)).max(0);
    let right = (i64::from(clip_x) + i64::from(clip_width)).min(i64::from(bounds.right) - i64::from(x)).min(i64::from(width));
    let bottom = (i64::from(clip_y) + i64::from(clip_height)).min(i64::from(bounds.bottom) - i64::from(y)).min(i64::from(height));
    let visible = right > left && bottom > top;
    let region = unsafe {
        if visible { windows_sys::Win32::Graphics::Gdi::CreateRectRgn(left as i32, top as i32, right as i32, bottom as i32) }
        else { windows_sys::Win32::Graphics::Gdi::CreateRectRgn(0, 0, 0, 0) }
    };
    if region.is_null() { return Err("cannot allocate editor clipping region".into()); }
    Ok((region, visible))
}

#[tauri::command]
#[cfg(windows)]
async fn embed_vst_editor(app: tauri::AppHandle, state: tauri::State<'_, EmbeddedVstWindow>, owned: tauri::State<'_, OwnedServerPid>, session_id: String, worker_pid: u32, revision: u64, expected_hwnd: isize, x: i32, y: i32, width: u32, height: u32, clip_x: i32, clip_y: i32, clip_width: u32, clip_height: u32) -> Result<isize, String> {
    if session_id.is_empty() || worker_pid == 0 || revision == 0 || expected_hwnd == 0 { return Err("invalid editor identity".into()); }
    let server_pid = owned.0.lock().map_err(|_| "server PID lock poisoned")?.ok_or("owned server is not running")?;
    if !is_descendant_process(server_pid, worker_pid) { return Err("editor worker is not a descendant of the owned sidecar".into()); }
    let body = serde_json::json!({"action":"status", "session_id":session_id, "worker_pid":worker_pid, "revision":revision});
    let result = tauri::async_runtime::spawn_blocking(move || {
        let client = reqwest::blocking::Client::builder().timeout(Duration::from_secs(3)).build().map_err(|e| e.to_string())?;
        client.post(format!("{SERVER_ORIGIN}/api/inference/vst/realtime/editor")).json(&body).send().map_err(|e| e.to_string())?
            .error_for_status().map_err(|e| e.to_string())?.json::<serde_json::Value>().map_err(|e| e.to_string())
    }).await.map_err(|e| e.to_string())??;
    if result["state"] != "opening" || result["worker_pid"].as_u64() != Some(worker_pid as u64)
        || result["revision"].as_u64() != Some(revision) || result["editor_hwnd"].as_i64() != Some(expected_hwnd as i64) {
        return Err("owned sidecar did not confirm this live editor HWND/revision".into());
    }
    // hwnd() may synchronously dispatch to the UI thread. Never hold the
    // editor mutex while waiting: UI resize/detach handlers take that mutex.
    let parent = app.get_webview_window("main").ok_or("main window unavailable")?.hwnd().map_err(|e| e.to_string())?;
    let mut guard = state.0.lock().map_err(|_| "editor state lock poisoned")?;
    if guard.is_some() { return Err("an embedded editor is already attached".into()); }
    EMBED_FOUND.store(0, Ordering::Relaxed);
    ENUM_TARGET_PID.store(worker_pid as isize, Ordering::Relaxed);
    unsafe { EnumWindows(Some(find_worker_window), 0); }
    let host = EMBED_FOUND.load(Ordering::Relaxed) as HWND;
    if host.is_null() || host as isize != expected_hwnd { return Err("no matching visible JUCE window belongs to the requested worker".into()); }
    // Pedalboard pumps its editor loop while the DocumentWindow is visible.
    // Reparenting that top-level peer can destroy it; embed its content only.
    let mut hwnd: HWND = std::ptr::null_mut();
    let mut child = unsafe { GetWindow(host, GW_CHILD) };
    while !child.is_null() {
        let mut owner = 0;
        let mut class = [0u16; 256];
        unsafe { GetWindowThreadProcessId(child, &mut owner); }
        let len = unsafe { GetClassNameW(child, class.as_mut_ptr(), class.len() as i32) };
        if owner == worker_pid && String::from_utf16_lossy(&class[..len.max(0) as usize]).starts_with("JUCE_") && unsafe { IsWindowVisible(child) } != 0 {
            if !hwnd.is_null() { return Err("ambiguous JUCE editor content windows".into()); }
            hwnd = child;
        }
        child = unsafe { GetWindow(child, GW_HWNDNEXT) };
    }
    if hwnd.is_null() { return Err("no live JUCE editor content window".into()); }
    let previous_parent = unsafe { GetParent(hwnd) };
    if previous_parent != host { return Err("JUCE content window is not a direct child of its confirmed host".into()); }
    let mut host_rect: RECT = unsafe { std::mem::zeroed() };
    let mut content_rect: RECT = unsafe { std::mem::zeroed() };
    unsafe {
        if GetWindowRect(host, &mut host_rect) == 0 || GetWindowRect(hwnd, &mut content_rect) == 0 { return Err("cannot snapshot editor geometry".into()); }
        windows_sys::Win32::Graphics::Gdi::MapWindowPoints(std::ptr::null_mut(), host, &mut content_rect as *mut RECT as _, 2);
    }
    let (region, _) = editor_clip(parent.0 as HWND, x, y, width, height, clip_x, clip_y, clip_width, clip_height)?;
    let previous_style = unsafe { windows_sys::Win32::UI::WindowsAndMessaging::GetWindowLongPtrW(hwnd, GWL_STYLE) };
    let previous_region = unsafe { windows_sys::Win32::Graphics::Gdi::CreateRectRgn(0, 0, 0, 0) };
    if previous_region.is_null() { unsafe { windows_sys::Win32::Graphics::Gdi::DeleteObject(region); } return Err("cannot allocate plugin window region snapshot".into()); }
    let has_region = unsafe { windows_sys::Win32::Graphics::Gdi::GetWindowRgn(hwnd, previous_region) };
    if has_region == 0 { unsafe { windows_sys::Win32::Graphics::Gdi::DeleteObject(previous_region); } }
    let previous_region = if has_region == 0 { 0 } else { previous_region as isize };
    unsafe { windows_sys::Win32::Foundation::SetLastError(0); }
    let previous = unsafe { SetParent(hwnd, parent.0 as HWND) };
    if previous.is_null() && unsafe { windows_sys::Win32::Foundation::GetLastError() } != 0 {
        unsafe { windows_sys::Win32::Graphics::Gdi::DeleteObject(region); }
        if previous_region != 0 { unsafe { windows_sys::Win32::Graphics::Gdi::DeleteObject(previous_region as _); } }
        return Err("SetParent failed".into());
    }
    unsafe {
        SetWindowLongPtrW(hwnd, GWL_STYLE, (previous_style & !(windows_sys::Win32::UI::WindowsAndMessaging::WS_POPUP as isize)) | WS_CHILD as isize | WS_CLIPSIBLINGS as isize | WS_VISIBLE as isize);
        let moved = MoveWindow(hwnd, x, y, width as i32, height as i32, 1) != 0;
        let move_error = std::io::Error::last_os_error();
        let clipped = moved && windows_sys::Win32::Graphics::Gdi::SetWindowRgn(hwnd, region, 1) != 0;
        let clip_error = std::io::Error::last_os_error();
        if !clipped {
            windows_sys::Win32::Graphics::Gdi::DeleteObject(region);
            SetParent(hwnd, previous);
            SetWindowLongPtrW(hwnd, GWL_STYLE, previous_style);
            let restored_region = if previous_region != 0 { windows_sys::Win32::Graphics::Gdi::SetWindowRgn(hwnd, previous_region as _, 1) != 0 } else { windows_sys::Win32::Graphics::Gdi::SetWindowRgn(hwnd, std::ptr::null_mut(), 1) != 0 };
            if !restored_region && previous_region != 0 { windows_sys::Win32::Graphics::Gdi::DeleteObject(previous_region as _); }
            MoveWindow(hwnd, content_rect.left, content_rect.top, content_rect.right - content_rect.left, content_rect.bottom - content_rect.top, 1);
            return Err(format!("cannot position or clip editor window (move={moved}, move_error={move_error}, clip_error={clip_error}); original parent restored"));
        }
        // JUCE treats hiding its host window as closing the editor. An empty
        // region clips all pixels without changing the editor's visibility state.
        ShowWindow(hwnd, SW_SHOW);
        // Keep the original peer visible to JUCE, but outside the desktop.
        if MoveWindow(host, -32000, -32000, host_rect.right - host_rect.left, host_rect.bottom - host_rect.top, 1) == 0 {
            SetParent(hwnd, previous);
            SetWindowLongPtrW(hwnd, GWL_STYLE, previous_style);
            let restored_region = if previous_region != 0 { windows_sys::Win32::Graphics::Gdi::SetWindowRgn(hwnd, previous_region as _, 1) != 0 } else { windows_sys::Win32::Graphics::Gdi::SetWindowRgn(hwnd, std::ptr::null_mut(), 1) != 0 };
            if !restored_region && previous_region != 0 { windows_sys::Win32::Graphics::Gdi::DeleteObject(previous_region as _); }
            MoveWindow(hwnd, content_rect.left, content_rect.top, content_rect.right - content_rect.left, content_rect.bottom - content_rect.top, 1);
            MoveWindow(host, host_rect.left, host_rect.top, host_rect.right - host_rect.left, host_rect.bottom - host_rect.top, 1);
            return Err("cannot park editor host window".into());
        }
    }
    *guard = Some(EmbeddedWindow { hwnd: hwnd as isize, host_hwnd: host as isize, container_hwnd: parent.0 as isize, session_id, worker_pid, revision, previous_parent: previous as isize, previous_style, previous_region, host_rect, content_rect });
    // The IPC result confirms the worker-reported host, not its internal child.
    Ok(host as isize)
}
#[tauri::command]
#[cfg(windows)]
fn resize_vst_editor(app: tauri::AppHandle, state: tauri::State<'_, EmbeddedVstWindow>, session_id: String, worker_pid: u32, revision: u64, x: i32, y: i32, width: u32, height: u32, clip_x: i32, clip_y: i32, clip_width: u32, clip_height: u32) -> Result<(), String> {
    let guard = state.0.lock().map_err(|_| "editor state lock poisoned")?;
    let item = guard.as_ref().ok_or("no embedded editor")?;
    if item.session_id != session_id || item.worker_pid != worker_pid || item.revision != revision { return Err("stale editor identity".into()); }
    let parent = app.get_webview_window("main").ok_or("main window unavailable")?.hwnd().map_err(|e| e.to_string())?;
    let (region, _) = editor_clip(parent.0 as HWND, x, y, width, height, clip_x, clip_y, clip_width, clip_height)?;
    let hwnd = item.hwnd as HWND;
    let mut owner = 0;
    unsafe {
        if IsWindow(hwnd) == 0 { windows_sys::Win32::Graphics::Gdi::DeleteObject(region); return Err("editor window closed".into()); }
        if parent.0 as isize != item.container_hwnd || GetParent(hwnd) != parent.0 as HWND { windows_sys::Win32::Graphics::Gdi::DeleteObject(region); return Err("editor window parent changed".into()); }
        GetWindowThreadProcessId(hwnd, &mut owner);
    }
    if owner != item.worker_pid { unsafe { windows_sys::Win32::Graphics::Gdi::DeleteObject(region); } return Err("editor ownership changed".into()); }
    unsafe {
        if MoveWindow(hwnd, x, y, width as i32, height as i32, 1) == 0 {
            windows_sys::Win32::Graphics::Gdi::DeleteObject(region);
            return Err("cannot position editor window".into());
        }
        if windows_sys::Win32::Graphics::Gdi::SetWindowRgn(hwnd, region, 1) == 0 {
            windows_sys::Win32::Graphics::Gdi::DeleteObject(region);
            return Err("cannot apply editor clipping region".into());
        }
        ShowWindow(hwnd, SW_SHOW);
    }
    Ok(())
}

#[tauri::command]
#[cfg(windows)]
fn detach_vst_editor(state: tauri::State<'_, EmbeddedVstWindow>, session_id: String, worker_pid: u32, revision: u64) -> Result<(), String> {
    let mut guard = state.0.lock().map_err(|_| "editor state lock poisoned")?;
    let item = guard.as_ref().ok_or("no embedded editor")?;
    if item.session_id != session_id || item.worker_pid != worker_pid || item.revision != revision { return Err("stale editor identity".into()); }
    let hwnd = item.hwnd as HWND;
    let host = item.host_hwnd as HWND;
    let mut owner = 0;
    let mut host_owner = 0;
    unsafe {
        let editor_live = IsWindow(hwnd) != 0;
        let host_live = IsWindow(host) != 0;
        if host_live { GetWindowThreadProcessId(host, &mut host_owner); }
        let host_owned = host_live && host_owner == item.worker_pid;
        let mut restore_error = None;
        if editor_live {
            let current_parent = GetParent(hwnd);
            let mut current_parent_owner = 0;
            if current_parent == item.container_hwnd as HWND { GetWindowThreadProcessId(current_parent, &mut current_parent_owner); }
            GetWindowThreadProcessId(hwnd, &mut owner);
            if owner != item.worker_pid || current_parent != item.container_hwnd as HWND || current_parent_owner != std::process::id() {
                restore_error = Some("editor parent changed".to_string());
                if item.previous_region != 0 { windows_sys::Win32::Graphics::Gdi::DeleteObject(item.previous_region as _); }
            } else {
                let mut previous_parent_owner = 0;
                let previous_parent = item.previous_parent as HWND;
                if IsWindow(previous_parent) != 0 { GetWindowThreadProcessId(previous_parent, &mut previous_parent_owner); }
                if previous_parent_owner != item.worker_pid {
                    restore_error = Some("original editor host is no longer owned by the sidecar worker".to_string());
                    if item.previous_region != 0 { windows_sys::Win32::Graphics::Gdi::DeleteObject(item.previous_region as _); }
                } else {
                ShowWindow(hwnd, SW_HIDE);
                windows_sys::Win32::Foundation::SetLastError(0);
                let previous = SetParent(hwnd, previous_parent);
                if previous.is_null() && windows_sys::Win32::Foundation::GetLastError() != 0 {
                    restore_error = Some("failed to restore plugin window parent".into());
                    if item.previous_region != 0 { windows_sys::Win32::Graphics::Gdi::DeleteObject(item.previous_region as _); }
                } else if windows_sys::Win32::Graphics::Gdi::SetWindowRgn(hwnd, item.previous_region as _, 1) == 0 {
                    restore_error = Some("cannot restore plugin clipping region".into());
                    if item.previous_region != 0 { windows_sys::Win32::Graphics::Gdi::DeleteObject(item.previous_region as _); }
                } else {
                    SetWindowLongPtrW(hwnd, GWL_STYLE, item.previous_style);
                    MoveWindow(hwnd, item.content_rect.left, item.content_rect.top, item.content_rect.right - item.content_rect.left, item.content_rect.bottom - item.content_rect.top, 1);
                    ShowWindow(hwnd, SW_SHOW);
                }
                }
            }
        } else if item.previous_region != 0 {
            windows_sys::Win32::Graphics::Gdi::DeleteObject(item.previous_region as _);
        }
        if host_owned {
            if MoveWindow(host, item.host_rect.left, item.host_rect.top, item.host_rect.right - item.host_rect.left, item.host_rect.bottom - item.host_rect.top, 1) == 0 && restore_error.is_none() {
                restore_error = Some("cannot restore editor host geometry".to_string());
            }
        } else if restore_error.is_none() {
            restore_error = Some("editor host is no longer owned by the sidecar worker".to_string());
        }
        *guard = None;
        if let Some(error) = restore_error { return Err(error); }
    }
    Ok(())
}
#[cfg(not(windows))]
#[tauri::command]
fn embed_vst_editor(_session_id: String, _worker_pid: u32, _revision: u64, _expected_hwnd: isize, _x: i32, _y: i32, _width: u32, _height: u32, _clip_x: i32, _clip_y: i32, _clip_width: u32, _clip_height: u32) -> Result<isize, String> { Err("native VST window embedding is Windows-only".into()) }
#[cfg(not(windows))]
#[tauri::command]
fn resize_vst_editor(_session_id: String, _worker_pid: u32, _revision: u64, _x: i32, _y: i32, _width: u32, _height: u32, _clip_x: i32, _clip_y: i32, _clip_width: u32, _clip_height: u32) -> Result<(), String> { Err("native VST window embedding is Windows-only".into()) }
#[cfg(not(windows))]
#[tauri::command]
fn detach_vst_editor(_session_id: String, _worker_pid: u32, _revision: u64) -> Result<(), String> { Err("native VST window embedding is Windows-only".into()) }
const SERVER_ADDR: &str = "127.0.0.1:8761";
const SERVER_ORIGIN: &str = "http://127.0.0.1:8761";

fn server_origin() -> String {
    if cfg!(debug_assertions) {
        std::env::var("SVS_API_ORIGIN").unwrap_or_else(|_| SERVER_ORIGIN.to_string())
    } else {
        SERVER_ORIGIN.to_string()
    }
}

#[derive(Default)]
struct ServerProcess(Mutex<Option<Child>>);

#[tauri::command]
fn server_api_origin() -> String {
    server_origin()
}

fn bundled_server_path(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    let name = if cfg!(windows) { "svs-server.exe" } else { "svs-server" };
    app.path().resource_dir()
        .map(|dir| dir.join("runtime").join(name))
        .map_err(|e| format!("cannot resolve application resources: {e}"))
}

fn start_server(app: &tauri::AppHandle) -> Result<Child, String> {
    match TcpListener::bind(SERVER_ADDR) {
        Ok(listener) => drop(listener),
        Err(e) if e.kind() == ErrorKind::AddrInUse => {
            return Err(format!("{SERVER_ADDR} is already in use; refusing to connect to an unmanaged server"));
        }
        Err(e) => return Err(format!("cannot reserve {SERVER_ADDR}: {e}")),
    }

    let executable = bundled_server_path(app)?;
    if !executable.is_file() {
        return Err(format!("server sidecar is missing: {} (run scripts/package_desktop.ps1 first)", executable.display()));
    }

    let mut command = Command::new(&executable);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000); // CREATE_NO_WINDOW: sidecar has no console UI.
    }
    command.current_dir(executable.parent().unwrap_or_else(|| std::path::Path::new(".")))
        .env("SVS_DESKTOP_MODE", "1")
        .env("PYTHONUTF8", "1")
        .env("SVS_DESKTOP_DIST", executable.parent().unwrap_or_else(|| std::path::Path::new(".")).join("desktop-dist"))
        .env("SVS_INFERENCE_SERVICE_DIR", executable.parent().unwrap_or_else(|| std::path::Path::new(".")).join("inference-service"))
        .env("SVS_G2P_DIR", executable.parent().unwrap_or_else(|| std::path::Path::new(".")).join("g2p-engine"))
        .env("SVS_INTERCHANGE_BRIDGE", executable.parent().unwrap_or_else(|| std::path::Path::new(".")).join("interchange").join("bridge.py"))
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    for name in ["SVS_MODEL_DIR", "SVS_GPU_PYTHON", "SVS_VOICEBANK"] {
        if let Some(value) = std::env::var_os(name).filter(|value| !value.is_empty()) {
            command.env(name, value);
        }
    }
    command.spawn().map_err(|e| format!("failed to start {}: {e}", executable.display()))
}

fn wait_for_server(child: &mut Child) -> Result<(), String> {
    let deadline = Instant::now() + Duration::from_secs(20);
    loop {
        if let Some(status) = child.try_wait().map_err(|e| format!("cannot inspect server sidecar: {e}"))? {
            return Err(format!("server sidecar exited before becoming ready: {status}"));
        }
        if TcpStream::connect_timeout(&SERVER_ADDR.parse::<SocketAddr>().unwrap(), Duration::from_millis(200)).is_ok() {
            return Ok(());
        }
        if Instant::now() >= deadline {
            return Err("server sidecar did not open 127.0.0.1:8761 within 20 seconds".into());
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

fn stop_child_tree(child: &mut Child) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        let status = Command::new("taskkill")
            .creation_flags(0x08000000)
            .args(["/PID", &child.id().to_string(), "/T", "/F"])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
        if status.map(|s| s.success()).unwrap_or(false) {
            let _ = child.wait();
            return;
        }
    }
    let _ = child.kill();
    let _ = child.wait();
}

fn stop_server(process: &ServerProcess) {
    if let Ok(mut guard) = process.0.lock() {
        if let Some(mut child) = guard.take() {
            stop_child_tree(&mut child);
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let process = ServerProcess::default();
    let builder = tauri::Builder::default().manage(process);
    #[cfg(windows)]
    let builder = builder.manage(EmbeddedVstWindow::default()).manage(OwnedServerPid(Mutex::new(None)))
        .invoke_handler(tauri::generate_handler![server_api_origin, embed_vst_editor, resize_vst_editor, detach_vst_editor]);
    #[cfg(not(windows))]
    let builder = builder.invoke_handler(tauri::generate_handler![server_api_origin, embed_vst_editor, resize_vst_editor, detach_vst_editor]);
    let app = builder
        .setup(|app| {
            let mut child = start_server(&app.handle()).map_err(std::io::Error::other)?;
            if let Err(error) = wait_for_server(&mut child) {
                stop_child_tree(&mut child);
                return Err(std::io::Error::other(error).into());
            }
            *app.state::<ServerProcess>().0.lock().map_err(|_| std::io::Error::other("server process lock poisoned"))? = Some(child);
            #[cfg(windows)]
            {
                *app.state::<OwnedServerPid>().0.lock().map_err(|_| std::io::Error::other("server PID lock poisoned"))? = Some(app.state::<ServerProcess>().0.lock().map_err(|_| std::io::Error::other("server process lock poisoned"))?.as_ref().ok_or("sidecar process missing")?.id());
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build Tauri application");

    app.run(|app_handle, event| {
        if matches!(event, RunEvent::Exit | RunEvent::ExitRequested { .. }) {
            stop_server(&app_handle.state::<ServerProcess>());
        }
    });
}
