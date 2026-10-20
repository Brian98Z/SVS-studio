//! Tauri desktop host. It owns the local HTTP server sidecar for the full
//! lifetime of the application; the Vue frontend uses the sidecar origin.

use std::io::ErrorKind;
use std::path::PathBuf;
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};
use tauri::{Manager, RunEvent};

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
    let app = tauri::Builder::default()
        .manage(process)
        .invoke_handler(tauri::generate_handler![server_api_origin])
        .setup(|app| {
            let mut child = start_server(&app.handle()).map_err(std::io::Error::other)?;
            if let Err(error) = wait_for_server(&mut child) {
                stop_child_tree(&mut child);
                return Err(std::io::Error::other(error).into());
            }
            *app.state::<ServerProcess>().0.lock().map_err(|_| std::io::Error::other("server process lock poisoned"))? = Some(child);
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
