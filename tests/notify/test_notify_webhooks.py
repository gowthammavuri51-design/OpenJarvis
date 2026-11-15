"""Webhook tests: press-1 on Jarvis calls and "OK 12" SMS replies."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("fastapi", reason="openjarvis[server] not installed")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from openjarvis.notify import store as st  # noqa: E402
from tests.notify.test_notifier import OWNER, make  # noqa: E402


@pytest.fixture
def setup():
    notifier, gw, _ = make()
    bridge = MagicMock()
    bridge.handle_incoming.return_value = "agent reply"
    with patch(
        "openjarvis.server.webhook_routes._load_notifier", return_value=notifier
    ):
        from openjarvis.server.webhook_routes import create_webhook_router

        app = FastAPI()
        app.include_router(create_webhook_router(bridge=bridge, twilio_auth_token=""))
    return TestClient(app), notifier, gw, bridge


def _signed(valid: bool = True):
    return patch(
        "openjarvis.server.webhook_routes._validate_twilio_signature",
        return_value=valid,
    )


def test_voice_keypress_acks(setup) -> None:
    client, notifier, gw, _ = setup
    a = notifier.notify("Fire", urgency="critical").alert_id
    with _signed():
        resp = client.post(f"/webhooks/twilio/voice?alert_id={a}", data={"Digits": "1"})
    assert resp.status_code == 200
    assert "Acknowledged" in resp.text
    assert notifier.store.get(a).status == st.ACKED


def test_voice_rejects_bad_signature(setup) -> None:
    client, notifier, _, _ = setup
    a = notifier.notify("Fire", urgency="critical").alert_id
    with _signed(False):
        resp = client.post(f"/webhooks/twilio/voice?alert_id={a}", data={"Digits": "1"})
    assert resp.status_code == 403
    assert notifier.store.get(a).status == st.SENT


def test_voice_bad_alert_id(setup) -> None:
    client, *_ = setup
    with _signed():
        resp = client.post("/webhooks/twilio/voice?alert_id=abc", data={"Digits": "1"})
    assert "Hangup" in resp.text


def test_sms_ok_reply_acks_without_agent(setup) -> None:
    client, notifier, _, bridge = setup
    a = notifier.notify("Fire", urgency="critical").alert_id
    with _signed():
        resp = client.post("/webhooks/twilio", data={"From": OWNER, "Body": f"OK {a}"})
    assert resp.status_code == 200
    assert f"Alert {a} acknowledged." in resp.text
    assert notifier.store.get(a).status == st.ACKED
    bridge.handle_incoming.assert_not_called()


def test_sms_other_message_goes_to_agent(setup) -> None:
    client, _, _, bridge = setup
    with _signed():
        resp = client.post(
            "/webhooks/twilio", data={"From": OWNER, "Body": "what's next today?"}
        )
    assert resp.status_code == 200
    assert "acknowledged" not in resp.text
