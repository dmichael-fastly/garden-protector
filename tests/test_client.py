import pytest
import responses
import requests
import time
from unittest.mock import MagicMock, patch
from hardware.client import GardenProtectorClient

@pytest.fixture
def client():
    # Instantiate client pointing to our mocked backend URL
    c = GardenProtectorClient(backend_url="http://mock-backend.local")
    # Quick default configurations for fast testing
    c.poll_interval = 0.1
    c.max_mitigation_seconds = 0.5  # set small watchdog limit for overrun tests
    return c

@pytest.fixture(autouse=True)
def mock_sleep():
    """Mock time.sleep globally to speed up tests."""
    with patch("time.sleep", return_value=None) as m:
        yield m

@responses.activate
def test_successful_mitigation_flow(client):
    """Verify that a successful trigger starts mitigation and completes normally."""
    # 1. Mock the /api/evidence POST response to return action: mitigate
    responses.add(
        responses.POST,
        "http://mock-backend.local/api/evidence",
        json={"action": "mitigate"},
        status=200
    )
    
    # 2. Mock two successful /api/status GET heartbeats, then a stop heartbeat
    responses.add(
        responses.GET,
        "http://mock-backend.local/api/status",
        json={"continue_mitigation": True},
        status=200
    )
    responses.add(
        responses.GET,
        "http://mock-backend.local/api/status",
        json={"continue_mitigation": False},
        status=200
    )
    
    # Track sprinkler and strobe active states
    assert not client.sprinkler.is_active
    assert not client.strobe.is_active
    
    # Trigger mitigation (this will run the event loop and exit when continue_mitigation is False)
    client.trigger_mitigation()
    
    # Verify both devices were disarmed
    assert not client.sprinkler.is_active
    assert not client.strobe.is_active
    assert client.state == "IDLE"

@responses.activate
def test_fail_closed_on_http_error(client):
    """Verify that if the /api/status heartbeat returns an HTTP error, we disarm immediately."""
    responses.add(
        responses.POST,
        "http://mock-backend.local/api/evidence",
        json={"action": "mitigate"},
        status=200
    )
    
    # Heatbeat returns 500 Internal Server Error
    responses.add(
        responses.GET,
        "http://mock-backend.local/api/status",
        json={"error": "Database error"},
        status=500
    )
    
    # Run loop
    client.trigger_mitigation()
    
    # Should disarm and enter cooldown
    assert not client.sprinkler.is_active
    assert not client.strobe.is_active
    assert client.state == "IDLE"

@responses.activate
def test_fail_closed_on_network_timeout(client):
    """Verify that if the heartbeat endpoint times out, we disarm immediately."""
    responses.add(
        responses.POST,
        "http://mock-backend.local/api/evidence",
        json={"action": "mitigate"},
        status=200
    )
    
    # Heartbeat times out/raises ConnectionError
    responses.add(
        responses.GET,
        "http://mock-backend.local/api/status",
        body=requests.exceptions.Timeout("Connection timed out")
    )
    
    client.trigger_mitigation()
    
    assert not client.sprinkler.is_active
    assert not client.strobe.is_active
    assert client.state == "IDLE"

@responses.activate
def test_fail_closed_on_evidence_503(client):
    """TEST-001: the edge's own fail-closed (e.g. model-not-loaded -> 503) on the
    evidence POST itself must disarm the Pi. The existing fail-closed tests only fail
    the *heartbeat* after a 200/mitigate; this exercises the `status_code != 200`
    branch of trigger_mitigation (client.py evidence-POST disarm path)."""
    responses.add(
        responses.POST,
        "http://mock-backend.local/api/evidence",
        json={"error": "model not loaded"},
        status=503,
    )
    # No heartbeat is mocked: if the client wrongly armed, start_mitigation's status
    # poll would 500 in responses and the test would still catch it — but the contract
    # is that a non-200 evidence reply never reaches mitigation at all.

    assert not client.sprinkler.is_active
    client.trigger_mitigation()

    # Fail-closed: deterrents off, back to a settled IDLE (via COOLDOWN), and the
    # mitigation loop was never entered (no /api/status heartbeat was sent).
    assert not client.sprinkler.is_active
    assert not client.strobe.is_active
    assert client.state == "IDLE"
    assert not any(c.request.url.endswith("/api/status") for c in responses.calls)


