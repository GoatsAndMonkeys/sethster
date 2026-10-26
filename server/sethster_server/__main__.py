"""`python -m sethster_server` runs the API server (host/port from SETHSTER_HOST / SETHSTER_PORT).

Hot swap: the app is fully imported BEFORE the port is bound, and binding retries for up to
SETHSTER_BIND_WAIT seconds (default 600). So a replacement server can be started while the old
one still serves; it writes SETHSTER_READY_FILE (default data/server.ready) with its pid once it
is loaded and only waiting for the port; then stop the old one and the new one takes the port
within ~0.2 s. (server/restart.sh does exactly this.)
"""
import os
import socket
import sys
import time

import uvicorn


def main() -> None:
    host = os.environ.get("SETHSTER_HOST", "0.0.0.0")
    port = int(os.environ.get("SETHSTER_PORT", "8642"))
    from .config import load_settings
    from .main import app  # slow imports happen here, before the port is taken

    ready = os.environ.get("SETHSTER_READY_FILE") or str(load_settings().data_dir / "server.ready")
    try:
        with open(ready, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    deadline = time.time() + float(os.environ.get("SETHSTER_BIND_WAIT", "600"))
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    while True:
        try:
            sock.bind((host, port))
            break
        except OSError:
            if time.time() > deadline:
                print(f"sethster: port {port} still busy after wait; giving up", file=sys.stderr)
                raise
            time.sleep(0.1)
    sock.listen(2048)
    sock.set_inheritable(True)
    # proxy_headers off: the app sees the real socket peer and accounts_api.client_ip alone reads
    # X-Forwarded-For, and only from trusted proxies (docs/plans/accounts.md [R2-1]).
    config = uvicorn.Config(app, host=host, port=port, proxy_headers=False,
                            log_level=os.environ.get("SETHSTER_LOG_LEVEL", "info"))
    from .app import install_log_redaction
    install_log_redaction()  # again after uvicorn's logging config, which must not drop the token filter
    uvicorn.Server(config).run(sockets=[sock])


if __name__ == "__main__":
    main()
