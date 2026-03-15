"""Pytest configuration and fixtures for rr-mcp tests."""

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Check if rr is available at import time
_rr_available = shutil.which("rr") is not None

# Mark integration fixtures so they skip when rr is not installed
requires_rr = pytest.mark.skipif(not _rr_available, reason="rr is not installed")


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """Get the fixtures directory path."""
    return Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def programs_dir(fixtures_dir: Path) -> Path:
    """Get the programs directory path."""
    return fixtures_dir / "programs"


@pytest.fixture(scope="session")
def build_programs(programs_dir: Path) -> None:
    """Build all test programs before running tests."""
    if not _rr_available:
        pytest.skip("rr is not installed")
    build_dir = programs_dir / "build"
    build_dir.mkdir(exist_ok=True)

    # Configure with CMake
    result = subprocess.run(
        ["cmake", ".."],
        cwd=build_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"Failed to configure test programs with CMake:\n{result.stderr}")

    # Build
    result = subprocess.run(
        ["cmake", "--build", "."],
        cwd=build_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"Failed to build test programs:\n{result.stderr}")


# ---------------------------------------------------------------------------
# Session-scoped trace directory and recorded traces (recorded once, reused)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def session_trace_dir() -> Iterator[Path]:
    """Session-scoped temp directory for recorded traces.

    Traces are recorded once here and reused across all tests.
    Does NOT set _RR_TRACE_DIR (that's test-specific).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        trace_dir = Path(tmpdir) / "traces"
        trace_dir.mkdir()
        yield trace_dir


def record_trace(program_path: Path, trace_dir: Path, trace_name: str) -> Path:
    """Record an rr trace of a program.

    Args:
        program_path: Path to the program to record.
        trace_dir: Directory where traces should be stored.
        trace_name: Name for the trace.

    Returns:
        Path to the recorded trace directory.

    Raises:
        RuntimeError: If recording fails.
    """
    # Set _RR_TRACE_DIR for this recording
    env = os.environ.copy()
    env["_RR_TRACE_DIR"] = str(trace_dir)

    result = subprocess.run(
        ["rr", "record", "-n", str(program_path)],
        env=env,
        capture_output=True,
        text=True,
    )

    # Note: rr passes through the program's exit code, so non-zero doesn't mean
    # recording failed. Check stderr for actual rr errors instead.
    if result.returncode != 0 and "rr:" in result.stderr:
        raise RuntimeError(f"Failed to record trace for {program_path.name}:\n{result.stderr}")

    # rr creates a directory named after the program in _RR_TRACE_DIR
    # Find the most recently created trace (only real directories, not symlinks)
    traces = [p for p in trace_dir.glob("*") if p.is_dir() and not p.is_symlink()]
    traces = sorted(traces, key=lambda p: p.stat().st_mtime, reverse=True)

    if not traces:
        raise RuntimeError(f"No trace found after recording {program_path.name}")

    trace_path = traces[0]

    # Optionally rename to our desired name
    if trace_path.name != trace_name:
        new_path = trace_dir / trace_name
        trace_path.rename(new_path)
        trace_path = new_path

    return trace_path


@pytest.fixture(scope="session")
def recorded_crash_trace(
    build_programs: None,
    programs_dir: Path,
    session_trace_dir: Path,
) -> Path:
    """Record a trace of the crash program (once per session)."""
    program = programs_dir / "build" / "crash"
    return record_trace(program, session_trace_dir, "crash-trace")


@pytest.fixture(scope="session")
def recorded_simple_trace(
    build_programs: None,
    programs_dir: Path,
    session_trace_dir: Path,
) -> Path:
    """Record a trace of the simple program (once per session)."""
    program = programs_dir / "build" / "simple"
    return record_trace(program, session_trace_dir, "simple-trace")


@pytest.fixture(scope="session")
def recorded_threads_trace(
    build_programs: None,
    programs_dir: Path,
    session_trace_dir: Path,
) -> Path:
    """Record a trace of the threads program (once per session)."""
    program = programs_dir / "build" / "threads"
    return record_trace(program, session_trace_dir, "threads-trace")


@pytest.fixture(scope="session")
def recorded_recursive_trace(
    build_programs: None,
    programs_dir: Path,
    session_trace_dir: Path,
) -> Path:
    """Record a trace of the recursive program (once per session)."""
    program = programs_dir / "build" / "recursive"
    return record_trace(program, session_trace_dir, "recursive-trace")


@pytest.fixture(scope="session")
def recorded_fork_trace(
    build_programs: None,
    programs_dir: Path,
    session_trace_dir: Path,
) -> Path:
    """Record a trace of the fork_test program (once per session)."""
    program = programs_dir / "build" / "fork_test"
    return record_trace(program, session_trace_dir, "fork-trace")


@pytest.fixture(scope="session")
def recorded_fork_no_exec_trace(
    build_programs: None,
    programs_dir: Path,
    session_trace_dir: Path,
) -> Path:
    """Record a trace of fork_no_exec (parent forks child that never exec()s)."""
    program = programs_dir / "build" / "fork_no_exec"
    return record_trace(program, session_trace_dir, "fork-no-exec-trace")


@pytest.fixture(scope="session")
def recorded_cpp_features_trace(
    build_programs: None,
    programs_dir: Path,
    session_trace_dir: Path,
) -> Path:
    """Record a trace of the cpp_features program (once per session)."""
    program = programs_dir / "build" / "cpp_features"
    return record_trace(program, session_trace_dir, "cpp-features-trace")


# ---------------------------------------------------------------------------
# Function-scoped fixture to set _RR_TRACE_DIR to the session trace dir.
# Use this in tests that call list_traces() / resolve_trace_path() by name.
# ---------------------------------------------------------------------------


@pytest.fixture
def use_session_traces(session_trace_dir: Path) -> Iterator[Path]:
    """Point _RR_TRACE_DIR at the session trace dir for one test."""
    old = os.environ.get("_RR_TRACE_DIR")
    os.environ["_RR_TRACE_DIR"] = str(session_trace_dir)
    try:
        yield session_trace_dir
    finally:
        if old is not None:
            os.environ["_RR_TRACE_DIR"] = old
        elif "_RR_TRACE_DIR" in os.environ:
            del os.environ["_RR_TRACE_DIR"]


# ---------------------------------------------------------------------------
# Function-scoped fixtures for unit tests (mocked, no real rr)
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_trace_dir() -> Iterator[Path]:
    """Create a temporary EMPTY directory for traces.

    Function-scoped for tests that need a clean/empty trace directory.
    Sets _RR_TRACE_DIR for the duration of the test.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        trace_dir = Path(tmpdir) / "traces"
        trace_dir.mkdir()

        old_trace_dir = os.environ.get("_RR_TRACE_DIR")
        os.environ["_RR_TRACE_DIR"] = str(trace_dir)

        try:
            yield trace_dir
        finally:
            if old_trace_dir is not None:
                os.environ["_RR_TRACE_DIR"] = old_trace_dir
            elif "_RR_TRACE_DIR" in os.environ:
                del os.environ["_RR_TRACE_DIR"]


@pytest.fixture
def mock_rr_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a temporary mock rr directory."""
    rr_dir = tmp_path / "mock_rr"
    rr_dir.mkdir()
    monkeypatch.setenv("_RR_TRACE_DIR", str(rr_dir))
    return rr_dir


@pytest.fixture
def fake_trace(mock_rr_dir: Path) -> Path:
    """Create a fake trace directory for testing."""
    trace_dir = mock_rr_dir / "test-trace-0"
    trace_dir.mkdir()
    (trace_dir / "version").write_text("5")
    (trace_dir / "data").write_bytes(b"fake trace data")
    return trace_dir


@pytest.fixture
def fake_trace_with_latest(fake_trace: Path, mock_rr_dir: Path) -> Path:
    """Create a fake trace with latest-trace symlink."""
    latest = mock_rr_dir / "latest-trace"
    latest.symlink_to(fake_trace)
    return fake_trace


@pytest.fixture
def mock_subprocess_run(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock subprocess.run for testing rr commands."""
    mock = MagicMock()
    monkeypatch.setattr("subprocess.run", mock)
    return mock
