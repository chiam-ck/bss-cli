//! Docker healthcheck self-probe.
//!
//! The Rust images are distroless — no shell, no curl — so each service/portal
//! binary answers its own container `HEALTHCHECK` via a `--healthcheck` flag: it
//! opens a TCP connection to the local HTTP port, sends `GET /health`, and exits 0
//! iff the response status is `200`. This reproduces the Python images'
//! `HEALTHCHECK CMD curl -f http://localhost:8000/health || exit 1` without a shell
//! or an HTTP client dependency (raw `std::net` only).

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::time::Duration;

/// `true` iff `--healthcheck` is on the command line.
pub fn healthcheck_requested() -> bool {
    std::env::args().any(|a| a == "--healthcheck")
}

/// If `--healthcheck` was passed, probe `GET /health` on `127.0.0.1:<port>` and
/// terminate the process — exit `0` on HTTP 200, exit `1` otherwise (connection
/// refused, timeout, or any non-200). Never returns in that case; a no-op when the
/// flag is absent. Call it as the FIRST line of `main`, before any telemetry / DB /
/// adapter bootstrap, so the probe stays cheap and side-effect free.
pub fn maybe_run_healthcheck(port: u16) {
    if !healthcheck_requested() {
        return;
    }
    std::process::exit(match probe(port) {
        Ok(true) => 0,
        _ => 1,
    });
}

/// Open `127.0.0.1:<port>`, send a minimal HTTP/1.0 `GET /health`, and return
/// `Ok(true)` iff the status line reports `200`. `Connection: close` + HTTP/1.0 so
/// the server closes the socket after responding.
fn probe(port: u16) -> std::io::Result<bool> {
    let timeout = Duration::from_secs(3);
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream = TcpStream::connect_timeout(&addr, timeout)?;
    stream.set_read_timeout(Some(timeout))?;
    stream.set_write_timeout(Some(timeout))?;
    stream.write_all(b"GET /health HTTP/1.0\r\nHost: localhost\r\nConnection: close\r\n\r\n")?;
    let mut buf = [0u8; 128];
    let n = stream.read(&mut buf)?;
    // Status line looks like "HTTP/1.1 200 OK" — the second whitespace-delimited
    // token is the status code.
    let head = String::from_utf8_lossy(&buf[..n]);
    Ok(head.split_whitespace().nth(1) == Some("200"))
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::probe;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::thread;

    /// Serve exactly one request with the given status line, then close.
    fn serve_once(status_line: &'static str) -> u16 {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        thread::spawn(move || {
            if let Ok((mut sock, _)) = listener.accept() {
                let mut scratch = [0u8; 256];
                let _ = sock.read(&mut scratch);
                let body = r#"{"status":"ok"}"#;
                let resp = format!(
                    "{status_line}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                let _ = sock.write_all(resp.as_bytes());
            }
        });
        port
    }

    #[test]
    fn probe_true_on_200() {
        let port = serve_once("HTTP/1.1 200 OK");
        assert!(probe(port).unwrap());
    }

    #[test]
    fn probe_false_on_500() {
        let port = serve_once("HTTP/1.1 500 Internal Server Error");
        assert!(!probe(port).unwrap());
    }

    #[test]
    fn probe_errs_when_nothing_listening() {
        // Port 1 is privileged and unbound in the test env → connection refused.
        assert!(probe(1).is_err());
    }
}
