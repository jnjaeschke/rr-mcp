"""Tests for trace discovery and metadata."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from rr_mcp.errors import RrCommandError, TraceNotFoundError
from rr_mcp.trace import (
    get_rr_dir,
    get_trace_info,
    get_trace_processes,
    list_traces,
    resolve_trace_path,
)


# Mock helper functions for unit tests
def create_mock_rr_traceinfo_output(events: int, time_ns: int) -> str:
    """Create mock output from rr traceinfo."""
    return f"Trace has {events} events\nTotal time: {time_ns} ns\n"


def create_mock_rr_ps_output(processes: list[tuple[int, int, str, str]]) -> str:
    """Create mock output from rr ps."""
    lines = ["PID\tPPID\tEXIT\tCMD"]
    for pid, ppid, exit_code, cmd in processes:
        lines.append(f"{pid}\t{ppid}\t{exit_code}\t{cmd}")
    return "\n".join(lines) + "\n"


class TestGetRrDir:
    """Tests for get_rr_dir()."""

    def test_returns_default_when_no_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Should return default path when _RR_TRACE_DIR is not set."""
        monkeypatch.delenv("_RR_TRACE_DIR", raising=False)
        result = get_rr_dir()
        assert result == Path.home() / ".local" / "share" / "rr"

    def test_returns_env_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Should return _RR_TRACE_DIR when set."""
        monkeypatch.setenv("_RR_TRACE_DIR", "/custom/rr/dir")
        result = get_rr_dir()
        assert result == Path("/custom/rr/dir")


class TestResolveTracePath:
    """Tests for resolve_trace_path()."""

    def test_none_returns_latest_trace(self, fake_trace_with_latest: Path) -> None:
        """Should resolve None to latest-trace symlink."""
        result = resolve_trace_path(None)
        assert result == fake_trace_with_latest

    def test_none_raises_when_no_latest(self, mock_rr_dir: Path) -> None:
        """Should raise when latest-trace doesn't exist."""
        with pytest.raises(TraceNotFoundError) as exc_info:
            resolve_trace_path(None)
        assert "latest-trace" in str(exc_info.value)

    def test_absolute_path_exists(self, fake_trace: Path) -> None:
        """Should accept valid absolute paths."""
        result = resolve_trace_path(str(fake_trace))
        assert result == fake_trace

    def test_absolute_path_not_exists(self) -> None:
        """Should raise for non-existent absolute paths."""
        with pytest.raises(TraceNotFoundError):
            resolve_trace_path("/nonexistent/trace/path")

    def test_name_in_rr_dir(self, fake_trace: Path, mock_rr_dir: Path) -> None:
        """Should resolve trace name to path in rr dir."""
        result = resolve_trace_path("test-trace-0")
        assert result == fake_trace

    def test_name_not_in_rr_dir(self, mock_rr_dir: Path) -> None:
        """Should raise for unknown trace name."""
        with pytest.raises(TraceNotFoundError):
            resolve_trace_path("nonexistent-trace")


class TestListTraces:
    """Tests for list_traces()."""

    def test_empty_dir(self, mock_rr_dir: Path) -> None:
        """Should return empty list for empty rr dir."""
        result = list_traces()
        assert result == []

    def test_single_trace(self, fake_trace: Path) -> None:
        """Should find a single trace."""
        result = list_traces()
        assert len(result) == 1
        assert result[0].name == "test-trace-0"
        assert result[0].path == str(fake_trace)
        assert result[0].size_bytes > 0

    def test_multiple_traces_sorted_by_time(self, mock_rr_dir: Path) -> None:
        """Should return traces sorted by creation time (newest first)."""
        import time

        # Create first trace
        trace1 = mock_rr_dir / "trace-1"
        trace1.mkdir()
        (trace1 / "version").write_text("5")
        (trace1 / "data").write_bytes(b"data1")

        time.sleep(0.01)  # Ensure different timestamps

        # Create second trace
        trace2 = mock_rr_dir / "trace-2"
        trace2.mkdir()
        (trace2 / "version").write_text("5")
        (trace2 / "data").write_bytes(b"data2")

        result = list_traces()
        assert len(result) == 2
        # Newest first
        assert result[0].name == "trace-2"
        assert result[1].name == "trace-1"

    def test_ignores_symlinks(self, fake_trace_with_latest: Path, mock_rr_dir: Path) -> None:
        """Should not include symlinks like latest-trace."""
        result = list_traces()
        names = [t.name for t in result]
        assert "latest-trace" not in names
        assert "test-trace-0" in names

    def test_ignores_non_traces(self, mock_rr_dir: Path) -> None:
        """Should ignore directories without version file."""
        not_trace = mock_rr_dir / "not-a-trace"
        not_trace.mkdir()
        (not_trace / "random-file").write_text("not a trace")

        result = list_traces()
        assert all(t.name != "not-a-trace" for t in result)


