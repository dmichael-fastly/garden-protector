"""Integration tests for the console's camera-gallery proxy routes.

The gallery is browser-facing, so it must NOT hold the per-garden token; the console
proxies the edge's per-device snapshot/event/telemetry routes and attaches the token
SERVER-SIDE. These tests stand up a fake edge + the real console handler and assert:

  * /api/console/snapshot attaches X-Garden-Auth (from the deploy-env file) and passes
    the JPEG bytes through, with a 404 (no snapshot yet) passed through too;
  * /api/console/device merges the per-device event + telemetry; and
  * the tokenless `default` garden sends NO auth header.

No passcode file is written to the tmp configs dir, so auth is disabled (open) and the
loopback client clears the LAN guard — exactly the localhost-dev posture.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from provision import console

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF--fake-cam-1-bytes--\xff\xd9"


class _FakeEdge(BaseHTTPRequestHandler):
    """Records (path, X-Garden-Auth) for each GET and serves device sub-resources."""
    log = []

    def log_message(self, *a):
        pass

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        _FakeEdge.log.append((self.path, self.headers.get("X-Garden-Auth")))
        if self.path.endswith("/devices/cam-1/snapshot"):
            self._send(200, "image/jpeg", JPEG)
        elif self.path.endswith("/devices/missing/snapshot"):
            self._send(404, "text/plain", b"No snapshot available")
        elif self.path.endswith("/devices/cam-1/event"):
            self._send(200, "application/json", json.dumps(
                {"species": "red fox", "confidence": 0.94, "action": "mitigate",
                 "reason": None, "ts": 1782012034000}).encode())
        elif self.path.endswith("/devices/cam-1/telemetry"):
            self._send(200, "application/json", json.dumps(
                {"soil_moisture_pct": 42, "last_seen_ms": 1782012090000}).encode())
        else:
            self._send(404, "text/plain", b"nope")


@pytest.fixture
def servers(tmp_path):
    _FakeEdge.log = []
    edge = ThreadingHTTPServer(("127.0.0.1", 0), _FakeEdge)
    # poll_interval=0.02 (vs 0.5s default) keeps shutdown() in teardown prompt.
    threading.Thread(target=edge.serve_forever, args=(0.02,), daemon=True).start()
    edge_url = f"http://127.0.0.1:{edge.server_address[1]}"

    # Deploy-env so garden_token("backyard") resolves to the token (server-side only).
    (tmp_path / "backyard-cam-1.env").write_text("GP_GARDEN_TOKEN=tok-xyz\nGP_GARDEN_ID=backyard\n")

    cfg = console.ConsoleConfig(configs_dir=tmp_path, mock=True, edge=edge_url)
    con = ThreadingHTTPServer(("127.0.0.1", 0), console.make_handler(cfg))
    threading.Thread(target=con.serve_forever, args=(0.02,), daemon=True).start()
    con_url = f"http://127.0.0.1:{con.server_address[1]}"
    try:
        yield con_url
    finally:
        con.shutdown(); con.server_close()
        edge.shutdown(); edge.server_close()


def _auth_for(suffix):
    hits = [a for (p, a) in _FakeEdge.log if p.endswith(suffix)]
    assert hits, f"edge never received {suffix}"
    return hits[-1]


def test_snapshot_proxy_attaches_token_and_passes_bytes(servers):
    r = requests.get(servers + "/api/console/snapshot?garden=backyard&device=cam-1", timeout=5)
    assert r.status_code == 200
    assert r.headers["Content-Type"] == "image/jpeg"
    assert r.content == JPEG
    assert _auth_for("/devices/cam-1/snapshot") == "tok-xyz"  # token attached server-side


def test_snapshot_404_is_passed_through(servers):
    r = requests.get(servers + "/api/console/snapshot?garden=backyard&device=missing", timeout=5)
    assert r.status_code == 404  # "no snapshot yet" -> the gallery keeps its placeholder


def test_device_proxy_merges_event_and_telemetry(servers):
    r = requests.get(servers + "/api/console/device?garden=backyard&device=cam-1", timeout=5)
    assert r.status_code == 200
    j = r.json()
    assert j["event"]["species"] == "red fox" and j["event"]["action"] == "mitigate"
    assert j["telemetry"]["soil_moisture_pct"] == 42
    assert _auth_for("/devices/cam-1/event") == "tok-xyz"


def test_default_garden_sends_no_token(servers):
    r = requests.get(servers + "/api/console/snapshot?garden=default&device=cam-1", timeout=5)
    assert r.status_code == 200
    assert _auth_for("/devices/cam-1/snapshot") is None  # tokenless default garden


def test_snapshot_requires_garden_and_device(servers):
    r = requests.get(servers + "/api/console/snapshot?garden=backyard", timeout=5)
    assert r.status_code == 400
    r = requests.get(servers + "/api/console/device?device=cam-1", timeout=5)
    assert r.status_code == 400
