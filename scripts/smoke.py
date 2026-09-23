"""Launch the real server on loopback and check health; use temporary data."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener


def main():
    root = Path(__file__).resolve().parents[1]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory() as directory:
        env = dict(os.environ, HACKALEM_DATA_DIR=directory,
                   HACKALEM_DATABASE_PATH=str(Path(directory) / "smoke.sqlite3"))
        # A Windows venv launcher can spawn another Python process. Ask uvicorn
        # to exit gracefully over stdin instead of terminating just the launcher.
        server_code = (
            "import sys,threading,uvicorn; "
            "server=uvicorn.Server(uvicorn.Config('app.main:app',host='127.0.0.1',"
            "port=int(sys.argv[1]),access_log=False)); "
            "threading.Thread(target=lambda:(sys.stdin.read(1),setattr(server,'should_exit',True)),"
            "daemon=True).start(); server.run()"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", server_code, str(port)], cwd=root, env=env,
            stdin=subprocess.PIPE,
        )
        try:
            opener = build_opener(ProxyHandler({}))
            for _ in range(50):
                if process.poll() is not None:
                    raise RuntimeError("Server exited before health check")
                try:
                    with opener.open(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                        assert response.status == 200
                        assert json.load(response) == {"status": "ok"}
                    print("PASS: live HTTP GET /health returned 200 and status=ok")
                    return
                except URLError:
                    time.sleep(0.1)
            raise RuntimeError("Server did not become ready")
        finally:
            try:
                process.communicate(input=b"\n", timeout=10)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   check=False, stdout=subprocess.DEVNULL)
                else:
                    process.kill()
                process.wait()


if __name__ == "__main__":
    main()
