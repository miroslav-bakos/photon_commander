"""Summarize one ConformU machine-readable results file to stdout.

Exits non-zero iff ``ErrorCount > 0`` — ConformU's own distinction between a
protocol/behaviour violation (``Errors``, the hard gate) and a softer
conformance deviation (``Issues``, printed for the record but not blocking;
see ``docs/CONFORMANCE.md``). Used by ``scripts/run-conformu.sh``; not part of
``ssc``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: {argv[0]} <device-name> <results.json>", file=sys.stderr)
        return 2
    device, results_path = argv[1], argv[2]
    data = json.loads(Path(results_path).read_text())

    error_count = data.get("ErrorCount", 0)
    issue_count = data.get("IssueCount", 0)
    status = "FAIL" if error_count > 0 else "ok"
    print(f"  {device}: {status} — Errors={error_count} Issues={issue_count}")

    for error in data.get("Errors", []):
        print(f"    ERROR [{error.get('Key')}] {error.get('Value')}")
    for issue in data.get("Issues", []):
        print(f"    issue [{issue.get('Key')}] {issue.get('Value')}")

    return 1 if error_count > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
