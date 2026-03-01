"""
Dashboard backend server - proxies the Hackapizza game API for the frontend dashboard.
Run with: python dashboard_server.py
Starts a local server on http://localhost:5050
"""

import os
import json
import asyncio
import aiohttp
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import threading

from dotenv import load_dotenv
load_dotenv()

TEAM_ID = os.getenv("TEAM_ID", "16")
TEAM_API_KEY = os.getenv("SERVER_API_KEY", os.getenv("TEAM_API_KEY", ""))
BASE_URL = os.getenv("BASE_URL", "https://hackapizza.datapizza.tech")
PORT = 5050


async def _fetch(endpoint: str, params: dict = None) -> any:
    url = f"{BASE_URL}{endpoint}"
    headers = {"x-api-key": TEAM_API_KEY}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, params=params) as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                return {"error": resp.status, "message": await resp.text()}


def run_async(coro):
    """Run an async coroutine in a new event loop (for threading)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress default logging

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: str, content_type: str):
        try:
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        # Serve dashboard HTML
        if path == "/" or path == "/dashboard":
            self.send_file(
                os.path.join(os.path.dirname(__file__), "dashboard.html"),
                "text/html; charset=utf-8",
            )
            return

        # ── API proxy routes ──────────────────────────────────────────────────
        if path == "/api/restaurants":
            data = run_async(_fetch("/restaurants"))
            self.send_json(data)

        elif path == "/api/my_restaurant":
            data = run_async(_fetch(f"/restaurant/{TEAM_ID}"))
            self.send_json(data)

        elif path == "/api/my_menu":
            data = run_async(_fetch(f"/restaurant/{TEAM_ID}/menu"))
            self.send_json(data)

        elif path == "/api/recipes":
            data = run_async(_fetch("/recipes"))
            self.send_json(data)

        elif path == "/api/market":
            data = run_async(_fetch("/market/entries"))
            self.send_json(data)

        elif path == "/api/meals":
            turn_id = qs.get("turn_id", [None])[0]
            restaurant_id = qs.get("restaurant_id", [TEAM_ID])[0]
            if turn_id:
                data = run_async(_fetch("/meals", {"turn_id": turn_id, "restaurant_id": restaurant_id}))
            else:
                data = {"error": "turn_id required"}
            self.send_json(data)

        elif path == "/api/bid_history":
            turn_id = qs.get("turn_id", [None])[0]
            if turn_id:
                data = run_async(_fetch("/bid_history", {"turn_id": turn_id}))
            else:
                data = {"error": "turn_id required"}
            self.send_json(data)

        elif path == "/api/config":
            self.send_json({"team_id": TEAM_ID, "base_url": BASE_URL})

        elif path == "/api/game_memory":
            memory_path = os.path.join(os.path.dirname(__file__), "game_memory.json")
            try:
                with open(memory_path, "r") as f:
                    data = json.load(f)
                self.send_json(data)
            except Exception as e:
                self.send_json({"error": str(e)})

        elif path == "/api/sse":
            self._proxy_sse()

        else:
            self.send_response(404)
            self.end_headers()

    def _proxy_sse(self):
        """Proxy the game server SSE stream to the browser."""
        import urllib.request
        url = f"{BASE_URL}/events/{TEAM_ID}"
        req = urllib.request.Request(url, headers={
            "Accept": "text/event-stream",
            "x-api-key": TEAM_API_KEY,
        })
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", errors="ignore").strip()
                    if not line:
                        self.wfile.write(b"\n")
                        self.wfile.flush()
                        continue
                    if line.startswith("data:"):
                        payload = line[5:].strip()
                        if payload == "connected":
                            self.wfile.write(b"data: {\"type\":\"connected\"}\n\n")
                        else:
                            self.wfile.write(f"data: {payload}\n\n".encode())
                        self.wfile.flush()
        except Exception:
            pass  # Client disconnected


def main():
    server = HTTPServer(("localhost", PORT), DashboardHandler)
    print(f"🍕 Hackapizza Dashboard running at http://localhost:{PORT}")
    print(f"   Team ID: {TEAM_ID}")
    print(f"   Game server: {BASE_URL}")
    print("   Press Ctrl+C to stop\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


if __name__ == "__main__":
    main()
