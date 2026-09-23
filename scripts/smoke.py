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
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", str(port), "--no-access-log"], cwd=root, env=env,
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
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    main()
