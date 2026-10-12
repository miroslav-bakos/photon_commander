"""The :4700 envelope, the one modelled event, and honest passthrough."""

from __future__ import annotations

import logging

from ssc.seestar.events import (
    EventEnvelope,
    EventParser,
    MalformedFrameEvent,
    PiStatusEvent,
    SeestarEvent,
    UnknownEvent,
)


def test_the_observed_pistatus_frame_parses_to_a_typed_event() -> None:
    parser = EventParser()

    event = parser.parse('{"Event":"PiStatus","Timestamp":"1654.97","temp":37.0}')

    assert isinstance(event, PiStatusEvent)
    assert event.envelope.name == "PiStatus"
    assert event.envelope.device_uptime_s == 1654.97
    assert event.status.temp_c == 37.0
    # Nothing else was in the pushed frame, so nothing else is claimed.
    assert event.status.battery_capacity is None
    assert event.status.charger_status is None


def test_received_at_is_the_only_wall_clock_and_it_is_utc() -> None:
    event = EventParser().parse('{"Event":"PiStatus","Timestamp":"1.0","temp":1.0}')
    assert isinstance(event, PiStatusEvent)
    assert event.envelope.received_at.tzinfo is not None
    assert event.envelope.received_at.utcoffset() is not None


def test_an_unmodelled_event_passes_through_and_is_logged_once(
    caplog: logging.LogRecordCaptureFixture,
) -> None:
    parser = EventParser()
    frame = '{"Event":"Stacking","Timestamp":"2.0","frame":3}'

    with caplog.at_level(logging.INFO, logger="ssc.seestar.events"):
        first = parser.parse(frame)
        second = parser.parse(frame)

    assert isinstance(first, UnknownEvent)
    assert isinstance(second, UnknownEvent)
    assert first.envelope.raw["frame"] == 3
    assert parser.unmodelled_names == frozenset({"Stacking"})
    assert sum("Stacking" in record.getMessage() for record in caplog.records) == 1


def test_an_unnamed_frame_is_still_surfaced() -> None:
    event = EventParser().parse('{"Timestamp":"2.0","temp":30.0}')
    assert isinstance(event, UnknownEvent)
    assert event.envelope.name == "<unnamed>"


def test_malformed_lines_become_a_typed_result_not_an_exception() -> None:
    parser = EventParser()

    assert isinstance(parser.parse("not json"), MalformedFrameEvent)
    assert isinstance(parser.parse("[1,2,3]"), MalformedFrameEvent)
    assert parser.parse("   ") is None


def test_a_new_event_type_is_registered_not_guessed() -> None:
    def factory(envelope: EventEnvelope) -> SeestarEvent:
        return UnknownEvent(envelope=envelope)

    parser = EventParser()
    parser.register("Stacking", factory)

    event = parser.parse('{"Event":"Stacking","Timestamp":"2.0"}')

    assert isinstance(event, UnknownEvent)
    assert parser.unmodelled_names == frozenset()


def test_event_payloads_are_redacted_for_logging() -> None:
    event = EventParser().parse('{"Event":"X","Timestamp":"1.0","passwd":"hunter2"}')
    assert isinstance(event, UnknownEvent)
    assert "hunter2" not in repr(event.envelope.redacted())
