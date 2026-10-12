# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The ASCOM-facing view of what the device's command verbs are known to do.

The *evidence* lives one layer down. ``ssc.seestar.session`` records which verbs
were driven against real hardware (``VERIFIED_COMMAND_METHODS``), which were
probed and found absent (``KNOWN_ABSENT_METHODS``), and what state a verb needs
before it will act (``METHOD_PRECONDITIONS``). This module **derives** from those
sets rather than restating them, so raising a verb's confidence stays a single
data change in the protocol layer and lights up the matching ASCOM member
automatically.

What this module adds is the part that is genuinely ASCOM's problem:

* the operation list the device publishes about itself, and the ``start_``/
  ``stop_`` verb names over it — including the places where the convention does
  **not** hold, which is why the exceptions are a table and not a rule;
* the observed parameter shape of each verified verb, which becomes the
  published JSON schema of the matching ``ssc.*`` action;
* one predicate, :func:`verb_is_callable`, that every device member consults
  before it claims a capability.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from ssc.seestar.rpc import (
    CODE_METHOD_NOT_FOUND,
    CODE_NO_SOLVE_DATA,
    CODE_NOT_SUPPORTED,
    CODE_OK,
)
from ssc.seestar.session import (
    COMMAND_VOCABULARY_KNOWN,
    KNOWN_ABSENT_METHODS,
    METHOD_PRECONDITIONS,
    VERIFIED_COMMAND_METHODS,
    VERIFIED_FIRMWARE,
)

#: The firmware every observation below was made against.
OBSERVED_FIRMWARE: Final = min(VERIFIED_FIRMWARE)

__all__ = [
    "CODE_METHOD_NOT_FOUND",
    "CODE_NOT_SUPPORTED",
    "CODE_NO_SOLVE_DATA",
    "CODE_OK",
    "COMMAND_VOCABULARY_COMPLETE",
    "COMMAND_VOCABULARY_KNOWN",
    "OBSERVED_FIRMWARE",
    "OPERATIONS",
    "VERBS",
    "Verb",
    "VerbStatus",
    "start_verb",
    "stop_verb",
    "unavailable_reason",
    "verb",
    "verb_is_callable",
]


class VerbStatus(StrEnum):
    """How much is known about one command verb.

    ``VERIFIED``
        Driven against hardware on the recorded firmware. Only these are sent.
    ``ABSENT``
        Probed and answered ``103``. The name does not exist.
    ``UNVERIFIED``
        Never probed, or probed inconclusively. Its name follows the device's own
        convention, which the ``ABSENT`` list proves is not evidence.
    """

    VERIFIED = "verified"
    ABSENT = "absent"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class Verb:
    """One command verb, its evidence, and its precondition if it has one."""

    method: str
    status: VerbStatus
    note: str
    #: Parameter names observed on the wire, in the order they were sent.
    params: tuple[str, ...] = field(default_factory=tuple)
    #: The device state the verb needs, in words, or ``None`` if it has none.
    requires: str | None = None
    firmware: str = OBSERVED_FIRMWARE

    @property
    def is_callable(self) -> bool:
        return self.status is VerbStatus.VERIFIED


# --------------------------------------------------------------------------- #
# The device's own operation list
# --------------------------------------------------------------------------- #

#: Verbatim from ``get_app_state`` on firmware 8.46 — the device's own operation
#: vocabulary. The ``ssc.*`` action namespace is derived from this and nothing
#: else, so SSC's action list is the device's list rather than a wish list.
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

#: Operations whose stop verb is *not* ``stop_<op>``. Probing established both
#: halves of each pair: ``stop_capture`` and ``stop_pa`` answer 103 while
#: ``stop_exposure`` and ``stop_polar_align`` work. The convention holds for most
#: of the operation list and not all of it, which is exactly why this is a table.
_STOP_VERB_OVERRIDES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "pa": "stop_polar_align",
        "capture": "stop_exposure",
    }
)

#: Verified verbs that belong to no operation in ``get_app_state``'s list.
_EXTRA_VERBS: Final[tuple[str, ...]] = (
    "iscope_start_view",
    "iscope_stop_view",
    "stop_create_dark",
)