@responses.activate
def test_fail_closed_on_evidence_post_timeout(client):
    """TEST-001: a Timeout/connection failure on the evidence POST (not the heartbeat)
    must disarm — the `requests.exceptions.RequestException` branch."""
    responses.add(
        responses.POST,
        "http://mock-backend.local/api/evidence",
        body=requests.exceptions.Timeout("evidence upload timed out"),
    )

    client.trigger_mitigation()

    assert not client.sprinkler.is_active
    assert not client.strobe.is_active
    assert client.state == "IDLE"
    assert not any(c.request.url.endswith("/api/status") for c in responses.calls)


@responses.activate
def test_fail_closed_on_evidence_malformed_json(client):
    """TEST-001: a 200 with a non-JSON body must NOT leave the device armed/unknown —
    the generic `except Exception` defensive branch (response.json() raises) disarms."""
    responses.add(
        responses.POST,
        "http://mock-backend.local/api/evidence",
        body="<html>not json — proxy error page</html>",
        status=200,
        content_type="text/html",
    )

    client.trigger_mitigation()

    assert not client.sprinkler.is_active
    assert not client.strobe.is_active
    assert client.state == "IDLE"
    assert not any(c.request.url.endswith("/api/status") for c in responses.calls)


@responses.activate
def test_trace_id_and_identity_injected_into_both_requests(client):
    """Phase 0: every backend call carries one 16-hex X-Garden-Trace-Id (equal on
    the evidence POST and that event's heartbeats) plus the forward-compat identity
    headers, defaulting to 'default'."""
    responses.add(
        responses.POST,
        "http://mock-backend.local/api/evidence",
        json={"action": "mitigate"},
        status=200,
    )
    # One continue heartbeat, then a stop heartbeat to end the loop.
    responses.add(
        responses.GET,
        "http://mock-backend.local/api/status",
        json={"continue_mitigation": True},
        status=200,
    )
    responses.add(
        responses.GET,
        "http://mock-backend.local/api/status",
        json={"continue_mitigation": False},
        status=200,
    )

    client.trigger_mitigation()

    # Collect calls by URL (don't rely on ordering/index).
    posts = [c for c in responses.calls if c.request.url.endswith("/api/evidence")]
    gets = [c for c in responses.calls if c.request.url.endswith("/api/status")]
    assert posts, "expected an evidence POST"
    assert gets, "expected at least one status heartbeat"

    post_trace = posts[0].request.headers.get("X-Garden-Trace-Id")
    assert post_trace and len(post_trace) == 16, f"expected 16-hex trace id, got {post_trace!r}"
    int(post_trace, 16)  # raises if not hex

    # The POST keeps its content type alongside the injected headers.
    assert posts[0].request.headers.get("Content-Type") == "image/jpeg"

    # Every heartbeat carries the SAME trace id as the POST.
    for g in gets:
        assert g.request.headers.get("X-Garden-Trace-Id") == post_trace

    # Forward-compat identity present on both call types, defaulting to "default".
    for c in posts + gets:
        assert c.request.headers.get("X-Garden-Id") == "default"
        assert c.request.headers.get("X-Device-Id") == "default"
        assert c.request.headers.get("X-Node-Id") == "default"


@responses.activate
def test_identity_headers_use_configured_ids():
    """Non-default identity flows through to the request headers."""
    c = GardenProtectorClient(
        backend_url="http://mock-backend.local",
        garden_id="backyard", device_id="pi-01", node_id="node-a",
    )
    c.poll_interval = 0.1
    c.max_mitigation_seconds = 0.5
    responses.add(responses.POST, "http://mock-backend.local/api/evidence",
                  json={"action": "none"}, status=200)

    c.trigger_mitigation()

    post = next(call for call in responses.calls if call.request.url.endswith("/api/evidence"))
    assert post.request.headers.get("X-Garden-Id") == "backyard"
    assert post.request.headers.get("X-Device-Id") == "pi-01"
    assert post.request.headers.get("X-Node-Id") == "node-a"


