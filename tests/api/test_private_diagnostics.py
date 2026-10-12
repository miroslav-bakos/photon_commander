"""Canaries must never reach disk, previews or receiver-shaped JSON."""
import json
import logging
import sys
import time

import pytest
from pydantic import ValidationError

from ssc.api.diagnostics import (
    BugReport,
    DiagnosticEvent,
    PrivateDiagnostics,
    event_of,
    read_events,
)

CANARIES = [
    "48.148596", "17.107748", "Bratislava", "Alice Secret", "alice@example.org",
    "192.168.1.99", "2001:db8::1234", "/home/alice/private/photo.fits", r"C:\Users\Alice\secret",
    "Bearer private-token-123", "wifi-password-123", "M31-private-plan", "RA=12.345 DEC=-67.89",
    "https://secret.example/?api_key=private", "-----BEGIN PRIVATE KEY-----",
]


@pytest.mark.parametrize("secret", CANARIES)
def test_log_values_and_exception_text_never_enter_report(secret, tmp_path):
    try:
        raise ValueError({"location": secret, "token": secret})
    except ValueError:
        record = logging.LogRecord(secret, logging.ERROR, f"/home/{secret}/main.py", 42,
                                   secret + " %s", ({"nested": secret},), sys.exc_info())
    record.stack_info = secret
    record.location = secret
    handler = PrivateDiagnostics(tmp_path / "diagnostics")
    handler.emit(record)
    handler.pending.join()
    handler.close()
    report = BugReport(report_id="a" * 32, build="development", category="other",
                       events=read_events(tmp_path / "diagnostics"), logging_enabled=True)
    serialized = report.model_dump_json()
    assert secret not in serialized
    assert secret not in (tmp_path / "diagnostics/events.jsonl").read_text()
    assert report.events[0].exception is True
    assert set(json.loads(serialized)) == {"schema_version", "report_id", "build", "category", "events", "logging_enabled"}


def test_never_formats_even_hostile_messages():
    class PrivateValue:
        def __str__(self):
            raise AssertionError("Private value was inspected")
    record = logging.LogRecord("ssc", logging.DEBUG, "source.py", 2, PrivateValue(), (), None)
    assert event_of(record, time.monotonic()).level == "DEBUG"


@pytest.mark.parametrize("field", ["location", "message", "email", "headers", "cookies", "settings", "body"])
def test_unknown_fields_rejected_at_both_schema_boundaries(field):
    event = {"event": "a" * 16, "elapsed_ms": 0, "level": "DEBUG", "exception": False, "trace": []}
    with pytest.raises(ValidationError):
        DiagnosticEvent.model_validate({**event, field: CANARIES})
    report = {"report_id": "a" * 32, "build": "development", "category": "other", "events": [event], "logging_enabled": True}
    with pytest.raises(ValidationError):
        BugReport.model_validate({**report, field: CANARIES})


def test_rejects_trace_text_and_contaminated_stored_events(tmp_path):
    event = {"event": "a" * 16, "elapsed_ms": 0, "level": "ERROR", "exception": True, "trace": ["alice@example.org"]}
    with pytest.raises(ValidationError):
        DiagnosticEvent.model_validate(event)
    (tmp_path / "events.jsonl").write_text(json.dumps(event) + "\n" + json.dumps({**event, "trace": [], "location": CANARIES}) + "\n")
    assert read_events(tmp_path) == []


def test_report_api_rejects_private_fields_and_uses_only_fixed_receiver(paired, respx_mock):
    import httpx

    from ssc.api.routes.bug_reports import REPORT_ENDPOINT

    preview = paired.post("/api/bug-reports/preview", json={"category": "connection"})
    assert preview.status_code == 200
    report = preview.json()
    assert "location" not in report
    receiver = respx_mock.post(REPORT_ENDPOINT).mock(return_value=httpx.Response(
        202, json={"report_id": report["report_id"]},
    ))
    sent = paired.post("/api/bug-reports/send", json={"report_id": report["report_id"]})
    assert sent.status_code == 200
    assert receiver.call_count == 1
    request = receiver.calls[0].request
    assert "cookie" not in request.headers
    assert "authorization" not in request.headers
    assert json.loads(request.content) == report


