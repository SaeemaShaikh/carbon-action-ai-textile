"""Start Carbon Action AI:  python run.py   ->  http://localhost:8000"""
import os
import sys
import threading
import time
import urllib.request
import webbrowser

import uvicorn

HERE = os.path.dirname(os.path.abspath(__file__))


def open_when_ready(url: str, timeout_s: int = 180):
    """Open the browser once the server answers (model training takes ~30 s)."""
    end = time.time() + timeout_s
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=2)
            webbrowser.open(url)
            return
        except OSError:
            time.sleep(1)


if __name__ == "__main__":
    os.chdir(HERE)  # works no matter which folder it is started from
    sys.path.insert(0, HERE)
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "127.0.0.1")  # set HOST=0.0.0.0 inside Docker
    if os.getenv("NO_BROWSER") != "1":
        threading.Thread(target=open_when_ready, args=(f"http://localhost:{port}",), daemon=True).start()
    uvicorn.run("app.main:app", host=host, port=port, log_level="warning")
