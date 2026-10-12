# SPDX-License-Identifier: GPL-3.0-only
"""Privacy by construction: diagnostics never format log messages or inspect values.

Only call-site fingerprints, severity and relative timing leave this module.
Raw logs, exception text, arguments, locals, URLs and state are never collected.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from io import TextIOWrapper
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Annotated, Literal, cast

from pydantic import BaseModel, ConfigDict, Field
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class DiagnosticEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    build: str = Field(default="development", pattern=r"^(?:[a-f0-9]{12}|development)$")
    session: str = Field(default="0000000000000000", pattern=r"^[a-f0-9]{16}$")
    event: str = Field(pattern=r"^[a-f0-9]{16}$")
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    elapsed_ms: int = Field(ge=0, le=2**53)
    exception: bool
    duration_ms: int = Field(default=0, ge=0, le=600000)
    status_code: int = Field(default=0, ge=0, le=599)
    trace: list[Annotated[str, Field(pattern=r"^[a-f0-9]{16}$")]] = Field(max_length=12)


class BugReport(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    report_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    build: str = Field(pattern=r"^(?:[a-f0-9]{12}|development)$")
    category: Literal["connection", "capture", "planning", "alignment", "interface", "other"]
    events: list[DiagnosticEvent] = Field(max_length=2000)
    logging_enabled: bool


def fingerprint(module: str, line: int) -> str:
    # No message/value is ever input to this hash. It locates the statement in source.
    return hashlib.sha256(f"{module}:{line}".encode()).hexdigest()[:16]


def source_id(filename: str) -> str:
    # PyInstaller code objects use relative paths, independent of runtime CWD.
    path = Path(filename)
    if not path.is_absolute() and path.parts[:1] == ("ssc",) and path.suffix == ".py":
        parts = path.with_suffix("").parts
        if all(part.isidentifier() for part in parts):
            return ".".join(parts)
    try:
        relative = Path(filename).resolve().relative_to(Path(__file__).resolve().parents[1])
        return "ssc." + ".".join(relative.with_suffix("").parts)
    except ValueError:
        return "external"


def event_of(record: logging.LogRecord, started: float, *, build: str = "development",
             session: str = "0000000000000000") -> DiagnosticEvent:
    trace: list[str] = []
    if record.exc_info is not None:
        tb = record.exc_info[2]
        while tb is not None and len(trace) < 12:
            trace.append(fingerprint(source_id(tb.tb_frame.f_code.co_filename), tb.tb_lineno))
            tb = tb.tb_next
    return DiagnosticEvent(
        build=build, session=session,
        event=getattr(record, "diagnostic_event", fingerprint(source_id(record.pathname), record.lineno)),
        duration_ms=getattr(record, "diagnostic_duration_ms", 0),
        status_code=getattr(record, "diagnostic_status", 0),
        level=cast(Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], record.levelname if record.levelname in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"} else "INFO"),
        elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
        exception=record.exc_info is not None, trace=trace,
    )


class _PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self) -> TextIOWrapper:
        stream = super()._open()
        os.chmod(self.baseFilename, 0o600)
        return stream


class PrivateDiagnostics(logging.Handler):
    """A bounded queue keeps file I/O off the application/event-loop thread."""
    def __init__(self, directory: Path) -> None:
        super().__init__(logging.DEBUG)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        self.path = directory / "events.jsonl"
        self.started = time.monotonic()
        self.build = build_identity()
        self.session = uuid.uuid4().hex[:16]
        self.pending: queue.Queue[str | None] = queue.Queue(maxsize=10000)
        self.writer = _PrivateRotatingFileHandler(self.path, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
        self.path.chmod(0o600)
        self.worker = threading.Thread(target=self._write, name="private-diagnostics", daemon=True)
        self.worker.start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.pending.put_nowait(event_of(record, self.started, build=self.build, session=self.session).model_dump_json())
        except (queue.Full, ValueError, TypeError):
            # Never call handleError: it prints raw record arguments to stderr.
            return

    def _write(self) -> None:
        while True:
            item = self.pending.get()
            try:
                if item is None:
                    return
                self.writer.emit(logging.LogRecord("diagnostics", logging.INFO, "", 0, item, (), None))
            finally:
                self.pending.task_done()

    def close(self) -> None:
        if hasattr(self, "worker") and self.worker.is_alive():
            self.pending.put(None)
            self.worker.join(timeout=2)
            self.writer.close()
        super().close()


@contextmanager
def collect_diagnostics(directory: Path, *, enabled: bool, console_level: str) -> Iterator[None]:
    """Install after Uvicorn configures logging; own the writer through shutdown."""
    if not enabled:
        yield
        return
    root = logging.getLogger()
    application = logging.getLogger("ssc")
    previous_level = application.level
    previous_handlers = [(handler, handler.level) for handler in root.handlers]
    collector = PrivateDiagnostics(directory)
    for handler, _ in previous_handlers:
        handler.setLevel(console_level.upper())
    root.addHandler(collector)
    application.setLevel(logging.DEBUG)
    try:
        yield
    finally:
        root.removeHandler(collector)
        application.setLevel(previous_level)
        for handler, level in previous_handlers:
            handler.setLevel(level)
        collector.close()


def read_events(directory: Path) -> list[DiagnosticEvent]:
    events: list[DiagnosticEvent] = []
    # Re-validate every stored field; corrupted/extended files fail closed.
    for path in (directory / "events.jsonl.2", directory / "events.jsonl.1", directory / "events.jsonl"):
        try:
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        raw = json.loads(line)
                        item = DiagnosticEvent.model_validate(raw)
                        if any(len(part) != 16 or any(c not in "0123456789abcdef" for c in part) for part in item.trace):
                            continue
                        events.append(item)
                        if len(events) > 2000:
                            del events[:1000]
                    except (ValueError, TypeError):
                        continue
        except FileNotFoundError:
            continue
    # No absolute wall-clock time, timezone or machine identity in the report.
    return events[-2000:]


def build_identity() -> str:
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        try:
            version = (Path(sys.executable).parent / "VERSION").read_text().strip()
            match = re.fullmatch(r"0\.0\.0\+g([a-f0-9]{12})", version)
            if match:
                return match.group(1)
        except OSError:
            pass
    return "development"


class DiagnosticRequests:
    """ASGI timing without inspecting URLs, headers, bodies or client addresses."""
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        status = 500

        async def capture(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            route = scope.get("route")
            # Starlette route templates are application constants, never raw request paths.
            template = getattr(route, "path", "unmatched")
            logging.getLogger("ssc.diagnostics.http").debug("request completed", extra={
                "diagnostic_event": fingerprint("http:" + template, status),
                "diagnostic_duration_ms": min(600000, max(0, int((time.monotonic() - started) * 1000))),
                "diagnostic_status": status,
            })
