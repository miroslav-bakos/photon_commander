# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""``Plan`` compilation and durable execution (DESIGN.md 4.2).

``ssc.scheduler.compiler`` compiles a ``Plan`` into a ``CompiledSchedule``.
``ssc.scheduler.executor`` steps that schedule against a device through a set of
``Protocol`` ports (``backend-core`` supplies the SQLite-backed ``Journal``, the
Alpaca-backed ``DeviceCommands``/``DeviceEvents``, the real ``Clock`` and
``GateInputs``). ``ssc.scheduler.gates`` is the pure hysteresis those gate checks run
on, and ``ssc.scheduler.exposure`` is the exposure block's frame-cycle
arithmetic, shared by the compiler and the executor so the two cannot disagree
about what "a whole cycle" means (DESIGN.md 4.3.0). None of the three does any I/O itself — no ``sqlite3``, no ``httpx``, no
``ssc.alpaca``, no ``ssc.seestar`` (the ``pure-core`` import-linter contract enforces
this).
"""

from ssc.scheduler.compiler import (
    CompiledItem,
    CompiledPanel,
    CompiledSchedule,
    CompiledTarget,
    NightWindow,
    ResolvedEvent,
    compile_plan,
)
from ssc.scheduler.executor import (
    CapturePort,
    Clock,
    ControlCommand,
    ControlKind,
    ControlSource,
    DeviceCommandError,
    DeviceCommandNotImplementedError,
    DeviceCommands,
    DeviceEvents,
    DeviceEventTimeoutError,
    EffectCall,
    EffectDeadlines,
    EffectName,
    EphemerisTargetResolver,
    FrameCountError,
    GateInputs,
    ItemStatus,
    Journal,
    JournalRecord,
    RecordKind,
    ResolvedPointing,
    RunState,
    RunStatus,
    TargetResolutionError,
    TargetResolver,
    decide,
    reduce,
    run,
)
from ssc.scheduler.exposure import (
    FRAME_OVERHEAD_S,
    CameraLimits,
    cycle_duration_s,
    whole_cycle_fits,
    whole_cycles_in,
)
from ssc.scheduler.gates import (
    GateDecision,
    GateSample,
    GateThresholds,
    TargetSnapshot,
    evaluate_gate,
)

__all__ = [
    "FRAME_OVERHEAD_S",
    "CameraLimits",
    "CapturePort",
    "Clock",
    "CompiledItem",
    "CompiledPanel",
    "CompiledSchedule",
    "CompiledTarget",
    "ControlCommand",
    "ControlKind",
    "ControlSource",
    "DeviceCommandError",
    "DeviceCommandNotImplementedError",
    "DeviceCommands",
    "DeviceEventTimeoutError",
    "DeviceEvents",
    "EffectCall",
    "EffectDeadlines",
    "EffectName",
    "EphemerisTargetResolver",
    "FrameCountError",
    "GateDecision",
    "GateInputs",
    "GateSample",
    "GateThresholds",
    "ItemStatus",
    "Journal",
    "JournalRecord",
    "NightWindow",
    "RecordKind",
    "ResolvedEvent",
    "ResolvedPointing",
    "RunState",
    "RunStatus",
    "TargetResolutionError",
    "TargetResolver",
    "TargetSnapshot",
    "compile_plan",
    "cycle_duration_s",
    "decide",
    "evaluate_gate",
    "reduce",
    "run",
    "whole_cycle_fits",
    "whole_cycles_in",
]