class TestGetTraceInfo:
    """Tests for get_trace_info()."""

    def test_with_mock_rr(self, fake_trace: Path, mock_subprocess_run: MagicMock) -> None:
        """Should parse rr traceinfo output."""
        mock_subprocess_run.return_value = MagicMock(
            returncode=0,
            stdout=create_mock_rr_traceinfo_output(events=5000, time_ns=10000000000),
            stderr="",
        )

        result = get_trace_info(str(fake_trace))

        assert result.name == "test-trace-0"
        assert result.path == str(fake_trace)
        assert result.total_events == 5000
        assert result.total_time_ns == 10000000000

    def test_rr_command_fails(self, fake_trace: Path, mock_subprocess_run: MagicMock) -> None:
        """Should raise RrCommandError when rr fails."""
        mock_subprocess_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="error: invalid trace",
        )

        with pytest.raises(RrCommandError) as exc_info:
            get_trace_info(str(fake_trace))
        assert "rr traceinfo" in str(exc_info.value)

    def test_trace_not_found(self, mock_rr_dir: Path) -> None:
        """Should raise TraceNotFoundError for invalid trace."""
        with pytest.raises(TraceNotFoundError):
            get_trace_info("nonexistent")


class TestGetTraceProcesses:
    """Tests for get_trace_processes()."""

    def test_parses_rr_ps_output(self, fake_trace: Path, mock_subprocess_run: MagicMock) -> None:
        """Should parse rr ps output correctly."""
        mock_subprocess_run.return_value = MagicMock(
            returncode=0,
            stdout=create_mock_rr_ps_output(
                [
                    (12345, 12340, "0", "/usr/bin/firefox -P debug"),
                    (12350, 12345, "SIGSEGV", "/usr/lib/firefox/firefox-bin -contentproc"),
                    (12360, 12345, "-", "/usr/bin/helper"),
                ]
            ),
            stderr="",
        )

        result = get_trace_processes(str(fake_trace))

        assert len(result) == 3

        # First process
        assert result[0].pid == 12345
        assert result[0].ppid == 12340
        assert result[0].exit_code == 0
        assert result[0].command == "/usr/bin/firefox"
        assert result[0].args == ("-P", "debug")

        # Second process (crashed with SIGSEGV)
        assert result[1].pid == 12350
        assert result[1].ppid == 12345
        assert result[1].exit_code == -11  # SIGSEGV
        assert result[1].command == "/usr/lib/firefox/firefox-bin"

        # Third process (still running, no exit code)
        assert result[2].pid == 12360
        assert result[2].exit_code is None

    def test_rr_command_fails(self, fake_trace: Path, mock_subprocess_run: MagicMock) -> None:
        """Should raise RrCommandError when rr ps fails."""
        mock_subprocess_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="error: cannot open trace",
        )

        with pytest.raises(RrCommandError) as exc_info:
            get_trace_processes(str(fake_trace))
        assert "rr ps" in str(exc_info.value)

    def test_empty_trace(self, fake_trace: Path, mock_subprocess_run: MagicMock) -> None:
        """Should return empty list for trace with no processes."""
        mock_subprocess_run.return_value = MagicMock(
            returncode=0,
            stdout="PID\tPPID\tEXIT\tCMD\n",
            stderr="",
        )

        result = get_trace_processes(str(fake_trace))
        assert result == []
