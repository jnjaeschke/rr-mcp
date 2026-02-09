"""Trace discovery and metadata extraction."""

import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from rr_mcp.errors import RrCommandError, TraceNotFoundError
from rr_mcp.models import ProcessInfo, TraceInfo, TraceSummary

# Default rr trace directory
DEFAULT_RR_DIR = Path.home() / ".local" / "share" / "rr"


def get_rr_dir() -> Path:
    """Get the rr trace directory.

    Uses _RR_TRACE_DIR environment variable if set, otherwise uses default.
    """
    env_dir = os.environ.get("_RR_TRACE_DIR")
    if env_dir:
        return Path(env_dir)
    return DEFAULT_RR_DIR


def resolve_trace_path(trace: str | None = None) -> Path:
    """Resolve a trace name or path to an absolute path.

    Args:
        trace: Trace name, path, or None for latest-trace.

    Returns:
        Absolute path to the trace directory.

    Raises:
        TraceNotFoundError: If the trace cannot be found.
    """
    rr_dir = get_rr_dir()

    if trace is None:
        # Use latest-trace symlink
        latest = rr_dir / "latest-trace"
        if not latest.exists():
            raise TraceNotFoundError("latest-trace")
        return latest.resolve()

    # Check if it's an absolute path
    trace_path = Path(trace)
    if trace_path.is_absolute():
        if not trace_path.exists():
            raise TraceNotFoundError(trace)
        return trace_path

    # Check if it's a trace name in the rr directory
    named_path = rr_dir / trace
    if named_path.exists():
        return named_path.resolve()

    # Check if it's a relative path from current directory
    if trace_path.exists():
        return trace_path.resolve()

    raise TraceNotFoundError(trace)


def list_traces() -> list[TraceSummary]:
    """List all available rr traces.

    Returns:
        List of trace summaries, sorted by creation time (newest first).
    """
    rr_dir = get_rr_dir()

    if not rr_dir.exists():
        return []

    traces: list[TraceSummary] = []

    for entry in rr_dir.iterdir():
        # Skip symlinks (like latest-trace) and non-directories
        if entry.is_symlink() or not entry.is_dir():
            continue

        # Check if it looks like a trace (has version file)
        version_file = entry / "version"
        if not version_file.exists():
            continue

        # Get directory stats
        stat = entry.stat()
        created_at = datetime.fromtimestamp(stat.st_mtime, tz=UTC)

        traces.append(
            TraceSummary(
                name=entry.name,
                path=str(entry),
                created_at=created_at,
            )
        )

    # Sort by creation time, newest first
    traces.sort(key=lambda t: t.created_at, reverse=True)
    return traces


def get_trace_info(trace: str | None = None) -> TraceInfo:
    """Get detailed information about a trace.

    Args:
        trace: Trace name, path, or None for latest-trace.

    Returns:
        Detailed trace information.

    Raises:
        TraceNotFoundError: If the trace cannot be found.
        RrCommandError: If rr command fails.
    """
    trace_path = resolve_trace_path(trace)
    stat = trace_path.stat()
    created_at = datetime.fromtimestamp(stat.st_mtime, tz=UTC)

    # Get event count using rr dump (count events)
    # This is a simplified approach - in practice we might parse trace files directly
    try:
        result = subprocess.run(
            ["rr", "traceinfo", str(trace_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RrCommandError("rr traceinfo", result.stderr, result.returncode)

        # Parse output - format varies by rr version
        output = result.stdout
        total_events = 0
        total_time_ns = 0

        # Look for event count in output
        for line in output.splitlines():
            if "events" in line.lower():
                match = re.search(r"(\d+)", line)
                if match:
                    total_events = int(match.group(1))
            if "time" in line.lower() and "ns" in line.lower():
                match = re.search(r"(\d+)", line)
                if match:
                    total_time_ns = int(match.group(1))

    except FileNotFoundError as err:
        raise RrCommandError("rr traceinfo", "rr command not found", -1) from err

    return TraceInfo(
        name=trace_path.name,
        path=str(trace_path),
        total_events=total_events,
        total_time_ns=total_time_ns,
        recording_time=created_at,
    )


def get_trace_processes(trace: str | None = None) -> list[ProcessInfo]:
    """Get all processes in a trace.

    Args:
        trace: Trace name, path, or None for latest-trace.

    Returns:
        List of process information.

    Raises:
        TraceNotFoundError: If the trace cannot be found.
        RrCommandError: If rr command fails.
    """
    trace_path = resolve_trace_path(trace)

    try:
        result = subprocess.run(
            ["rr", "ps", str(trace_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RrCommandError("rr ps", result.stderr, result.returncode)

    except FileNotFoundError as err:
        raise RrCommandError("rr ps", "rr command not found", -1) from err

    processes: list[ProcessInfo] = []

    # Parse rr ps output
    # Format: PID PPID EXIT CMD
    lines = result.stdout.strip().splitlines()

    for line in lines:
        # Skip header line if present
        if line.startswith("PID") or not line.strip():
            continue

        parts = line.split(None, 3)  # Split into at most 4 parts
        if len(parts) < 3:
            continue

        pid = int(parts[0])

        # PPID can be "--" if unavailable
        ppid_str = parts[1]
        ppid = int(ppid_str) if ppid_str != "--" else 0

        # Exit code can be a number or a signal like "SIGKILL"
        exit_str = parts[2]
        if exit_str.startswith("SIG"):
            # Convert signal to negative number (convention)
            exit_code = _signal_to_code(exit_str)
        elif exit_str == "-":
            exit_code = None
        else:
            try:
                exit_code = int(exit_str)
            except ValueError:
                exit_code = None

        # Command and args
        cmd_str = parts[3] if len(parts) > 3 else ""
        cmd_parts = cmd_str.split()
        command = cmd_parts[0] if cmd_parts else ""
        args = cmd_parts[1:] if len(cmd_parts) > 1 else []

        processes.append(
            ProcessInfo(
                pid=pid,
                ppid=ppid,
                exit_code=exit_code,
                command=command,
                args=tuple(args),
            )
        )

    return processes


def _signal_to_code(signal_name: str) -> int:
    """Convert a signal name to a negative exit code."""
    import signal as signal_module

    # Remove "SIG" prefix if present
    name = signal_name.removeprefix("SIG")

    try:
        sig = signal_module.Signals[f"SIG{name}"]
        return -sig.value
    except KeyError:
        # Unknown signal, return -1
        return -1
