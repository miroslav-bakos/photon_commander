"""Scheduled focus recovery and sustained image-quality monitoring."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from statistics import median

from ssc.scheduler.executor import DeviceFocusConditionsError


async def adaptive_autofocus(
    run_at_exposure: Callable[[float], Awaitable[None]],
    report: Callable[[str], None],
) -> None:
    """Retry weak-star focus measurements; the caller must restore focus on failure."""
    for exposure_s in (2.0, 4.0, 8.0):
        try:
            await run_at_exposure(exposure_s)
            return
        except DeviceFocusConditionsError as exc:
            next_action = (
                "Clouds or a weak star field are suspected. Waiting 10 minutes before retrying, if time remains in the imaging window."
                if exposure_s == 8.0
                else "Retrying with a longer focus exposure."
            )
            report(
                f"Autofocus could not measure a reliable curve at {exposure_s:g}s: {exc}. {next_action}"
            )
            if exposure_s == 8.0:
                raise


@dataclass
class ScheduledFocusMonitor:
    """Request focus after three new weak-star or degraded-HFR frames.

    A relative HFR threshold avoids assuming one absolute value works for every
    optic. The first three usable frames establish the field's baseline. Each
    frame contributes once, even when the scheduler polls faster than stacking.
    """

    last_frame: object = None
    baseline: list[float] = field(default_factory=list)
    bad_frames: int = 0
    pending: bool = False

    def observe(self, frame: object, hfr: float | None, stars: int) -> bool:
        if frame == self.last_frame:
            return self.pending
        self.last_frame = frame
        poor = stars < 3 or hfr is None
        if not poor and hfr is not None:
            if len(self.baseline) < 3:
                self.baseline.append(hfr)
            else:
                poor = hfr > median(self.baseline) * 1.5
        self.bad_frames = self.bad_frames + 1 if poor else 0
        self.pending = self.pending or self.bad_frames >= 3
        return self.pending

    def focused(self) -> None:
        self.baseline.clear()
        self.bad_frames = 0
        self.pending = False
