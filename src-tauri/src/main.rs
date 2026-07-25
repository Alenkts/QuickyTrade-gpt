use std::io::{Error, ErrorKind, Read, Write};
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr, TcpStream};
#[cfg(unix)]
use std::os::unix::process::CommandExt;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::env;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tauri::Manager;

struct ChildGuard(Child);

impl ChildGuard {
    fn terminate(&mut self) {
        #[cfg(unix)]
        {
            // std::process::Child::kill is SIGKILL on Unix. Give server.mjs a
            // bounded SIGTERM window so it can stop its managed Python core,
            // close SQLite, and release both loopback listeners.
            let _ = Command::new("/bin/kill")
                .args(["-TERM", &format!("-{}", self.0.id())])
                .status();
            for _ in 0..20 {
                if self.0.try_wait().ok().flatten().is_some() {
                    return;
                }
                std::thread::sleep(Duration::from_millis(50));
            }
        }
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

impl Drop for ChildGuard {
    fn drop(&mut self) {
        self.terminate();
    }
}

struct AppState {
    _server_process: Mutex<Option<ChildGuard>>,
}

fn validated_loopback_url(value: &str) -> Result<tauri::Url, Error> {
    let url = tauri::Url::parse(value)
        .map_err(|_| Error::new(ErrorKind::InvalidInput, "desktop server URL is invalid"))?;
    let loopback = matches!(url.host_str(), Some("127.0.0.1") | Some("::1") | Some("[::1]"));
    let clean_origin = matches!(url.scheme(), "http" | "https")
        && url.username().is_empty()
        && url.password().is_none()
        && url.query().is_none()
        && url.fragment().is_none()
        && (url.path().is_empty() || url.path() == "/");
    if !loopback || !clean_origin {
        return Err(Error::new(
            ErrorKind::PermissionDenied,
            "QT_DESKTOP_SERVER_URL must be a credential-free loopback origin",
        ));
    }
    Ok(url)
}

fn service_probe(url: &tauri::Url, instance_token: Option<&str>) -> Result<bool, Error> {
    let port = url
        .port_or_known_default()
        .ok_or_else(|| Error::new(ErrorKind::InvalidInput, "desktop server URL has no port"))?;
    let ip = match url.host_str() {
        Some("127.0.0.1") => IpAddr::V4(Ipv4Addr::LOCALHOST),
        Some("::1") | Some("[::1]") => IpAddr::V6(Ipv6Addr::LOCALHOST),
        _ => return Ok(false),
    };
    let mut stream = TcpStream::connect_timeout(&SocketAddr::new(ip, port), Duration::from_millis(200))?;
    stream.set_read_timeout(Some(Duration::from_millis(300)))?;
    stream.set_write_timeout(Some(Duration::from_millis(300)))?;
    let path = instance_token
        .map(|token| format!("/healthz?instanceToken={token}"))
        .unwrap_or_else(|| "/healthz".to_string());
    let request = format!(
        "GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
    );
    stream.write_all(request.as_bytes())?;
    let mut response = String::new();
    stream.read_to_string(&mut response)?;
    let healthy = response.starts_with("HTTP/1.1 200")
        && response.contains("\"status\":\"ok\"")
        && if let Some(token) = instance_token {
            response.contains("\"listenersReady\":true")
                && response.contains(&format!("\"instanceToken\":\"{token}\""))
        } else {
            true
        };
    Ok(healthy)
}

fn wait_for_service(
    url: &tauri::Url,
    mut child: Option<&mut Child>,
    instance_token: Option<&str>,
) -> Result<(), Error> {
    let deadline = Instant::now() + Duration::from_secs(8);
    loop {
        if let Some(process) = child.as_deref_mut() {
            if let Some(status) = process.try_wait()? {
                return Err(Error::new(
                    ErrorKind::ConnectionRefused,
                    format!("QuickyTrade service exited before readiness ({status})"),
                ));
            }
        }
        if service_probe(url, instance_token).unwrap_or(false) {
            return Ok(());
        }
        if Instant::now() >= deadline {
            return Err(Error::new(
                ErrorKind::TimedOut,
                "QuickyTrade service ownership/readiness probe timed out",
            ));
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

#[tauri::command]
fn get_desktop_runtime() -> serde_json::Value {
    let port = env::var("QT_DESKTOP_PORT").unwrap_or_else(|_| "4173".to_string());
    let server_url = env::var("QT_DESKTOP_SERVER_URL").unwrap_or_else(|_| format!("http://127.0.0.1:{}", port));
    let started = env::var("QT_DESKTOP_SERVER_URL").is_err();
    
    serde_json::json!({
        "serverAddress": server_url,
        "serverOwnership": if started { "DESKTOP" } else { "EXTERNAL" },
        "brokerConnection": "NOT_STARTED_BY_DESKTOP",
        "profileAuthority": format!("{}/api/connection-profiles", server_url),
        "notice": "The loopback service is the only profile authority. This shell never opens an IBKR connection."
    })
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![get_desktop_runtime])
        .setup(|app| {
            let port = env::var("QT_DESKTOP_PORT").unwrap_or_else(|_| "4173".to_string());
            let server_url = env::var("QT_DESKTOP_SERVER_URL").unwrap_or_else(|_| format!("http://127.0.0.1:{}", port));
            let navigation_url = validated_loopback_url(&server_url)?;
            
            if env::var("QT_DESKTOP_SERVER_URL").is_err() {
                // Spawn node server.mjs
                let current_dir = env::current_dir().unwrap_or_default();
                let parent_dir = current_dir.parent().unwrap_or(&current_dir);
                let server_path = parent_dir.join("server.mjs");
                
                let data_dir = app.path().app_data_dir().unwrap_or_default().join("service-data");
                
                let instance_token = format!(
                    "{}-{}",
                    std::process::id(),
                    SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos()
                );
                let mut command = Command::new("node");
                command
                    .arg(server_path)
                    .env("PORT", port)
                    .env("QT_DATA_DIR", data_dir)
                    // server.mjs also watches this owner PID. A dev reload or
                    // abrupt shell termination can bypass Rust Drop handlers;
                    // the watchdog prevents an orphan from serving stale code.
                    .env("QT_DESKTOP_PARENT_PID", std::process::id().to_string())
                    .env("QT_DESKTOP_INSTANCE_TOKEN", &instance_token)
                    .stdin(Stdio::piped());
                #[cfg(unix)]
                command.process_group(0);
                let child = command
                    .spawn()
                    .expect("Failed to start QuickyTrade server");
                let mut child_guard = ChildGuard(child);

                // Prove this exact spawn owns a healthy main listener and has
                // also bound the TradingView ingress before navigation.
                wait_for_service(
                    &navigation_url,
                    Some(&mut child_guard.0),
                    Some(&instance_token),
                )?;
                
                app.manage(AppState {
                    _server_process: Mutex::new(Some(child_guard)),
                });
            } else {
                app.manage(AppState {
                    _server_process: Mutex::new(None),
                });
                wait_for_service(&navigation_url, None, None)?;
            }
            
            // Navigate window
            if let Some(window) = app.get_webview_window("main") {
                window.navigate(navigation_url)?;
            }
            
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[cfg(test)]
mod tests {
    use super::validated_loopback_url;

    #[test]
    fn accepts_literal_loopback_origins() {
        assert!(validated_loopback_url("http://127.0.0.1:4173").is_ok());
        assert!(validated_loopback_url("http://[::1]:4173").is_ok());
    }

    #[test]
    fn rejects_remote_or_script_injectable_urls() {
        assert!(validated_loopback_url("https://example.com").is_err());
        assert!(validated_loopback_url("http://localhost:4173").is_err());
        assert!(validated_loopback_url("http://127.0.0.1:4173/?x=';alert(1)").is_err());
        assert!(validated_loopback_url("http://user:pass@127.0.0.1:4173").is_err());
    }
}
