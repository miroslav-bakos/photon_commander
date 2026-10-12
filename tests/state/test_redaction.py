"""Credential scrubbing on the way out (docs/M0-FINDINGS.md, second exposure)."""

from __future__ import annotations

from ssc.state.redaction import REDACTED, redact, redact_mapping


def test_the_two_known_device_credential_keys_are_scrubbed() -> None:
    payload = {
        "ap": {"ssid": "Seestar", "passwd": "hunter2"},
        "setting": {"password": "hunter2", "connect_lock": False},
    }
    scrubbed = redact_mapping(payload)
    assert scrubbed == {
        "ap": {"ssid": "Seestar", "passwd": REDACTED},
        "setting": {"password": REDACTED, "connect_lock": False},
    }
    assert "hunter2" not in repr(scrubbed)


def test_scrubbing_reaches_into_lists_and_nested_objects() -> None:
    payload = {"peers": [{"name": "a", "psk": "s3cret"}, {"name": "b"}]}
    assert redact(payload) == {"peers": [{"name": "a", "psk": REDACTED}, {"name": "b"}]}


def test_matching_is_case_insensitive_and_leaves_everything_else_alone() -> None:
    assert redact({"Password": "x", "temp": 37.0}) == {"Password": REDACTED, "temp": 37.0}
    assert redact("passwd") == "passwd"
    assert redact(None) is None


def test_the_outbound_gate_is_at_least_as_strong_as_the_wire_gate() -> None:
    """code-reviewer finding 3.

    ``EventEnvelope.raw`` reaches ``GET /api/state`` / the WS ``device.event``
    frame through this gate alone (``ssc.main._event_record``); the wire-layer
    gate (``ssc.seestar.transport.redact``) only ever touches log lines. A gate
    here that matches whole key names, while the wire gate matches substrings,
    means a key like ``ap_passwd`` sails straight through to the browser.
    """
    payload = {
        "ap_passwd": "hunter2",
        "wifi_password": "s3cret",
        "access_token": "abc",
        "auth_challenge": "xyz",
        "key_mgmt": "WPA2-PSK",  # must survive: not a secret (matches transport.py's own case)
    }
    scrubbed = redact_mapping(payload)
    assert scrubbed == {
        "ap_passwd": REDACTED,
        "wifi_password": REDACTED,
        "access_token": REDACTED,
        "auth_challenge": REDACTED,
        "key_mgmt": "WPA2-PSK",
    }
    for leaked in ("hunter2", "s3cret", "abc", "xyz"):
        assert leaked not in repr(scrubbed)
