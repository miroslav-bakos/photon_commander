# SPDX-License-Identifier: GPL-3.0-only

"""Pure stacking helpers."""

from ssc.stacking.calibration import (
    CalibrationFrame,
    CalibrationMatch,
    CalibrationMatchError,
    match_dark,
)
from ssc.stacking.integration import StackIntegrationError, StackResult, sigma_clip_stack
from ssc.stacking.live import (
    LiveStackEngine,
    LiveStackError,
    LiveStackFrameRejectedError,
    LiveStackRun,
)
from ssc.stacking.registration import (
    FrameObservation,
    RegistrationError,
    RegistrationResult,
    register_translation,
)

__all__ = [
    "CalibrationFrame",
    "CalibrationMatch",
    "CalibrationMatchError",
    "FrameObservation",
    "LiveStackEngine",
    "LiveStackError",
    "LiveStackFrameRejectedError",
    "LiveStackRun",
    "RegistrationError",
    "RegistrationResult",
    "StackIntegrationError",
    "StackResult",
    "match_dark",
    "register_translation",
    "sigma_clip_stack",
]
