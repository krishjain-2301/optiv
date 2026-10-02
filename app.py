"""PII Shield dashboard.   python app.py [--port 8000] [--no-browser]

Starts the local server (server/api.py: a JSON API over the pipeline plus the built React
dashboard in web/dist) on 127.0.0.1 and opens it in the browser. Runs entirely on this machine;
nothing is sent to any external service.
"""
from __future__ import annotations

import argparse
import threading
import webbrowser

import uvicorn


def main() -> None:
    ap = argparse.ArgumentParser(description="PII Shield dashboard")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true", help="do not open the browser")
    a = ap.parse_args()
    if not a.no_browser:
        threading.Timer(1.5, webbrowser.open, [f"http://127.0.0.1:{a.port}"]).start()
    uvicorn.run("server.api:app", host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
