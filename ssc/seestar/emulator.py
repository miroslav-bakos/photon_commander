# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Offline Seestar. The whole app is built and tested against this, not the sky.

It reproduces the **behaviours** recorded in ``docs/M0-FINDINGS.md`` for firmware
8.46, not just the happy path:

* ``\\r\\n``-terminated replies, and ``\\n`` or ``\\r\\n`` accepted on input.
* Non-JSON and unterminated input is ignored **silently**, as the device does.
* An unknown method returns ``{"error": "method not found", "code": 103}`` and
  **keeps the connection open** — the ordinary error path.
* ``get_device_state`` with an **invalid key** silently omits it: ``code`` 0, no
  error, and the key simply is not in ``result``. An absent section therefore
  means "no data OR not a real key", never "feature not present". This trap is
  reproduced on purpose.
* ``get_device_state`` with **no params** is the one observed crash: it resets
  the connection and the control port then refuses new connections for several
  seconds. That is the fault that makes the reconnect path testable in CI, and it
  is a *deliberate, narrow* trigger rather than the default behaviour.
* The event stream on ``:4700`` is **not** push-then-close. The M0.6 capture
  (``tests/fixtures/wire/8.46/events_idle_timeout.jsonl``) shows a 15.0 s idle
  timeout on the *client's* traffic: three silent attaches were closed by the
  device at 15.0 s each, and one that sent a keepalive every 6 s stayed open for
  the whole window. Across 110 s of held-open listening at an idle device, zero
  frames arrived — the stream is purely event-driven, with no idle telemetry.
  Modelled by :attr:`EmulatorConfig.event_idle_timeout_s`, which a client write
  resets exactly as the device's does.
* ``start_auto_goto`` is state-gated on **two** things: star view running *and*
  the arm open (``mount.close == false``). Either one missing and it answers
  ``code`` 210 ``"not supported"`` — the method *exists*, it is unavailable in
  this state. That is a different thing from 103 and must not be read as one.
  No verb on 8.46 raises the arm (14 candidates all answer 103), so the emulator
  starts stowed and only :meth:`SeestarEmulator.set_arm` opens it — standing in
  for the person doing it from the phone app, which is the only way it happens.
* Every method in ``session.VERIFIED_COMMAND_METHODS`` is implemented, and
  **only** those: anything in ``session.KNOWN_ABSENT_METHODS`` falls through to
  the same 103 the device gives it. ``tests/seestar/test_emulator.py`` asserts
  that correspondence in both directions, so the emulator cannot drift into
  being more permissive than the device it stands for.

Payload honesty. Every payload here is now taken from a recorded exchange under
``tests/fixtures/wire/8.46/``. The two remaining exceptions are named in
:data:`ASSUMED_PAYLOAD_SHAPES` and :data:`DIVERGENT_PAYLOADS`, so a guess is
never mistaken for an observation.

Credentials. The device hands out two cleartext passwords. The emulator carries
placeholders that are obviously not secrets, so the redaction path has something
to redact without a credential-shaped string ever entering the repository.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from ssc.seestar.transport import (
    LINE_TERMINATOR,
    ChannelEndpoint,
    ConnectionLostError,
    EmulatorTransportFactory,
    PortRefusedError,
    TransportFactory,
)

_log = logging.getLogger(__name__)

#: Placeholder secrets. Deliberately self-describing so nobody can mistake one
#: for a real credential, while still exercising redaction end to end.
PLACEHOLDER_SECRET: Final = "EMULATOR-NOT-A-REAL-SECRET"

#: RFC 5737 documentation addresses. Never a connection target — the emulator is
#: reached in-process, and no real device address exists anywhere in ``ssc/``.
_DOC_IP: Final = "192.0.2.10"
_DOC_GATEWAY: Final = "192.0.2.1"

#: Methods whose payload is still the emulator's own reading rather than a
#: recording. ``get_app_state``, ``get_controls``, ``pi_get_time``,
#: ``pi_station_state`` and ``get_albums`` left this set in M0.6 — they are now
#: verbatim from ``tests/fixtures/wire/8.46/readonly_getters.jsonl``.
#:
#: ``get_setting`` stays: its full blob (100+ keys) *was* captured, but only via
#: ``get_device_state(keys=["setting"])`` and never as its own method, and SSC
#: deliberately never asks for it — it returns the connect password in cleartext.
#: The emulator models the two fields that matter and says so.
ASSUMED_PAYLOAD_SHAPES: Final[frozenset[str]] = frozenset({"get_setting"})

#: Methods where the emulator's *shape* deliberately differs from the recording,
#: with the reason. Read this before "fixing" a conformance mismatch.
DIVERGENT_PAYLOADS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "get_view_state": (
            "Two shapes have been observed: {} on a device that has not started a "
            "view since boot, and a {'View': {...}} object once it has. The "
            "emulator models the never-viewed device, consistently with its idle, "
            "stowed mount — so it returns {}. Both are real."
        ),
    }
)