@responses.activate
def test_garden_token_sent_only_when_configured():
    """Forward-compat auth (Step 2 plumbing): a configured garden token rides on
    every backend call as X-Garden-Auth; a tokenless (default) garden sends NO
    auth header at all (don't transmit an empty credential)."""
    responses.add(responses.POST, "http://mock-backend.local/api/evidence",
                  json={"action": "mitigate"}, status=200)
    responses.add(responses.GET, "http://mock-backend.local/api/status",
                  json={"continue_mitigation": False}, status=200)

    # Token configured -> header present (== token) on POST and GET.
    c = GardenProtectorClient(backend_url="http://mock-backend.local",
                              garden_id="backyard", garden_token="s3cr3t-token")
    c.poll_interval = 0.1
    c.max_mitigation_seconds = 0.5
    c.trigger_mitigation()

    calls = [call for call in responses.calls
             if call.request.url.endswith(("/api/evidence", "/api/status"))]
    assert calls, "expected at least one backend call"
    for call in calls:
        assert call.request.headers.get("X-Garden-Auth") == "s3cr3t-token"

    # Tokenless default garden -> no X-Garden-Auth header is sent.
    responses.calls.reset()
    d = GardenProtectorClient(backend_url="http://mock-backend.local")
    d.poll_interval = 0.1
    d.max_mitigation_seconds = 0.5
    d.trigger_mitigation()

    tokenless = [call for call in responses.calls
                 if call.request.url.endswith(("/api/evidence", "/api/status"))]
    assert tokenless, "expected at least one backend call"
    for call in tokenless:
        assert call.request.headers.get("X-Garden-Auth") is None


@responses.activate
def test_local_watchdog_overrun(client):
    """Verify that the local 60-second limit forces disarming even if the backend says continue."""
    responses.add(
        responses.POST,
        "http://mock-backend.local/api/evidence",
        json={"action": "mitigate"},
        status=200
    )
    
    # Heartbeat always says continue_mitigation = True
    responses.add(
        responses.GET,
        "http://mock-backend.local/api/status",
        json={"continue_mitigation": True},
        status=200
    )
    
    # Set the watchdog limit very low (0.2s)
    client.max_mitigation_seconds = 0.2
    client.poll_interval = 0.05
    
    start = time.time()
    client.trigger_mitigation()
    duration = time.time() - start

    # Verify that we disarmed and exited the mitigation loop quickly
    assert duration < 1.0
    assert not client.sprinkler.is_active
    assert not client.strobe.is_active
    assert client.state == "IDLE"


def test_set_state_emits_fsm_transitions(tmp_path):
    """A full mitigate->stop flow records every FSM transition in the telemetry DB
    (proves _set_state instrumentation fires for the real state machine)."""
    import sqlite3
    import hardware.telemetry as telemetry

    db = str(tmp_path / "telemetry.db")
    telemetry.init(db_path=db, garden_id="g1", device_id="d1", node_id="n1")
    try:
        c = GardenProtectorClient(backend_url="http://mock-backend.local")
        c.poll_interval = 0.1
        c.max_mitigation_seconds = 0.5
        with responses.RequestsMock() as rsps:
            rsps.add(responses.POST, "http://mock-backend.local/api/evidence",
                     json={"action": "mitigate"}, status=200)
            rsps.add(responses.GET, "http://mock-backend.local/api/status",
                     json={"continue_mitigation": False}, status=200)
            c.trigger_mitigation()
    finally:
        telemetry.shutdown()

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        transitions = [
            r[0] for r in conn.execute(
                "SELECT args FROM events WHERE component='fsm' AND op='transition' ORDER BY ts"
            ).fetchall()
        ]
    finally:
        conn.close()

    # The IDLE->TRIGGERED->MITIGATING->COOLDOWN->IDLE lifecycle is fully recorded.
    for state in ("TRIGGERED", "MITIGATING", "COOLDOWN", "IDLE"):
        assert any(f'"to": "{state}"' in t for t in transitions), f"missing transition to {state}"
