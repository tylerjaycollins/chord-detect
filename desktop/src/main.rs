//! Overtone as its own Mac app.
//!
//! A thin shell: the analysis stays in Python (`chord_detect.server`), and this
//! app gives it a window and a Dock icon instead of a Safari tab. At launch it
//! picks a free port, starts the server on it, shows a "Starting" screen until
//! the server answers, then loads the page. Quitting the app stops the server.
//!
//! The server runs from the repo's own `.venv`, not a frozen copy, so code
//! edits take effect on the next launch (or the page's Restart) with no rebuild.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs::{File, OpenOptions};
use std::io::Write as _;
use std::net::{Ipv4Addr, SocketAddr, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use tauri::webview::{DownloadEvent, NewWindowResponse};
use tauri::{Manager, RunEvent, Url, WebviewUrl, WebviewWindow, WebviewWindowBuilder};

/// The chord-detect checkout this app was built from. Overridable with
/// `OVERTONE_REPO` in case the folder ever moves without a rebuild.
const BUILT_REPO: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/..");

/// How long to wait for the server before calling it a failure. Models load
/// lazily, so a healthy start answers in a couple of seconds.
const START_TIMEOUT: Duration = Duration::from_secs(60);

/// The running server, so quitting can stop it.
struct Server(Mutex<Option<Child>>);

fn repo_dir() -> PathBuf {
    let dir = std::env::var_os("OVERTONE_REPO")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(BUILT_REPO));
    dir.canonicalize().unwrap_or(dir)
}

fn log_path() -> PathBuf {
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_default();
    home.join("Library/Logs/chord-detect.log")
}

/// Ask the OS for a port nobody is using. There's a moment between dropping
/// the listener and Python binding it, but nothing else on this Mac grabs
/// random high ports, unlike the fixed 8765/8770 that collided before.
fn free_port() -> std::io::Result<u16> {
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0))?;
    Ok(listener.local_addr()?.port())
}

fn open_log() -> std::io::Result<File> {
    let path = log_path();
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    OpenOptions::new().create(true).append(true).open(path)
}

fn start_server(repo: &Path, port: u16) -> Result<Child, String> {
    let program = repo.join(".venv/bin/chord-detect-app");
    if !program.exists() {
        return Err(format!(
            "Overtone's Python setup isn't there. In a terminal, run \
             <code>cd {} && uv sync</code>, then open Overtone again.",
            repo.display()
        ));
    }
    let mut log = open_log().map_err(|e| format!("Couldn't open the log file: {e}"))?;
    let _ = writeln!(log, "--- desktop app launch, port {port} ---");
    let err_log = log
        .try_clone()
        .map_err(|e| format!("Couldn't open the log file: {e}"))?;

    Command::new(&program)
        .current_dir(repo)
        .env("OVERTONE_PORT", port.to_string())
        .env("OVERTONE_NO_BROWSER", "1")
        .env("OVERTONE_PARENT_PID", std::process::id().to_string())
        .stdin(Stdio::null())
        .stdout(log)
        .stderr(err_log)
        .spawn()
        .map_err(|e| format!("Couldn't start the Overtone server: {e}"))
}

fn server_answers(port: u16) -> bool {
    let addr = SocketAddr::from((Ipv4Addr::LOCALHOST, port));
    TcpStream::connect_timeout(&addr, Duration::from_millis(300)).is_ok()
}

/// Escape text for a single-quoted JS string literal.
fn js_str(text: &str) -> String {
    let mut out = String::with_capacity(text.len() + 2);
    out.push('\'');
    for c in text.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\'' => out.push_str("\\'"),
            '\n' => out.push_str("\\n"),
            '\r' => {}
            _ => out.push(c),
        }
    }
    out.push('\'');
    out
}

fn show_error(window: &WebviewWindow, title: &str, detail: &str) {
    let script = format!("showError({}, {})", js_str(title), js_str(detail));
    let _ = window.eval(script);
}