#: Asking for one ``get_device_state`` key can return more than one section.
#: Observed on 8.46: the S30 Pro's wide-angle head shows up unasked-for. A caller
#: that iterates the reply must tolerate sections it did not request.
COMPANION_SECTIONS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {"camera": ("second_camera",), "focuser": ("second_focuser",)}
)

#: ``stop_<op>`` verbs whose operation is named literally in the method *and*
#: appears in ``get_app_state``'s operation list, so stopping it is observable.
#: The other three verified stops — ``stop_exposure``, ``stop_polar_align`` and
#: ``stop_create_dark`` — have no operation of that name in the list (``pa`` is
#: not ``polar_align``; there is no ``exposure`` or ``create_dark`` entry at all)
#: and which operation each clears has never been observed. So they answer 0 and
#: change nothing: an unobserved side effect is not modelled on a hunch.
STOP_VERB_OPERATIONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "stop_annotate": "annotate",
        "stop_solve": "solve",
        "stop_stack": "stack",
        "stop_export_image": "export_image",
        "stop_find_star": "find_star",
        "stop_auto_goto": "auto_goto",
    }
)

#: Operations ``get_app_state`` publishes, verbatim from
#: ``tests/fixtures/wire/8.46/readonly_getters.jsonl``. The device's own action
#: list — never derive method names from it (see ``session``: seven of fifteen
#: derived ``stop_<op>`` names do not exist).
OPERATIONS: Final[tuple[str, ...]] = (
    "annotate",
    "solve",
    "capture",
    "pa",
    "pa_3p",
    "auto_goto",
    "stack",
    "export_image",
    "merid_flip",
    "auto_focus",
    "find_star",
    "avi_record",
    "rtmp",
    "auto_exp",
    "restart_guide",
)

#: The only event frame ever observed on 8.46, captured during device activity.
#: An idle device pushes nothing at all, so this is *not* the default any more:
#: it is the script a test opts into when it wants a frame to flow.
DEFAULT_EVENT_SCRIPT: Final[tuple[Mapping[str, object], ...]] = (
    {"Event": "PiStatus", "temp": 37.0},
)


@dataclass(frozen=True, slots=True)
class EmulatorConfig:
    """Timing and identity knobs. Defaults mirror the observed device."""

    firmware: str = "8.46"
    firmware_int: int = 2846
    product_model: str = "Seestar S30 Pro"
    serial: str = "EMULATED0"
    is_verified: bool = True
    connect_lock: bool = False
    #: Observed: the event stream says nothing for about nine seconds.
    event_stream_silence_s: float = 9.0
    #: Observed: the device pushes an event and then closes the connection.
    close_event_stream_after_push: bool = True
    #: Observed on 8.46: :4700 closes after this long with **no client traffic**.
    #: Any client write resets it, so a keepalive holds the stream open — three
    #: silent attaches were closed at 15.0 s, a keepalive attach was not.
    #: ``None`` disables the timeout, for tests that do not care about it.
    event_idle_timeout_s: float | None = 15.0
    #: Observed: after the crash, the control port refuses for several seconds.
    reset_refusal_s: float = 5.0
    #: Artificial per-call latency, for exercising timeouts.
    reply_delay_s: float = 0.0
    #: Return the solve error nested inside ``result`` instead of at the top
    #: level. The 8.46 capture shows the **top-level** form for both solve
    #: getters (``code`` 215, ``error`` "no solve data"); this knob keeps the
    #: nested reader exercised.
    solve_error_nested: bool = False
    #: How long ``start_auto_goto`` reports ``move_type == "ScopeGoto"`` before
    #: settling to ``"none"``. The real slew is minutes; CI is not.
    goto_settle_s: float = 0.05
    #: The arm's resting state. A Seestar sits stowed (``mount.close == true``)
    #: and no firmware 8.46 verb raises it, so ``True`` is the honest default —
    #: a test that wants to slew must open the arm the way the user does, with
    #: :meth:`SeestarEmulator.set_arm`.
    arm_closed: bool = True


@dataclass(frozen=True, slots=True)
class _Reply:
    result: object = None
    code: int = 0
    error: str | None = None


class _Channel:
    """One in-process connection. Byte-oriented, so framing is really exercised."""

    def __init__(self) -> None:
        self._to_client: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._from_client: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._reset = False
        self._closed = False

    # -- client side: satisfies transport.ChannelEndpoint -------------------- #

    async def send_bytes(self, data: bytes) -> None:
        if self._reset:
            raise ConnectionLostError("emulator: connection reset by peer", was_reset=True)
        if self._closed:
            raise ConnectionLostError("emulator: connection closed")
        await self._from_client.put(data)

    async def receive_bytes(self) -> bytes:
        item = await self._to_client.get()
        if item is None:
            if self._reset:
                raise ConnectionLostError("emulator: connection reset by peer", was_reset=True)
            return b""
        return item

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._to_client.put_nowait(None)
        self._from_client.put_nowait(None)

    # -- device side --------------------------------------------------------- #

    @property
    def is_open(self) -> bool:
        return not self._closed and not self._reset

    async def device_read(self) -> bytes | None:
        return await self._from_client.get()

    def device_write(self, data: bytes) -> None:
        if self.is_open:
            self._to_client.put_nowait(data)

    def device_close(self) -> None:
        if self._closed or self._reset:
            return
        self._closed = True
        self._to_client.put_nowait(None)
        self._from_client.put_nowait(None)

    def device_reset(self) -> None:
        if self._reset:
            return
        self._reset = True
        self._to_client.put_nowait(None)
        self._from_client.put_nowait(None)