def test_receiver_failure_is_not_reported_as_delivery(paired, respx_mock):
    import httpx

    from ssc.api.routes.bug_reports import REPORT_ENDPOINT

    report = paired.post("/api/bug-reports/preview", json={"category": "other"}).json()
    respx_mock.post(REPORT_ENDPOINT).mock(return_value=httpx.Response(503, text="private server details"))
    response = paired.post("/api/bug-reports/send", json={"report_id": report["report_id"]})
    assert response.status_code == 502
    assert "private server details" not in response.text


def test_browser_events_reject_values_and_unknown_routes(paired):
    response = paired.post("/api/bug-reports/event", json={"kind": "action", "screen": "settings", "control": "button", "value": CANARIES})
    assert response.status_code == 422
    assert paired.post("/api/bug-reports/event", json={"kind": "navigation", "screen": "Alice"}).status_code == 422


def test_send_cannot_replace_preview_with_client_payload(paired):
    preview = paired.post("/api/bug-reports/preview", json={"category": "other"}).json()
    response = paired.post("/api/bug-reports/send", json={"report_id": preview["report_id"], "events": CANARIES})
    assert response.status_code == 422


def test_rotation_preserves_private_permissions_and_session_provenance(tmp_path, monkeypatch):
    import ssc.api.diagnostics as diagnostics

    record = logging.LogRecord('ssc', logging.INFO, diagnostics.__file__, 1, 'safe', (), None)
    monkeypatch.setattr(diagnostics, 'build_identity', lambda: 'a' * 12)
    first = PrivateDiagnostics(tmp_path)
    first.writer.maxBytes = 400
    for _ in range(4):
        first.emit(record)
    first.pending.join()
    first.close()
    old_session = first.session
    monkeypatch.setattr(diagnostics, 'build_identity', lambda: 'b' * 12)
    second = PrivateDiagnostics(tmp_path)
    second.emit(record)
    second.pending.join()
    second.close()
    events = read_events(tmp_path)
    assert any(e.build == 'a' * 12 and e.session == old_session for e in events)
    assert events[-1].build == 'b' * 12
    assert events[-1].session != old_session
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in tmp_path.glob('events.jsonl*'))


def test_same_basename_source_locations_are_distinct():
    from pathlib import Path

    import ssc.api.diagnostics as diagnostics

    root = Path(diagnostics.__file__).resolve().parents[1]
    records = [logging.LogRecord('ssc', logging.ERROR, str(root / path), 42, 'safe', (), None)
               for path in ('settings/store.py', 'state/store.py')]
    assert event_of(records[0], time.monotonic()).event != event_of(records[1], time.monotonic()).event


def test_packaged_code_paths_match_source_checkout(monkeypatch):
    import ssc.api.diagnostics as diagnostics

    expected = diagnostics.source_id(diagnostics.__file__)
    monkeypatch.setattr(diagnostics, '__file__', '/bundle/_internal/ssc/api/diagnostics.py')
    assert diagnostics.source_id('ssc/api/diagnostics.py') == expected
    assert diagnostics.source_id('ssc/../private.py') == 'external'


def test_trace_heavy_preview_fits_receiver_and_uploads_unchanged(paired, monkeypatch, respx_mock):
    import httpx

    from ssc.api.routes import bug_reports

    event = DiagnosticEvent(event='a' * 16, level='ERROR', elapsed_ms=2**53,
                            exception=True, trace=['b' * 16] * 12)
    monkeypatch.setattr(bug_reports, 'read_events', lambda _: [event] * 2000)
    preview = paired.post('/api/bug-reports/preview', json={'category': 'other'})
    assert preview.status_code == 200
    report = preview.json()
    assert 0 < len(report['events']) < 2000
    receiver = respx_mock.post(bug_reports.REPORT_ENDPOINT).mock(return_value=httpx.Response(
        202, json={'report_id': report['report_id']},
    ))
    assert paired.post('/api/bug-reports/send', json={'report_id': report['report_id']}).status_code == 200
    request = receiver.calls[0].request
    assert len(request.content) <= 512_000
    assert json.loads(request.content) == report


