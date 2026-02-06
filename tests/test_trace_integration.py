"""Integration tests for trace management using real rr traces."""

from pathlib import Path

import pytest

from rr_mcp.trace import (
    get_trace_info,
    get_trace_processes,
    list_traces,
    resolve_trace_path,
)


def test_list_traces_empty(temp_trace_dir: Path) -> None:  # noqa: ARG001
    """Test listing traces when directory is empty."""
    traces = list_traces()
    assert traces == []


def test_list_traces_with_recording(recorded_simple_trace: Path) -> None:
    """Test listing traces after recording."""
    traces = list_traces()
    assert len(traces) >= 1

    # Find our trace
    trace_names = [t.name for t in traces]
    assert "simple-trace" in trace_names

    # Check trace has metadata
    simple_trace = next(t for t in traces if t.name == "simple-trace")
    assert simple_trace.path == str(recorded_simple_trace)
    assert simple_trace.size_bytes > 0
    assert simple_trace.created_at is not None


def test_resolve_trace_path_absolute(recorded_simple_trace: Path) -> None:
    """Test resolving an absolute trace path."""
    resolved = resolve_trace_path(str(recorded_simple_trace))
    assert resolved == recorded_simple_trace


def test_resolve_trace_path_by_name(recorded_simple_trace: Path) -> None:
    """Test resolving a trace by name."""
    resolved = resolve_trace_path("simple-trace")
    assert resolved == recorded_simple_trace


def test_resolve_trace_path_not_found(temp_trace_dir: Path) -> None:  # noqa: ARG001
    """Test resolving a non-existent trace."""
    from rr_mcp.errors import TraceNotFoundError

    with pytest.raises(TraceNotFoundError) as exc_info:
        resolve_trace_path("nonexistent-trace")

    assert "nonexistent-trace" in str(exc_info.value)


def test_get_trace_info_real(recorded_simple_trace: Path) -> None:
    """Test getting trace metadata from real trace."""
    info = get_trace_info(str(recorded_simple_trace))

    assert info.name == "simple-trace"
    assert info.path == str(recorded_simple_trace)
    assert info.total_events >= 0
    assert info.total_time_ns >= 0
    assert info.recording_time is not None


def test_get_trace_processes_simple(recorded_simple_trace: Path) -> None:
    """Test getting processes from simple trace."""
    processes = get_trace_processes(str(recorded_simple_trace))

    assert len(processes) >= 1
    main_process = processes[0]
    assert main_process.pid > 0
    assert main_process.command.endswith("simple")
    assert main_process.exit_code == 0


def test_get_trace_processes_crash(recorded_crash_trace: Path) -> None:
    """Test getting processes from crashed program."""
    processes = get_trace_processes(str(recorded_crash_trace))

    assert len(processes) >= 1
    main_process = processes[0]
    assert main_process.exit_code == -11  # SIGSEGV


def test_get_trace_processes_threads(recorded_threads_trace: Path) -> None:
    """Test getting processes from multi-threaded program."""
    processes = get_trace_processes(str(recorded_threads_trace))

    assert len(processes) >= 1
    main_process = processes[0]
    assert main_process.command.endswith("threads")
    assert main_process.exit_code == 0
