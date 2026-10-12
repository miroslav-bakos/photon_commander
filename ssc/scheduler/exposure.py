# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The exposure block's own arithmetic (DESIGN.md 4.3.0).

Two questions, one answer each, shared by the compiler (which warns about a
block *before* the night) and the executor (which asks again, against the real
clock, before every single frame): **how long is one whole cycle**, and **does
another whole one fit**.

They live in one module precisely so the two callers cannot drift. A compiler
that thought a cycle was the exposure alone would lay out a block that the
executor then cut short every time, and the plan would be quietly wrong in a
way only a frame count at dawn would reveal.

**The cycle-time model, stated plainly.** One cycle is the exposure *plus* the
fixed overhead of getting the frame off the sensor and onto disk:

    cycle = exposure_sec + FRAME_OVERHEAD_S

``FRAME_OVERHEAD_S`` is 4.0 s, the top of the measured range rather than its
middle: the download alone is ~1.6 s, 2.3-2.8 s when both cameras transfer, and
a full cycle measured ~3.5-4 s on top of the exposure (DESIGN.md 9.5.2). Taking
the top is deliberate — the cost of over-estimating is one fewer frame at the
end of a block, and the cost of under-estimating is a frame the block cannot
finish and a partial file the catalogue has to reject.

**Dithering is not in the model, and that is on purpose.** A dither adds a
settle SSC has no measured number for, and inventing one would be exactly the
guess CLAUDE.md invariant 7 rules out. It does not need to be in the model: the
executor re-asks "does a whole cycle still fit" against the wall clock before
*every* frame, so a cycle that ran long for any reason — a dither, a slow
download, a busy device — simply means the last frame is not started. Model
error costs frames, never partial files.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

#: Seconds of non-exposure time in one frame cycle — see the module docstring
#: for the measurements and for why this is the top of the range.
FRAME_OVERHEAD_S: Final = 4.0


@dataclass(frozen=True, slots=True)
class CameraLimits:
    """What the camera itself reports about what it will accept.

    Read from ``Camera.ExposureMin``/``ExposureMax``/``GainMin``/``GainMax`` and
    ``FilterWheel.Names`` by ``backend-core`` and handed to
    :func:`ssc.scheduler.compiler.compile_plan` — never hardcoded here. On the
    S30 Pro they were measured at 3e-05 s / 2000 s and gain 0-600, but those are
    facts about one device on one firmware, not constants of the scheduler.

    ``filter_names`` empty means "the wheel's positions are unknown", which is
    not the same as "there are none": the compiler then declines to check a
    block's filter rather than reporting a position it cannot verify as wrong.
    """

    exposure_min_s: float
    exposure_max_s: float
    gain_min: int
    gain_max: int
    filter_names: tuple[str, ...] = ()


def cycle_duration_s(exposure_sec: float, *, overhead_s: float = FRAME_OVERHEAD_S) -> float:
    """One whole frame cycle: the exposure plus its download/write overhead."""
    return exposure_sec + overhead_s


def whole_cycle_fits(
    remaining_s: float, exposure_sec: float, *, overhead_s: float = FRAME_OVERHEAD_S
) -> bool:
    """Is there room for another *complete* cycle in ``remaining_s`` seconds?

    The check DESIGN.md 4.3.0 insists on: "does a whole cycle fit", never "is
    there time left". Starting a 40-second cycle with 20 seconds of window left
    produces a frame the block cannot finish.
    """
    return remaining_s >= cycle_duration_s(exposure_sec, overhead_s=overhead_s)


def whole_cycles_in(
    window_s: float, exposure_sec: float, *, overhead_s: float = FRAME_OVERHEAD_S
) -> int:
    """How many whole cycles fit in ``window_s`` seconds — never a partial one,
    and never negative."""
    cycle = cycle_duration_s(exposure_sec, overhead_s=overhead_s)
    if cycle <= 0.0 or window_s <= 0.0:
        return 0
    return int(window_s // cycle)


__all__ = [
    "FRAME_OVERHEAD_S",
    "CameraLimits",
    "cycle_duration_s",
    "whole_cycle_fits",
    "whole_cycles_in",
]
