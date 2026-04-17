"""One-time browser geolocation flow for Weather agent.

Usage: (run from root)
    python -m auth.weather

Opens a local browser page that requests GPS location via the browser's
Geolocation API. On approval, reverse-geocodes once via Nominatim and saves:
    ~/.config/radio-podcast/weather_location.json

Mirrors the pattern of auth/calendar_auth.py (OAuth via local server).
"""

from __future__ import annotations

import json
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

TOKEN_DIR = Path.home() / ".config" / "radio-podcast"
LOCATION_PATH = TOKEN_DIR / "weather_location.json"

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"

_HTML = """\
<!DOCTYPE html>
<html>
<head>
  <title>Weather — Allow Location</title>
  <style>
    body { font-family: sans-serif; max-width: 480px; margin: 80px auto; text-align: center; }
    button { padding: 12px 24px; font-size: 16px; cursor: pointer; }
    #status { margin-top: 20px; color: #555; }
  </style>
</head>
<body>
  <h2>Weather Agent — Location Access</h2>
  <p>Click below and approve the browser location prompt.</p>
  <button onclick="getLocation()">Share My Location</button>
  <p id="status"></p>
  <script>
    function getLocation() {
      document.getElementById('status').textContent = 'Requesting location...';
      navigator.geolocation.getCurrentPosition(
        function(pos) {
          document.getElementById('status').textContent = 'Got it — saving location...';
          fetch('/callback?lat=' + pos.coords.latitude + '&lon=' + pos.coords.longitude)
            .then(r => r.text())
            .then(msg => { document.getElementById('status').textContent = msg; });
        },
        function(err) {
          document.getElementById('status').textContent = 'Error: ' + err.message;
        }
      );
    }
  </script>
</body>
</html>"""


def _reverse_geocode(lat: float, lon: float) -> str:
    """One-time reverse geocode via Nominatim. Returns city/state or coordinate string."""
    try:
        resp = httpx.get(
            _NOMINATIM_URL,
            params={"lat": lat, "lon": lon, "format": "json"},
            headers={"User-Agent": "radio-podcast/1.0 (auth/weather.py)"},
            timeout=5.0,
        )
        resp.raise_for_status()
        addr = resp.json().get("address", {})
        city = (
            addr.get("city")
            or addr.get("town")
            or addr.get("village")
            or addr.get("county", "")
        )
        state = addr.get("state", "")
        parts = [p for p in [city, state] if p]
        return ", ".join(parts) if parts else f"{lat:.4f}, {lon:.4f}"
    except Exception:
        return f"{lat:.4f}, {lon:.4f}"


class _Handler(BaseHTTPRequestHandler):
    result: dict | None = None
    _stop = threading.Event()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/":
            body = _HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif parsed.path == "/callback":
            params = parse_qs(parsed.query)
            try:
                lat = float(params["lat"][0])
                lon = float(params["lon"][0])
            except (KeyError, ValueError):
                self._respond(400, "Bad callback parameters.")
                return

            location_name = _reverse_geocode(lat, lon)
            _Handler.result = {"lat": lat, "lon": lon, "location_name": location_name}

            msg = f"Location saved: {location_name}. You can close this tab."
            self._respond(200, msg)
            _Handler._stop.set()

        else:
            self._respond(404, "Not found.")

    def _respond(self, code: int, text: str) -> None:
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


def main() -> None:
    _Handler.result = None
    _Handler._stop.clear()

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print(f"Opening browser: {url}")
    webbrowser.open(url)

    _Handler._stop.wait(timeout=120)
    server.shutdown()

    result = _Handler.result
    if not result:
        print("Location not captured (timeout or denied).")
        return

    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    LOCATION_PATH.write_text(
        json.dumps(
            {
                "lat": result["lat"],
                "lon": result["lon"],
                "location_name": result["location_name"],
                "acquired_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        )
    )

    print(
        f"Saved: {result['location_name']} ({result['lat']:.4f}, {result['lon']:.4f})"
    )
    print(f"  → {LOCATION_PATH}")


if __name__ == "__main__":
    main()
