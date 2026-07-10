#!/usr/bin/env python3
"""provision/edge_client.py — ONE admin-side edge proxy client (CHARTER: one Python
service library).

The Pi portal (``hardware/portal.py``) and the admin console (``provision/console.py``)
both proxy dashboard reads/controls to the Fastly Compute edge. Before this they each
had their own copy — the portal on ``requests``, the console on ``urllib`` — with the
per-garden identity/auth headers built in two places. This class is the single
``requests``-based client; it injects the identity + trace headers
(``X-Garden-Id`` / ``X-Device-Id`` / ``X-Node-Id`` / ``X-Garden-Auth`` /
``X-Garden-Trace-Id``) in one place.

Instance defaults are merged with per-call overrides, so:
  * the portal constructs it bound to ITS garden/device/node/token and calls bare;
  * the console constructs it base-only and passes ``token=`` (and the garden via the
    request PATH) per garden.

A header is emitted only when its value is truthy, so a single-garden/tokenless deploy
sends nothing special and the edge mints a trace id when none is supplied — i.e. the
default behavior is unchanged from the prior two implementations.

NOTE — this is the ADMIN/dashboard read-proxy client. The gateway's SAFETY-PATH edge
client (``hardware/gateway.py:EdgeClient`` — ``post_evidence`` / ``post_telemetry`` /
``post_alert``, fail-closed, on the spray loop) is deliberately SEPARATE and untouched;
that path is sacred (CHARTER "safety contract"). Do not merge them.
"""
import json

import requests


class EdgeClient:
    """Admin/dashboard read-proxy to the edge over ``requests``."""

    def __init__(self, base_url, *, timeout=3.0, garden_id=None, device_id=None,
                 node_id=None, token=None):
        self.base = (base_url or "").rstrip("/")
        self.timeout = timeout
        self.garden_id = garden_id
        self.device_id = device_id
        self.node_id = node_id
        self.token = token

    # -- header injection (one place) ---------------------------------------
    def headers(self, content_type=None, *, garden_id=None, device_id=None,
                node_id=None, token=None, trace_id=None):
        gid = self.garden_id if garden_id is None else garden_id
        did = self.device_id if device_id is None else device_id
        nid = self.node_id if node_id is None else node_id
        tok = self.token if token is None else token
        h = {}
        if gid:
            h["X-Garden-Id"] = gid
        if did:
            h["X-Device-Id"] = did
        if nid:
            h["X-Node-Id"] = nid
        if tok:
            h["X-Garden-Auth"] = tok
        if trace_id:
            h["X-Garden-Trace-Id"] = trace_id
        if content_type:
            h["Content-Type"] = content_type
        return h

    def _url(self, path):
        # Accept an absolute URL (the console already builds some) or a base-relative path.
        return path if path.startswith("http") else self.base + path

    # -- raw passthrough -> (status, content_type, content) -----------------
    # Used by the portal's dashboard/admin proxy; never raises on an HTTP status.
    def proxy_get(self, path, *, timeout=None, **idkw):
        r = requests.get(self._url(path), headers=self.headers(**idkw),
                         timeout=timeout or self.timeout)
        return r.status_code, r.headers.get("Content-Type", "application/json"), r.content

    def proxy_post(self, path, body, *, timeout=None, **idkw):
        r = requests.post(self._url(path), data=body,
                          headers=self.headers("application/json", **idkw),
                          timeout=timeout or self.timeout)
        return r.status_code, r.headers.get("Content-Type", "application/json"), r.content

    # -- JSON convenience (console) -> raises requests.HTTPError on non-2xx --
    def get_json(self, path, *, timeout=None, **idkw):
        r = requests.get(self._url(path), headers=self.headers(**idkw),
                         timeout=timeout or self.timeout)
        r.raise_for_status()
        return r.json() if r.content else {}

    def post_json(self, path, body, *, timeout=None, **idkw):
        r = requests.post(self._url(path), data=json.dumps(body).encode(),
                          headers=self.headers("application/json", **idkw),
                          timeout=timeout or self.timeout)
        r.raise_for_status()
        return r.json() if r.content else {}

    # -- raw bytes (console snapshot proxy) ---------------------------------
    # Returns the response even on a 4xx/5xx (e.g. 404 = no snapshot yet) so the
    # caller can pass the status through to the browser as a normal placeholder case.
    def get_bytes(self, path, *, timeout=None, **idkw):
        r = requests.get(self._url(path), headers=self.headers(**idkw),
                         timeout=timeout or self.timeout)
        return r.status_code, r.headers.get("Content-Type", "application/octet-stream"), r.content
