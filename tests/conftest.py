import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from browsertasklab.config import Settings
from browsertasklab.store import Store


@pytest.fixture(scope="session")
def local_app(tmp_path_factory):
    root = tmp_path_factory.mktemp("local-app")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = os.environ.copy() | {"BTL_RUNTIME_DIR": str(root), "BTL_BASE_URL": f"http://127.0.0.1:{port}"}
    process = subprocess.Popen([sys.executable, "-m", "browsertasklab", "serve", "--port", str(port)], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(300):
            if process.poll() is not None:
                raise RuntimeError(process.stderr.read().decode(errors="replace"))
            try:
                with urllib.request.urlopen(url + "/api/health", timeout=.3) as response:
                    if response.status == 200:
                        break
            except (OSError, TimeoutError):
                time.sleep(.1)
        else:
            raise RuntimeError("Local test server did not start")
        settings = Settings(base_url=url, runtime_dir=root, action_timeout_ms=800, max_retries=2)
        yield settings, Store(root / "lab.sqlite3")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stderr.close()