/// Wait for the server, then load it into the window; or explain what went wrong.
fn wait_then_load(window: WebviewWindow, port: u16) {
    let started = Instant::now();
    let log = log_path();
    loop {
        if server_answers(port) {
            let url: Url = format!("http://127.0.0.1:{port}/")
                .parse()
                .expect("a localhost URL always parses");
            if let Err(e) = window.navigate(url) {
                show_error(&window, "Couldn't open Overtone", &e.to_string());
            }
            return;
        }

        let exited = {
            let state = window.state::<Server>();
            let mut guard = state.0.lock().unwrap_or_else(|p| p.into_inner());
            match guard.as_mut().map(Child::try_wait) {
                Some(Ok(Some(status))) => Some(status.to_string()),
                _ => None,
            }
        };
        if let Some(status) = exited {
            show_error(
                &window,
                "Overtone's server stopped while starting",
                &format!(
                    "It {status}. The reason is at the end of <code>{}</code>.",
                    log.display()
                ),
            );
            return;
        }
        if started.elapsed() > START_TIMEOUT {
            show_error(
                &window,
                "Overtone's server didn't start",
                &format!(
                    "It didn't answer within a minute. See <code>{}</code>.",
                    log.display()
                ),
            );
            return;
        }
        thread::sleep(Duration::from_millis(250));
    }
}

/// Open a link in the person's browser (Ledgr links, anything off-app).
fn open_in_browser(url: &Url) {
    let _ = Command::new("/usr/bin/open").arg(url.as_str()).spawn();
}

/// Put a download in ~/Downloads without replacing a file already there.
fn download_destination(suggested: &Path, url: &Url) -> PathBuf {
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_default();
    let downloads = home.join("Downloads");
    let name = suggested
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .filter(|n| !n.is_empty())
        .or_else(|| {
            url.path_segments()
                .and_then(|mut s| s.next_back().map(str::to_owned))
                .filter(|n| !n.is_empty())
        })
        .unwrap_or_else(|| "download".to_owned());

    let first = downloads.join(&name);
    if !first.exists() {
        return first;
    }
    let (stem, ext) = match name.rsplit_once('.') {
        Some((s, e)) if !s.is_empty() => (s.to_owned(), format!(".{e}")),
        _ => (name.clone(), String::new()),
    };
    (2..)
        .map(|n| downloads.join(format!("{stem} ({n}){ext}")))
        .find(|p| !p.exists())
        .expect("some numbered name is free")
}

fn build_window(app: &tauri::App, port: u16) -> tauri::Result<WebviewWindow> {
    let server_origin = format!("http://127.0.0.1:{port}");
    let nav_origin = server_origin.clone();

    WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
        .title("Overtone")
        .inner_size(1280.0, 860.0)
        .min_inner_size(720.0, 500.0)
        // Let the page's own drop zone receive dragged audio files; Tauri's
        // handler would otherwise swallow the drop.
        .disable_drag_drop_handler()
        .on_navigation(move |url| {
            let inside = url.scheme() == "tauri"
                || url.as_str().starts_with(&nav_origin)
                || url.scheme() == "about"
                || url.scheme() == "blob"
                || url.scheme() == "data";
            if !inside {
                open_in_browser(url);
            }
            inside
        })
        // "Open in Ledgr" uses target=_blank: send it to the browser rather
        // than spawning a bare second window.
        .on_new_window(move |url, _features| {
            if url.as_str().starts_with(&server_origin) {
                NewWindowResponse::Allow
            } else {
                open_in_browser(&url);
                NewWindowResponse::Deny
            }
        })
        .on_download(|_webview, event| {
            match event {
                DownloadEvent::Requested { url, destination } => {
                    *destination = download_destination(destination, &url);
                }
                DownloadEvent::Finished {
                    path: Some(path),
                    success: true,
                    ..
                } => {
                    // Show it in Finder, so the click visibly did something.
                    let _ = Command::new("/usr/bin/open").arg("-R").arg(path).spawn();
                }
                _ => {}
            }
            true
        })
        .build()
}

fn main() {
    let app = tauri::Builder::default()
        .manage(Server(Mutex::new(None)))
        .setup(|app| {
            let port = free_port()?;
            let window = build_window(app, port)?;

            match start_server(&repo_dir(), port) {
                Ok(child) => {
                    *app.state::<Server>()
                        .0
                        .lock()
                        .unwrap_or_else(|p| p.into_inner()) = Some(child);
                    thread::spawn(move || wait_then_load(window, port));
                }
                Err(message) => {
                    // Let the loading page finish before replacing its text.
                    thread::spawn(move || {
                        thread::sleep(Duration::from_millis(400));
                        show_error(&window, "Overtone couldn't start", &message);
                    });
                }
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("Overtone failed to start");

    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            let state = handle.state::<Server>();
            let mut guard = state.0.lock().unwrap_or_else(|p| p.into_inner());
            if let Some(mut child) = guard.take() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    });
}