#: Parameter names as they were actually sent on the wire. Only verbs that have
#: been driven appear here; an unverified verb's parameters are unknown and are
#: published as such rather than as an empty schema that looks authoritative.
_VERB_PARAMS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "start_auto_goto": ("target_name", "ra", "dec", "is_j2000"),
        "iscope_start_view": ("mode",),
    }
)

_VERB_NOTES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "start_auto_goto": (
            "slews the mount and settles; RA in HOURS, dec in DEGREES, with an "
            "explicit is_j2000 flag"
        ),
        "stop_auto_goto": "aborts a slew in progress; takes no parameters",
        "iscope_start_view": (
            "enters a view mode and starts the imaging pipeline; star view is the "
            "precondition for a goto"
        ),
        "iscope_stop_view": "leaves view mode; takes no parameters",
    }
)


def start_verb(operation: str) -> str:
    """The ``start_`` verb for an operation, by the device's own convention."""
    return f"start_{operation}"


def stop_verb(operation: str) -> str:
    """The stop verb for an operation, honouring the probed exceptions."""
    return _STOP_VERB_OVERRIDES.get(operation, f"stop_{operation}")


# --------------------------------------------------------------------------- #
# The derived registry
# --------------------------------------------------------------------------- #


def _status(method: str) -> VerbStatus:
    if method in VERIFIED_COMMAND_METHODS:
        return VerbStatus.VERIFIED
    if method in KNOWN_ABSENT_METHODS:
        return VerbStatus.ABSENT
    return VerbStatus.UNVERIFIED


def _default_note(method: str, status: VerbStatus) -> str:
    if status is VerbStatus.VERIFIED:
        return f"driven against hardware on firmware {OBSERVED_FIRMWARE}"
    if status is VerbStatus.ABSENT:
        return f"probed on firmware {OBSERVED_FIRMWARE} and answered code 103 (method not found)"
    return (
        "name follows the device's start_/stop_ convention but has never been "
        "probed or called; the convention is demonstrably not universal"
    )


def _build(method: str) -> Verb:
    status = _status(method)
    precondition = METHOD_PRECONDITIONS.get(method)
    return Verb(
        method=method,
        status=status,
        note=_VERB_NOTES.get(method, _default_note(method, status)),
        params=_VERB_PARAMS.get(method, ()),
        requires=precondition.requires if precondition is not None else None,
    )


def _build_registry() -> Mapping[str, Verb]:
    names: list[str] = []
    for operation in OPERATIONS:
        names.extend((start_verb(operation), stop_verb(operation)))
    names.extend(_EXTRA_VERBS)
    names.extend(VERIFIED_COMMAND_METHODS)
    return MappingProxyType({method: _build(method) for method in sorted(set(names))})


#: Every command verb SSC knows the name of, with the evidence behind it.
VERBS: Final[Mapping[str, Verb]] = _build_registry()

#: True only once every operation's start and stop verb is callable. It is not:
#: the view/goto path is verified end to end, the rest of the operation list is
#: not, and several stop verbs do not exist at all. The UI reads this so it can
#: say so out loud rather than presenting a half-working control panel.
COMMAND_VOCABULARY_COMPLETE: Final = all(
    verb_entry.is_callable
    for operation in OPERATIONS
    for verb_entry in (VERBS[start_verb(operation)], VERBS[stop_verb(operation)])
)


def verb(method: str) -> Verb:
    """The registry entry for ``method``.

    A name nobody has probed is reported as ``UNVERIFIED`` rather than raising:
    "no evidence" is the honest answer, and it already makes the verb uncallable.
    """
    known = VERBS.get(method)
    return known if known is not None else _build(method)


def verb_is_callable(method: str) -> bool:
    """Whether the driver may send ``method`` to a device."""
    return verb(method).is_callable


def unavailable_reason(method: str) -> str:
    """Why an ASCOM member backed by ``method`` answers ``NotImplemented``."""
    entry = verb(method)
    return (
        f"the Seestar verb {entry.method!r} is {entry.status.value} on firmware "
        f"{entry.firmware} ({entry.note}); SSC will not send an unverified verb to a mount"
    )