def test_application_lifespan_owns_collector(config, gateway):
    import uvicorn

    from ssc.main import create_app
    from tests.api.conftest import CLIENT_ADDRESS, TestClient

    app = create_app(config, gateway=gateway)
    # This is the real server startup ordering; no socket is opened.
    uvicorn.Config(app)
    with TestClient(app, client=CLIENT_ADDRESS) as client:
        from tests.api.conftest import pairing_token
        assert client.post('/api/auth/pair', json={'token': pairing_token(client)}).status_code == 200
        collectors = [h for h in logging.getLogger().handlers if isinstance(h, PrivateDiagnostics)]
        assert len(collectors) == 1
        collector = collectors[0]
        assert collector.worker.is_alive()
        logging.getLogger('ssc.diagnostics').debug('private-canary %s', 'private-canary')
        collector.pending.join()
        response = client.post('/api/bug-reports/preview', json={'category': 'other'})
        assert response.status_code == 200
        assert response.json()['events']
        assert 'private-canary' not in response.text
    assert not collector.worker.is_alive()
    assert collector not in logging.getLogger().handlers


async def test_embedded_alpaca_configuration_preserves_collector(tmp_path, monkeypatch):
    import uvicorn

    from ssc.api.diagnostics import collect_diagnostics
    from ssc.main import AlpacaRuntime

    class StopBeforeBindingError(Exception):
        pass

    def stop_before_binding(config):
        raise StopBeforeBindingError

    monkeypatch.setattr(uvicorn, 'Server', stop_before_binding)
    with collect_diagnostics(tmp_path, enabled=True, console_level='INFO'):
        collector = next(h for h in logging.getLogger().handlers if isinstance(h, PrivateDiagnostics))
        runtime = AlpacaRuntime(port=0)
        try:
            with pytest.raises(StopBeforeBindingError):
                await runtime.start()
            assert collector.worker.is_alive()
            logging.getLogger('ssc.diagnostics').debug('safe event')
            collector.pending.join()
            assert read_events(tmp_path)
        finally:
            await runtime.aclose()


def test_reporter_details_are_separate_from_sanitized_preview(paired, respx_mock):
    import httpx

    from ssc.api.routes.bug_reports import REPORT_ENDPOINT

    report = paired.post('/api/bug-reports/preview', json={'category': 'capture'}).json()
    assert 'details' not in report
    receiver = respx_mock.post(REPORT_ENDPOINT).mock(return_value=httpx.Response(202, json={'report_id': report['report_id']}))
    details = 'Second frame failed.\nExpected capture to continue. Žluťoučký'
    assert paired.post('/api/bug-reports/send', json={'report_id': report['report_id'], 'details': details}).status_code == 200
    assert json.loads(receiver.calls[0].request.content) == {**report, 'details': details}
    assert paired.post('/api/bug-reports/send', json={'report_id': report['report_id'], 'details': 'x' * 4001}).status_code == 422


def test_native_compiled_diagnostics_use_release_identity(tmp_path, monkeypatch):
    import sys

    import ssc.api.diagnostics as diagnostics

    (tmp_path / 'VERSION').write_text('0.0.0+g123456abcdef\n')
    monkeypatch.setattr(sys, 'executable', str(tmp_path / 'ssc-core.exe'))
    monkeypatch.setattr(diagnostics, '__compiled__', object(), raising=False)
    assert diagnostics.build_identity() == '123456abcdef'


def test_empty_diagnostics_preview_explains_recovery(paired, monkeypatch):
    monkeypatch.setattr('ssc.api.routes.bug_reports.read_events', lambda folder: [])
    response = paired.post('/api/bug-reports/preview', json={'category':'other'})
    assert response.status_code == 409
    assert 'Enable diagnostics' in response.json()['detail']