class SeestarEmulator:
    """An offline device that fails the way the real one fails."""

    def __init__(
        self,
        config: EmulatorConfig | None = None,
        *,
        event_script: Sequence[Mapping[str, object]] = DEFAULT_EVENT_SCRIPT,
    ) -> None:
        self._config = config or EmulatorConfig()
        self._event_script = tuple(event_script)
        self._started_at = time.monotonic()
        self._refuse_control_until = 0.0
        self._control_channels: set[_Channel] = set()
        self._event_channels: set[_Channel] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self.control_connection_count = 0
        self.event_connection_count = 0
        self.control_reset_count = 0
        # Mount / view state. The emulator starts idle with the arm stowed.
        self._view_mode: str | None = None
        self._move_type = "none"
        self._arm_closed = self._config.arm_closed
        self._tracking = False
        self._goto_target: tuple[float, float] = (0.0, 0.0)
        #: One flag per published operation. Only ``auto_goto`` moves on its own
        #: (a goto sets it and the settle clears it); the rest are scripted by a
        #: test through :meth:`set_operation_working` so a ``stop_*`` verb has
        #: something to stop.
        self._working: dict[str, bool] = dict.fromkeys(OPERATIONS, False)
        self._methods: dict[str, Callable[[Mapping[str, object]], _Reply]] = {
            "test_connection": self._m_test_connection,
            "get_device_state": self._m_get_device_state,
            "get_setting": self._m_get_setting,
            "pi_is_verified": self._m_pi_is_verified,
            "pi_station_state": self._m_pi_station_state,
            "pi_get_time": self._m_pi_get_time,
            "get_app_state": self._m_get_app_state,
            "get_camera_info": self._m_get_camera_info,
            "get_camera_state": self._m_get_camera_state,
            "get_camera_exp_and_bin": self._m_get_camera_exp_and_bin,
            "get_controls": self._m_get_controls,
            "get_focuser_position": self._m_get_focuser_position,
            "get_focuser_state": self._m_get_focuser_state,
            "get_wheel_position": self._m_get_wheel_position,
            "get_wheel_setting": self._m_get_wheel_setting,
            "get_wheel_state": self._m_get_wheel_state,
            "get_stack_info": self._m_get_stack_info,
            "get_last_solve_result": self._m_solve_result,
            "get_solve_result": self._m_solve_result,
            "get_albums": self._m_get_albums,
            "get_disk_volume": self._m_get_disk_volume,
            "get_view_state": self._m_get_view_state,
            # Verbs verified on 8.46 with the user at the scope. Everything here
            # has been seen to answer; nothing is registered on a guess.
            "iscope_start_view": self._m_iscope_start_view,
            "iscope_stop_view": self._m_iscope_stop_view,
            "start_auto_goto": self._m_start_auto_goto,
            # The stop family. Every envelope below was recorded in
            # tests/fixtures/wire/8.46/stop_verbs.jsonl with all operations idle
            # — including stop_export_image's, which is the one that is *not* a
            # bare 0. Assuming the family was uniform would have been wrong.
            "stop_auto_goto": self._m_stop_auto_goto,
            "stop_annotate": self._stop_operation("stop_annotate"),
            "stop_solve": self._stop_operation("stop_solve"),
            "stop_stack": self._stop_operation("stop_stack"),
            "stop_find_star": self._stop_operation("stop_find_star"),
            "stop_export_image": self._m_stop_export_image,
            "stop_exposure": self._m_stop_unmapped,
            "stop_polar_align": self._m_stop_unmapped,
            "stop_create_dark": self._m_stop_unmapped,
        }

    # -- wiring -------------------------------------------------------------- #

    def transport_factory(self) -> TransportFactory:
        """A drop-in replacement for :class:`~ssc.seestar.transport.TcpTransportFactory`."""
        return EmulatorTransportFactory(self._open_control, self._open_events)

    def add_method(self, name: str, handler: Callable[[Mapping[str, object]], _Reply]) -> None:
        """Register an extra method — for a test that needs one, not for guesses."""
        self._methods[name] = handler

    @property
    def methods(self) -> frozenset[str]:
        """Every method name this emulator answers. Anything else gets 103.

        Compared against ``session.VERIFIED_METHODS`` by
        ``tests/seestar/test_emulator.py`` in **both** directions: a verb the
        device has and the emulator does not would let the driver pass against a
        device that never answers, and a verb the emulator invents would let it
        pass against a device that answers 103.
        """
        return frozenset(self._methods)

    def set_arm(self, *, closed: bool) -> None:
        """Stow or raise the arm — the one thing no 8.46 verb can do.

        Every park/unpark/open-arm candidate answers 103, and a view does not
        raise it either (re-confirmed in ``view_verbs.jsonl``: ``mount.close``
        was unchanged across a star view). So the person does it from the phone
        app, and in a test this method stands in for the person.
        """
        self._arm_closed = closed

    def set_operation_working(self, operation: str, *, working: bool) -> None:
        """Script one of ``get_app_state``'s operations as running.

        Nothing in the emulator starts a stack or a solve — the ``start_*`` verbs
        for those have never been called against real hardware, so they are not
        implemented here. This lets a test put an operation in flight anyway, so
        the matching ``stop_*`` verb has something to clear.
        """
        if operation not in self._working:
            raise KeyError(
                f"{operation!r} is not one of the operations 8.46 publishes: "
                f"{', '.join(OPERATIONS)}"
            )
        self._working[operation] = working

    async def aclose(self) -> None:
        tasks, self._tasks = set(self._tasks), set()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for channel in list(self._control_channels) + list(self._event_channels):
            channel.device_close()
        self._control_channels.clear()
        self._event_channels.clear()

    # -- deliberate faults --------------------------------------------------- #

    def trigger_control_reset(self, reason: str = "input validation crash") -> None:
        """Reproduce the observed crash: reset every control connection, then
        refuse new ones for :attr:`EmulatorConfig.reset_refusal_s`."""
        self.control_reset_count += 1
        self._refuse_control_until = time.monotonic() + self._config.reset_refusal_s
        _log.debug("emulator: control server reset (%s)", reason)
        for channel in list(self._control_channels):
            channel.device_reset()
        self._control_channels.clear()

    def push_event(self, frame: Mapping[str, object]) -> None:
        """Push a frame to every attached event channel, immediately."""
        payload = self._encode({**frame, "Timestamp": self._uptime()})
        for channel in list(self._event_channels):
            channel.device_write(payload)

    # -- channel openers ----------------------------------------------------- #

    async def _open_control(self) -> ChannelEndpoint:
        remaining = self._refuse_control_until - time.monotonic()
        if remaining > 0:
            raise PortRefusedError(
                f"emulator: control port restarting, refusing for another {remaining:.2f}s"
            )
        channel = _Channel()
        self._control_channels.add(channel)
        self.control_connection_count += 1
        self._spawn(self._serve_control(channel), "emulator-control")
        return channel

    async def _open_events(self) -> ChannelEndpoint:
        channel = _Channel()
        self._event_channels.add(channel)
        self.event_connection_count += 1
        self._spawn(self._serve_events(channel), "emulator-events")
        return channel

    def _spawn(self, coro: Coroutine[object, object, None], name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- control server ------------------------------------------------------ #

    async def _serve_control(self, channel: _Channel) -> None:
        buffer = bytearray()
        try:
            while True:
                chunk = await channel.device_read()
                if chunk is None:
                    return
                buffer += chunk
                while True:
                    index = buffer.find(b"\n")
                    if index < 0:
                        break  # unterminated payload: ignored, as the device does
                    line = bytes(buffer[:index]).decode("utf-8", "replace").removesuffix("\r")
                    del buffer[: index + 1]
                    if not await self._handle_control_line(channel, line):
                        return
        finally:
            self._control_channels.discard(channel)

    async def _handle_control_line(self, channel: _Channel, line: str) -> bool:
        """Return False when the connection is gone."""
        if not line.strip():
            return True
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            # Length-prefixed framing and other malformed input: silent rejection.
            return True
        if not isinstance(request, dict):
            return True

        method = request.get("method")
        request_id = request.get("id")
        params = request.get("params")
        params_map: Mapping[str, object] = params if isinstance(params, Mapping) else {}

        if not isinstance(method, str) or not isinstance(request_id, int):
            return True

        # THE observed crash: get_device_state with no params at all.
        if method == "get_device_state" and not params_map.get("keys"):
            self.trigger_control_reset("get_device_state without keys")
            return False

        if self._config.reply_delay_s:
            await asyncio.sleep(self._config.reply_delay_s)

        handler = self._methods.get(method)
        reply = (
            _Reply(code=103, error="method not found") if handler is None else handler(params_map)
        )
        envelope: dict[str, object] = {
            "jsonrpc": "2.0",
            "Timestamp": self._uptime(),
            "method": method,
            "id": request_id,
        }
        if reply.error is None:
            envelope["result"] = reply.result
        else:
            envelope["error"] = reply.error
        envelope["code"] = reply.code
        channel.device_write(self._encode(envelope))
        return channel.is_open

    # -- event server -------------------------------------------------------- #

    async def _serve_events(self, channel: _Channel) -> None:
        try:
            await asyncio.sleep(self._config.event_stream_silence_s)
            for frame in self._event_script:
                channel.device_write(self._encode({**frame, "Timestamp": self._uptime()}))
            if self._config.close_event_stream_after_push:
                channel.device_close()
                return
            # Stay attached; push_event() writes here. The device ignores whatever
            # the client sends on this port — but it does *count* it: the
            # connection is dropped after event_idle_timeout_s with no client
            # traffic, and every write resets that timer. Three silent attaches
            # were closed at 15.0 s; one with a 6 s keepalive was not.
            await self._idle_timeout_loop(channel)
        finally:
            self._event_channels.discard(channel)

    async def _idle_timeout_loop(self, channel: _Channel) -> None:
        timeout = self._config.event_idle_timeout_s
        while True:
            if timeout is None:
                if await channel.device_read() is None:
                    return
                continue
            try:
                chunk = await asyncio.wait_for(channel.device_read(), timeout)
            except TimeoutError:
                _log.debug("emulator: event stream idle for %.2fs, closing", timeout)
                channel.device_close()
                return
            if chunk is None:
                return

    # -- helpers ------------------------------------------------------------- #

    def _uptime(self) -> str:
        """Seconds since start, as a string — the device's ``Timestamp`` shape."""
        return f"{time.monotonic() - self._started_at:.6f}"

    @staticmethod
    def _encode(obj: Mapping[str, object]) -> bytes:
        return json.dumps(obj, separators=(",", ":")).encode("utf-8") + LINE_TERMINATOR

    # -- method handlers (payloads as observed) ------------------------------ #

    def _m_test_connection(self, _: Mapping[str, object]) -> _Reply:
        return _Reply(result="server connected!")

    def _m_pi_is_verified(self, _: Mapping[str, object]) -> _Reply:
        return _Reply(result=self._config.is_verified)

    def _m_get_device_state(self, params: Mapping[str, object]) -> _Reply:
        raw_keys = params.get("keys")
        keys = [k for k in raw_keys if isinstance(k, str)] if isinstance(raw_keys, list) else []
        sections = self._device_state_sections()
        result: dict[str, object] = {}
        for key in keys:
            # An unrecognised key is **omitted from the result**, with code 0 and
            # no error — recorded in get_device_state_keys.jsonl, where
            # keys=["battery"] answers a bare {}. So a section that is simply
            # absent means "no data OR not a real key", and a caller that reads
            # absence as "feature not present" is guessing.
            if key in sections:
                result[key] = sections[key]
            for companion in COMPANION_SECTIONS.get(key, ()):
                result[companion] = sections[companion]
        return _Reply(result=result)

    def _device_state_sections(self) -> Mapping[str, Mapping[str, object]]:
        cfg = self._config
        return {
            "device": {
                "name": "ASI AIR imager",
                "firmware_ver_int": cfg.firmware_int,
                "firmware_ver_string": cfg.firmware,
                "is_verified": cfg.is_verified,
                "ios_ver": "Seestar_v1.05_rv1126bp 20260110",
                "sn": cfg.serial,
                "product_model": cfg.product_model,
                "user_product_model": cfg.product_model,
                "cpuId": "EMULATED-CPU-ID",
                "focal_len": 160.0,
                "fnumber": 5.0,
                "can_star_mode_sel_cam": True,
                "can_wide_cam_roi": True,
                "can_wide_cam_af": True,
                "can_roi_scale": True,
                "firmware_platform": 1,
            },
            # The emulator models an idle device with the arm stowed — the safe
            # resting state, and the one the M0.5 probes ran against.
            "mount": {
                "move_type": self._move_type,
                "close": self._arm_closed,
                "tracking": self._tracking,
                "equ_mode": True,
            },
            "focuser": {"state": "idle", "max_step": 2600, "step": 1209},
            # Asking for "focuser" also yields "second_focuser", and "camera"
            # also yields "second_camera": the S30 Pro has a wide-angle head.
            # See _COMPANION_SECTIONS — the device returns sections you did not
            # ask for, so a caller that iterates the reply must tolerate extras.
            "second_focuser": {"state": "idle", "max_step": 1023, "step": 512},
            "camera": {
                # [width, height] — the S30 Pro output frame is portrait.
                "chip_size": [2160, 3840],
                "pixel_size_um": 2.9,
                "debayer_pattern": "GR",
                "hpc_num": 12114,
            },
            "second_camera": {
                "chip_size": [2160, 3840],
                "pixel_size_um": 1.6,
                "debayer_pattern": "GR",
                "hpc_num": 15442,
            },
            "pi_status": {
                "is_overtemp": False,
                "temp": 39.18,
                "charger_status": "Full",
                "battery_capacity": 100,
                "charge_online": True,
                "is_typec_connected": False,
                "battery_overtemp": False,
                "battery_temp": 28,
                "battery_temp_type": "normal",
            },
            "balance_sensor": {
                "code": 0,
                "data": {"x": 0.005, "y": -0.001, "z": 0.998, "angle": 0.29342},
            },
            "storage": {
                "is_typec_connected": False,
                "connected_storage": ["emmc"],
                # Recorded in M0.6. Note every size is present twice, snake_case
                # and camelCase, with identical values — read either, not both.
                "storage_volume": [
                    {
                        "name": "emmc",
                        "state": "mounted",
                        "total_mb": 105066,
                        "totalMB": 105066,
                        "free_mb": 76854,
                        "freeMB": 76854,
                        "disk_mb": 118015,
                        "diskSizeMB": 118015,
                        "used_percent": 34,
                    }
                ],
                "cur_storage": "emmc",
            },
            # Credential-bearing sections. Present so redaction is exercised.
            "ap": {"ssid": "S30Pro-EMULATOR", "passwd": PLACEHOLDER_SECRET, "is_5g": True},
            # The real blob is 100+ keys (see ASSUMED_PAYLOAD_SHAPES). Only the
            # two fields SSC would ever care about are modelled, and one of them
            # is a password that SSC must never read.
            "setting": {"connect_lock": self._config.connect_lock, "password": PLACEHOLDER_SECRET},
            "station": {
                "server": True,
                "freq": 5240,
                "ip": _DOC_IP,
                "ssid": "emulated-network",
                "gateway": _DOC_GATEWAY,
                "netmask": "255.255.255.0",
                "sig_lev": -39,
                "key_mgmt": "WPA2-PSK",
            },
        }

    def _m_get_setting(self, _: Mapping[str, object]) -> _Reply:
        # The real blob is much larger (heater, stacking, mosaic, preview, wifi
        # country, expert mode, …). Only the two documented fields are modelled;
        # the password is what matters for the redaction contract.
        return _Reply(
            result={"connect_lock": self._config.connect_lock, "password": PLACEHOLDER_SECRET}
        )

    def _m_pi_station_state(self, _: Mapping[str, object]) -> _Reply:
        return _Reply(result=self._device_state_sections()["station"])

    def _m_pi_get_time(self, _: Mapping[str, object]) -> _Reply:
        """Key set verbatim from the recording; the instant is the emulator's own.

        The fields are **local** wall clock in ``time_zone`` — no offset, no
        epoch, no UTC marker — so a caller must combine the two or it is holding
        an ambiguous timestamp. The recording confirmed the *shape*; the moment
        it was taken is device state, not protocol, so a fixed evening instant is
        used here rather than coupling every downstream test to the second at
        which the capture happened to run.
        """
        return _Reply(
            result={
                "year": 2026,
                "mon": 8,
                "day": 28,
                "hour": 21,
                "min": 30,
                "sec": 0,
                "time_zone": "Europe/Bratislava",
            }
        )

    def _m_get_app_state(self, _: Mapping[str, object]) -> _Reply:
        """The device's own operation vocabulary, verbatim from the recording.

        Note the sub-objects are **not** uniform: only ``is_working`` is common.
        Anything that assumes a shared ``{"is_working", "state"}`` shape is wrong
        — ``capture`` has ``state``, ``solve`` has ``filename``, ``auto_goto``
        carries the last commanded ``target_ra_dec``, and ``auto_focus`` nests a
        ``reason`` object.
        """
        working = self._working
        return _Reply(
            result={
                "page": "focus",
                "annotate": {"is_working": working["annotate"], "lapse_ms": 0},
                "solve": {"is_working": working["solve"], "lapse_ms": 0, "filename": ""},
                "capture": {
                    "exposure_mode": "video",
                    "is_working": working["capture"],
                    "state": "idle",
                },
                "pa": {"is_working": working["pa"]},
                "pa_3p": {"is_working": working["pa_3p"]},
                "auto_goto": {
                    "is_working": working["auto_goto"],
                    "target_ra_dec": list(self._goto_target),
                },
                "stack": {
                    "is_working": working["stack"],
                    "frame_type": "light",
                    "stacked_frame": 0,
                    "dropped_frame": 0,
                    "total_frame": 0,
                },
                "export_image": {
                    "is_working": working["export_image"],
                    "success_frame": 0,
                    "total_frame": 0,
                    "keep": True,
                    "dst_storage": "",
                },
                "merid_flip": {"is_working": working["merid_flip"]},
                "auto_focus": {
                    "result": {},
                    "is_working": working["auto_focus"],
                    "focuser_opened": True,
                    "reason": {"comment": "manually", "code": 0},
                },
                "find_star": {"is_working": working["find_star"], "lapse_ms": 0},
                "avi_record": {
                    "is_working": working["avi_record"],
                    "lapse_sec": 0,
                    "fps": 10.0,
                    "write_file_fps": 0.0,
                },
                "rtmp": {"is_working": working["rtmp"]},
                "auto_exp": {"is_working": working["auto_exp"]},
                "restart_guide": {"is_working": working["restart_guide"]},
            }
        )

    def _m_get_camera_info(self, _: Mapping[str, object]) -> _Reply:
        return _Reply(
            result={
                "chip_size": [2160, 3840],
                "bins": [1, 2],
                "pixel_size_um": 2.9,
                "unity_gain": 0,
                "has_cooler": False,
                "is_color": True,
                "is_usb3_host": False,
                "has_hpc": False,
                "debayer_pattern": "GR",
            }
        )

    def _m_get_camera_state(self, _: Mapping[str, object]) -> _Reply:
        return _Reply(result={"state": "idle", "name": "imx585", "path": "on-board-imx585"})

    def _m_get_camera_exp_and_bin(self, _: Mapping[str, object]) -> _Reply:
        return _Reply(result={"exposure": 500000, "bin": 1})  # µs

    def _m_get_controls(self, _: Mapping[str, object]) -> _Reply:
        """Verbatim from the recording — eight controls, and **no ``value``**.

        An earlier reading of the prose gave each entry a ``value``. The device
        does not send one: ``get_controls`` describes the *ranges*, and the
        current values come from ``get_camera_exp_and_bin`` and friends. Anything
        in ``ssc/alpaca/`` that reads ``control["value"]`` is reading a field
        that has never existed.
        """
        return _Reply(
            result=[
                {"name": "Gain", "min": 0, "max": 600, "read_only": False},
                {"name": "Exposure", "min": 30, "max": 2000000000, "read_only": False},
                {"name": "Temperature", "min": 0, "max": 1, "read_only": True},
                {"name": "Red", "min": 0, "max": 100, "read_only": False},
                {"name": "Blue", "min": 0, "max": 100, "read_only": False},
                {"name": "HPC", "min": 0, "max": 1, "read_only": False},
                {"name": "ISP_Gain", "min": 0, "max": 600, "read_only": False},
                {"name": "ISP_Exposure", "min": 30, "max": 1000000, "read_only": False},
            ]
        )

    def _m_get_focuser_position(self, _: Mapping[str, object]) -> _Reply:
        return _Reply(result=1209)

    def _m_get_focuser_state(self, _: Mapping[str, object]) -> _Reply:
        return _Reply(
            result={"state": "idle", "name": "ZWO GPIO EAF", "id": 0, "max_step_range": 2600}
        )

    def _m_get_wheel_position(self, _: Mapping[str, object]) -> _Reply:
        return _Reply(result=1)  # 1-based on the wire

    def _m_get_wheel_setting(self, _: Mapping[str, object]) -> _Reply:
        return _Reply(result={"names": ["dark", "IRCUT", "LP"], "exp_sec": [2.0, 2.0, 2.0]})

    def _m_get_wheel_state(self, _: Mapping[str, object]) -> _Reply:
        # "idle" in the M0.6 recording; an earlier probe caught it mid-move.
        return _Reply(result={"id": 0, "state": "idle", "unidirection": False})

    def _m_get_stack_info(self, _: Mapping[str, object]) -> _Reply:
        return _Reply(result={"width": 0, "height": 0})

    def _m_solve_result(self, _: Mapping[str, object]) -> _Reply:
        if self._config.solve_error_nested:
            return _Reply(result={"code": 215, "error": "no solve data"})
        return _Reply(code=215, error="no solve data")

    def _m_get_albums(self, _: Mapping[str, object]) -> _Reply:
        """The real album tree, shape verbatim from the recording.

        ``list`` is a list of *groups*, each with a ``group_name`` and a list of
        works. ``thn`` is a thumbnail **path relative to the port-80 file
        server**, and it is empty for a work that has none — empty is "no
        thumbnail", not "not loaded yet". ``count`` is the number of subframes.
        """
        return _Reply(
            result={
                "path": "MyWorks",
                "list": [
                    {
                        "group_name": "DeepSky",
                        "files": [
                            {"name": "M 31_sub", "thn": "", "count": 1196, "type": 0},
                            {
                                "name": "M 31",
                                "thn": (
                                    "M 31/Stacked_625_M 31_30.0s_IRCUT_20260816-035952_thn.jpg"
                                ),
                                "count": 2,
                                "type": 0,
                            },
                            {"name": "NGC 6605", "thn": "", "count": 1, "type": 0},
                        ],
                    }
                ],
            }
        )

    def _m_get_disk_volume(self, _: Mapping[str, object]) -> _Reply:
        return _Reply(result={"totalMB": 105066, "freeMB": 76854})

    def _m_get_view_state(self, _: Mapping[str, object]) -> _Reply:
        """``{}`` — see :data:`DIVERGENT_PAYLOADS`. The emulator models a device
        that has not started a view since boot; one that has returns a ``View``
        object instead. Both shapes are recorded, so a caller that assumes a
        mapping with ``View`` in it will break on a freshly booted device."""
        return _Reply(result={})

    # -- verbs (verified on 8.46; the mount actually moves for these) --------- #

    def _published_mount(self) -> Mapping[str, object]:
        """The ``mount`` section as this emulator would *report* it right now.

        Read back through the registered ``get_device_state`` handler rather than
        off the internal attributes. A device has exactly one mount state, and so
        must the emulator: a verb that refuses while the device reports an open
        arm — or acts while it reports a stowed one — is a fiction no test should
        be able to create, including by scripting ``get_device_state`` through
        :meth:`add_method`.
        """
        result = self._methods["get_device_state"]({"keys": ["mount"]}).result
        if isinstance(result, Mapping):
            mount = result.get("mount")
            if isinstance(mount, Mapping):
                return mount
        return {}

    def _m_iscope_start_view(self, params: Mapping[str, object]) -> _Reply:
        """``result`` 0, recorded in ``view_verbs.jsonl``.

        It starts the imaging pipeline and **does not touch the mount**: that
        capture read ``mount.close`` before, during and after and it never
        changed. Entering a view is not a way to raise the arm.
        """
        mode = params.get("mode")
        self._view_mode = mode if isinstance(mode, str) else None
        return _Reply(result=0)

    def _m_iscope_stop_view(self, _: Mapping[str, object]) -> _Reply:
        self._view_mode = None
        return _Reply(result=0)

    def _m_start_auto_goto(self, params: Mapping[str, object]) -> _Reply:
        """The state preconditions, both of them, reproduced.

        Recorded on 8.46: with no star view running this answers ``code`` 210
        ``"not supported"`` and the mount does not move
        (``error_210_not_supported.jsonl``, captured with the arm *open*, so the
        view is independently necessary). M0.5 established the other half — with
        star view running but the arm stowed it also answers 210, and no verb on
        this firmware raises the arm.

        210 means *the method exists and is unavailable in this state*. It is not
        103, and treating it as "not implemented" would permanently disable a
        verb that works.
        """
        if self._published_mount().get("close") is not False:
            # Unknown (None) counts as refused: unknown is not "open".
            return _Reply(code=210, error="not supported")
        if self._view_mode != "star":
            return _Reply(code=210, error="not supported")
        ra = params.get("ra")
        dec = params.get("dec")
        if not isinstance(ra, int | float) or not isinstance(dec, int | float):
            return _Reply(code=210, error="not supported")
        self._goto_target = (float(ra), float(dec))
        self._working["auto_goto"] = True
        self._move_type = "ScopeGoto"
        self._spawn(self._settle_goto(), "emulator-goto")
        return _Reply(result=0)

    async def _settle_goto(self) -> None:
        """``move_type`` runs ``"ScopeGoto"`` then falls back to ``"none"``.

        The recorded slew showed both readings 3.4 s apart, so a client polling
        ``Slewing`` sees a sequence rather than an instant completion. The arm is
        deliberately *not* touched: the goto did not open it.
        """
        await asyncio.sleep(self._config.goto_settle_s)
        if self._working["auto_goto"]:
            self._working["auto_goto"] = False
            self._move_type = "none"
            self._tracking = True

    def _m_stop_auto_goto(self, _: Mapping[str, object]) -> _Reply:
        # Answers 0 whether or not a goto is running — a no-op stop is not an
        # error on this device, which is what made the stop family safe to probe.
        self._working["auto_goto"] = False
        self._move_type = "none"
        return _Reply(result=0)

    def _stop_operation(self, method: str) -> Callable[[Mapping[str, object]], _Reply]:
        """``stop_<op>`` for the ops named literally in the method.

        Recorded ``result`` 0 with the operation idle; it clears the operation's
        ``is_working`` so ``get_app_state`` agrees with the stop that just ran.
        """
        operation = STOP_VERB_OPERATIONS[method]

        def handler(_: Mapping[str, object]) -> _Reply:
            self._working[operation] = False
            return _Reply(result=0)

        return handler

    def _m_stop_export_image(self, _: Mapping[str, object]) -> _Reply:
        """The one stop verb that does **not** answer a bare 0.

        Recorded on 8.46: ``result`` is
        ``{"success_frame": 0, "total_frame": 0}`` — the export's tally, not a
        status integer. A caller that reads every stop's result as a number
        breaks here, which is exactly why the family was captured instead of
        assumed uniform.
        """
        self._working["export_image"] = False
        return _Reply(result={"success_frame": 0, "total_frame": 0})

    def _m_stop_unmapped(self, _: Mapping[str, object]) -> _Reply:
        """``stop_exposure``, ``stop_polar_align``, ``stop_create_dark``.

        All three answer ``result`` 0 (recorded). None of them names an operation
        that ``get_app_state`` publishes, and none has been observed stopping
        anything, so none of them changes state here. Modelling ``pa`` for
        ``stop_polar_align`` would be a guess dressed as fidelity.
        """
        return _Reply(result=0)
