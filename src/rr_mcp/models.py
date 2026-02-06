"""Data models for rr-mcp."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TypedDict


@dataclass(frozen=True)
class TraceSummary:
    """Summary information about an rr trace."""

    name: str
    path: str
    created_at: datetime
    size_bytes: int


@dataclass(frozen=True)
class TraceInfo:
    """Detailed information about an rr trace."""

    name: str
    path: str
    total_events: int
    total_time_ns: int
    recording_time: datetime


@dataclass(frozen=True)
class ProcessInfo:
    """Information about a process in a trace."""

    pid: int
    ppid: int
    exit_code: int | None
    command: str
    args: tuple[str, ...]
    event_start: int
    event_end: int


class SessionState(Enum):
    """State of a replay session.

    Note: With the current synchronous GDB/MI architecture, sessions are always
    in PAUSED state when accessible to clients (commands block until completion).
    RUNNING and STEPPING would only be meaningful with async execution.
    """

    PAUSED = "paused"
    RUNNING = "running"  # Currently unused (synchronous execution)
    STEPPING = "stepping"  # Currently unused (synchronous execution)
    CLOSED = "closed"


@dataclass(frozen=True)
class Location:
    """A location in the debugged program."""

    event: int
    tick: int
    function: str | None
    file: str | None
    line: int | None
    address: str


@dataclass(frozen=True)
class SignalInfo:
    """Information about a signal."""

    name: str
    meaning: str


@dataclass(frozen=True)
class StopResult:
    """Result of an execution command (continue, step, etc.)."""

    reason: str
    location: Location
    signal: SignalInfo | None = None
    breakpoint_id: int | None = None


@dataclass
class SessionInfo:
    """Information about an active session."""

    session_id: str
    trace: str
    pid: int
    state: SessionState


@dataclass(frozen=True)
class CheckpointInfo:
    """Information about an rr checkpoint."""

    id: int
    event: int
    tick: int


@dataclass(frozen=True)
class ThreadInfo:
    """Information about a thread."""

    id: int
    name: str | None
    state: str | None
    current: bool
    frame: "FrameInfo | None"


@dataclass(frozen=True)
class FrameInfo:
    """Information about a stack frame."""

    level: int | None
    function: str | None
    file: str | None
    line: int | None
    address: str | None


class MemoryData(TypedDict):
    """Memory examination result."""

    address: str
    value: str


class VariableDict(TypedDict):
    """Variable information as dictionary."""

    name: str
    value: str | None
    type: str | None


class BacktraceFrameDict(TypedDict, total=False):
    """Stack frame information as dictionary."""

    level: int | None
    func: str | None
    file: str | None
    line: int | None
    addr: str | None
    locals: list[VariableDict]  # Optional, only if full=True


class ThreadDict(TypedDict, total=False):
    """Thread information as dictionary."""

    id: int
    name: str | None
    state: str | None
    frame: "FrameDict | None"


class FrameDict(TypedDict, total=False):
    """Frame information as dictionary."""

    func: str | None
    file: str | None
    line: int | None


class SourceLineEntry(TypedDict):
    """A single source code line."""

    line_num: int
    content: str


class SourceLinesDict(TypedDict, total=False):
    """Source code lines information."""

    file: str | None
    start_line: int
    lines: list[SourceLineEntry]
    current_line: int | None
    error: str  # Optional error message


# JSON response types for MCP server
class LocationDict(TypedDict):
    """Location information as JSON dict."""

    event: int
    tick: int
    function: str | None
    file: str | None
    line: int | None
    address: str


class SignalDict(TypedDict):
    """Signal information as JSON dict."""

    name: str
    meaning: str


class StopResultDict(TypedDict, total=False):
    """Stop result as JSON dict."""

    reason: str
    location: LocationDict
    signal: SignalDict  # Optional
    breakpoint_id: int  # Optional


class ErrorInfoDict(TypedDict, total=False):
    """Error information as JSON dict."""

    code: str
    message: str
    trace: str  # Optional, for TraceNotFoundError
    session_id: str  # Optional, for SessionNotFoundError


class ErrorResponseDict(TypedDict):
    """Error response as JSON dict."""

    success: bool
    error: ErrorInfoDict
