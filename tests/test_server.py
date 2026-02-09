"""Unit tests for server.py handler logic.

Tests the MCP tool handlers for correct error handling, argument parsing,
and response formatting. Uses mocked Session objects to avoid needing rr.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rr_mcp.errors import GdbError, RrMcpError
from rr_mcp.gdbmi import BreakpointData
from rr_mcp.models import Location, SignalInfo, StopResult
from rr_mcp.server import (
    _DEBUGGING_GUIDE,
    _get_int_arg,
    _get_int_arg_with_default,
    _get_optional_int_arg,
    _handle_tool,
    _stop_result_to_dict,
    read_resource,
)

# ---------------------------------------------------------------------------
# _get_int_arg_with_default
# ---------------------------------------------------------------------------


class TestGetIntArgWithDefault:
    """Ensure explicit zero is not swallowed by the default."""

    def test_absent_key_returns_default(self) -> None:
        assert _get_int_arg_with_default({}, "count", 20) == 20

    def test_none_value_returns_default(self) -> None:
        assert _get_int_arg_with_default({"count": None}, "count", 20) == 20

    def test_explicit_zero_preserved(self) -> None:
        """The whole reason this helper exists: 0 must not become the default."""
        assert _get_int_arg_with_default({"count": 0}, "count", 20) == 0

    def test_positive_value_preserved(self) -> None:
        assert _get_int_arg_with_default({"count": 5}, "count", 20) == 5

    def test_string_value_converted(self) -> None:
        assert _get_int_arg_with_default({"count": "3"}, "count", 20) == 3


# ---------------------------------------------------------------------------
# _stop_result_to_dict
# ---------------------------------------------------------------------------


class TestStopResultToDict:
    """Ensure None raises instead of returning fake data."""

    def test_none_raises(self) -> None:
        with pytest.raises(RrMcpError, match="No stop event"):
            _stop_result_to_dict(None)

    def test_valid_result(self) -> None:
        result = StopResult(
            reason="breakpoint-hit",
            location=Location(
                event=10, tick=0, function="main", file="main.cpp", line=5, address="0x1234"
            ),
            breakpoint_id=1,
        )
        d = _stop_result_to_dict(result)
        assert d["reason"] == "breakpoint-hit"
        assert d["location"]["event"] == 10
        assert d["location"]["function"] == "main"
        assert d["breakpoint_id"] == 1

    def test_with_signal(self) -> None:
        result = StopResult(
            reason="signal-received",
            location=Location(
                event=5, tick=0, function="crash", file="crash.cpp", line=10, address="0x0"
            ),
            signal=SignalInfo(name="SIGSEGV", meaning="Segmentation fault"),
        )
        d = _stop_result_to_dict(result)
        assert d["signal"]["name"] == "SIGSEGV"
        assert d["signal"]["meaning"] == "Segmentation fault"

    def test_without_optional_fields(self) -> None:
        result = StopResult(
            reason="end-stepping-range",
            location=Location(
                event=1, tick=0, function="foo", file="foo.c", line=1, address="0x1"
            ),
        )
        d = _stop_result_to_dict(result)
        assert "signal" not in d
        assert "breakpoint_id" not in d


# ---------------------------------------------------------------------------
# Handler-level tests using mocked sessions
# ---------------------------------------------------------------------------


def _make_mock_session() -> MagicMock:
    """Create a mock Session with common async methods."""
    session = MagicMock()
    session.session_id = "test-session-id"
    session.trace = "/tmp/trace"
    session.pid = 1234
    session.get_current_location = AsyncMock(
        return_value=Location(
            event=1, tick=0, function="main", file="main.cpp", line=1, address="0x1000"
        )
    )
    session.get_current_position = AsyncMock(return_value=(1, 0))
    return session


def _make_mock_manager(session: MagicMock) -> MagicMock:
    """Create a mock SessionManager that returns the given session."""
    manager = MagicMock()
    manager.get_session.return_value = session
    return manager


class TestWatchpointSetHandler:
    """Test the watchpoint_set handler correctly uses BreakpointData."""

    @pytest.mark.asyncio
    async def test_success_returns_watchpoint_id(self) -> None:
        session = _make_mock_session()
        session.set_watchpoint = AsyncMock(
            return_value=BreakpointData(
                number=3,
                type="hw watchpoint",
                enabled=True,
                address=None,
                file=None,
                line=None,
                function=None,
                condition=None,
                times=0,
                watchpoint=True,
            )
        )

        with patch("rr_mcp.server.get_session_manager", return_value=_make_mock_manager(session)):
            result = await _handle_tool(
                "watchpoint_set",
                {"session_id": "s1", "expression": "my_var"},
            )

        assert result["watchpoint_id"] == 3
        assert result["expression"] == "my_var"

    @pytest.mark.asyncio
    async def test_failure_raises_gdb_error(self) -> None:
        session = _make_mock_session()
        session.set_watchpoint = AsyncMock(return_value=None)

        with (
            patch("rr_mcp.server.get_session_manager", return_value=_make_mock_manager(session)),
            pytest.raises(GdbError, match="Failed to set watchpoint"),
        ):
            await _handle_tool(
                "watchpoint_set",
                {"session_id": "s1", "expression": "bad_expr"},
            )

    @pytest.mark.asyncio
    async def test_failure_when_number_is_none(self) -> None:
        session = _make_mock_session()
        session.set_watchpoint = AsyncMock(
            return_value=BreakpointData(
                number=None,
                type=None,
                enabled=False,
                address=None,
                file=None,
                line=None,
                function=None,
                condition=None,
                times=0,
                watchpoint=True,
            )
        )

        with (
            patch("rr_mcp.server.get_session_manager", return_value=_make_mock_manager(session)),
            pytest.raises(GdbError, match="Failed to set watchpoint"),
        ):
            await _handle_tool(
                "watchpoint_set",
                {"session_id": "s1", "expression": "bad_expr"},
            )


class TestBreakpointSetHandler:
    """Test breakpoint_set raises on failure instead of returning soft error."""

    @pytest.mark.asyncio
    async def test_failure_raises_gdb_error(self) -> None:
        session = _make_mock_session()
        session.set_breakpoint = AsyncMock(return_value=None)

        with (
            patch("rr_mcp.server.get_session_manager", return_value=_make_mock_manager(session)),
            pytest.raises(GdbError, match="Failed to set breakpoint"),
        ):
            await _handle_tool(
                "breakpoint_set",
                {"session_id": "s1", "location": "nonexistent_func"},
            )

    @pytest.mark.asyncio
    async def test_success_returns_breakpoint_id(self) -> None:
        session = _make_mock_session()
        session.set_breakpoint = AsyncMock(
            return_value=BreakpointData(
                number=1,
                type="breakpoint",
                enabled=True,
                address="0x1234",
                file="main.cpp",
                line=10,
                function="main",
                condition=None,
                times=0,
            )
        )

        with patch("rr_mcp.server.get_session_manager", return_value=_make_mock_manager(session)):
            result = await _handle_tool(
                "breakpoint_set",
                {"session_id": "s1", "location": "main"},
            )

        assert result["breakpoint_id"] == 1
        assert len(result["locations"]) == 1


class TestFrameSelectHandler:
    """Test frame_select raises on failure."""

    @pytest.mark.asyncio
    async def test_failure_raises_gdb_error(self) -> None:
        session = _make_mock_session()
        session.select_frame = AsyncMock(return_value=False)

        with (
            patch("rr_mcp.server.get_session_manager", return_value=_make_mock_manager(session)),
            pytest.raises(GdbError, match="Failed to select frame"),
        ):
            await _handle_tool(
                "frame_select",
                {"session_id": "s1", "frame_num": 999},
            )

    @pytest.mark.asyncio
    async def test_success_returns_location(self) -> None:
        session = _make_mock_session()
        session.select_frame = AsyncMock(return_value=True)

        with patch("rr_mcp.server.get_session_manager", return_value=_make_mock_manager(session)):
            result = await _handle_tool(
                "frame_select",
                {"session_id": "s1", "frame_num": 0},
            )

        assert result["frame_num"] == 0
        assert result["function"] == "main"


class TestThreadSelectHandler:
    """Test thread_select raises on failure."""

    @pytest.mark.asyncio
    async def test_failure_raises_gdb_error(self) -> None:
        session = _make_mock_session()
        session.select_thread = AsyncMock(return_value=False)

        with (
            patch("rr_mcp.server.get_session_manager", return_value=_make_mock_manager(session)),
            pytest.raises(GdbError, match="Failed to select thread"),
        ):
            await _handle_tool(
                "thread_select",
                {"session_id": "s1", "thread_id": 99},
            )


class TestCheckpointCreateHandler:
    """Test checkpoint_create raises on failure."""

    @pytest.mark.asyncio
    async def test_failure_raises_gdb_error(self) -> None:
        session = _make_mock_session()
        session.create_checkpoint = AsyncMock(return_value=None)

        with (
            patch("rr_mcp.server.get_session_manager", return_value=_make_mock_manager(session)),
            pytest.raises(GdbError, match="Failed to create checkpoint"),
        ):
            await _handle_tool(
                "checkpoint_create",
                {"session_id": "s1"},
            )

    @pytest.mark.asyncio
    async def test_success_returns_checkpoint_id(self) -> None:
        session = _make_mock_session()
        session.create_checkpoint = AsyncMock(return_value=1)

        with patch("rr_mcp.server.get_session_manager", return_value=_make_mock_manager(session)):
            result = await _handle_tool(
                "checkpoint_create",
                {"session_id": "s1"},
            )

        assert result["checkpoint_id"] == 1
        assert result["event"] == 1


class TestCheckpointRestoreHandler:
    """Test checkpoint_restore raises on failure."""

    @pytest.mark.asyncio
    async def test_failure_raises_gdb_error(self) -> None:
        session = _make_mock_session()
        session.restore_checkpoint = AsyncMock(return_value=False)

        with (
            patch("rr_mcp.server.get_session_manager", return_value=_make_mock_manager(session)),
            pytest.raises(GdbError, match="Failed to restore checkpoint"),
        ):
            await _handle_tool(
                "checkpoint_restore",
                {"session_id": "s1", "checkpoint_id": 99},
            )


class TestExecutionHandlersUseCorrectDefaults:
    """Verify execution handlers pass correct defaults (not swallowed by `or`)."""

    @pytest.mark.asyncio
    async def test_step_count_zero_passed_through(self) -> None:
        """count=0 must reach session.step(count=0), not be replaced by 1."""
        session = _make_mock_session()
        stop = StopResult(
            reason="end-stepping-range",
            location=Location(event=1, tick=0, function="f", file="f.c", line=1, address="0x1"),
        )
        session.step = AsyncMock(return_value=stop)

        with patch("rr_mcp.server.get_session_manager", return_value=_make_mock_manager(session)):
            await _handle_tool("step", {"session_id": "s1", "count": 0})

        session.step.assert_called_once_with(count=0)

    @pytest.mark.asyncio
    async def test_continue_timeout_zero_passed_through(self) -> None:
        """timeout=0 must reach session, not be replaced by 30."""
        session = _make_mock_session()
        stop = StopResult(
            reason="exited",
            location=Location(event=1, tick=0, function=None, file=None, line=None, address="0x0"),
        )
        session.continue_execution = AsyncMock(return_value=stop)

        with patch("rr_mcp.server.get_session_manager", return_value=_make_mock_manager(session)):
            await _handle_tool("continue", {"session_id": "s1", "timeout": 0})

        session.continue_execution.assert_called_once_with(timeout_sec=0)

    @pytest.mark.asyncio
    async def test_backtrace_count_zero_passed_through(self) -> None:
        """count=0 must reach session.get_backtrace(max_depth=0)."""
        session = _make_mock_session()
        session.get_backtrace = AsyncMock(return_value=[])

        with patch("rr_mcp.server.get_session_manager", return_value=_make_mock_manager(session)):
            await _handle_tool("backtrace", {"session_id": "s1", "count": 0})

        session.get_backtrace.assert_called_once_with(max_depth=0, full=False)


class TestUnknownToolRaises:
    """Test that unknown tool names raise RrMcpError."""

    @pytest.mark.asyncio
    async def test_unknown_tool(self) -> None:
        with pytest.raises(RrMcpError, match="Unknown tool"):
            await _handle_tool("nonexistent_tool", {})


class TestGdbRawUsesEscape:
    """Test that gdb_raw properly escapes the command."""

    @pytest.mark.asyncio
    async def test_escapes_quotes_in_command(self) -> None:
        session = _make_mock_session()
        session.execute = AsyncMock(return_value=[])

        with patch("rr_mcp.server.get_session_manager", return_value=_make_mock_manager(session)):
            await _handle_tool(
                "gdb_raw",
                {"session_id": "s1", "command": 'print "hello"'},
            )

        # The command should have escaped quotes
        call_args = session.execute.call_args[0][0]
        assert r"\"hello\"" in call_args


# ---------------------------------------------------------------------------
# Float handling in _get_int_arg / _get_optional_int_arg (#13)
# ---------------------------------------------------------------------------


class TestGetIntArgFloatHandling:
    """Ensure float inputs are handled correctly."""

    def test_whole_float_converted(self) -> None:
        assert _get_int_arg({"n": 5.0}, "n") == 5

    def test_non_integer_float_rejected(self) -> None:
        with pytest.raises(TypeError, match="non-integer float"):
            _get_int_arg({"n": 5.5}, "n")

    def test_bool_rejected(self) -> None:
        """bool is subclass of int but should not be accepted."""
        with pytest.raises(TypeError, match="got bool"):
            _get_int_arg({"n": True}, "n")

    def test_optional_whole_float_converted(self) -> None:
        assert _get_optional_int_arg({"n": 3.0}, "n") == 3

    def test_optional_non_integer_float_rejected(self) -> None:
        with pytest.raises(TypeError, match="non-integer float"):
            _get_optional_int_arg({"n": 3.7}, "n")

    def test_optional_bool_rejected(self) -> None:
        with pytest.raises(TypeError, match="got bool"):
            _get_optional_int_arg({"n": False}, "n")


# ---------------------------------------------------------------------------
# Soft-failure-to-raise conversions (#7)
# ---------------------------------------------------------------------------


class TestBreakpointDeleteRaisesOnFailure:
    """breakpoint_delete must raise, not return {success: False}."""

    @pytest.mark.asyncio
    async def test_failure_raises_gdb_error(self) -> None:
        session = _make_mock_session()
        session.delete_breakpoint = AsyncMock(return_value=False)

        with (
            patch("rr_mcp.server.get_session_manager", return_value=_make_mock_manager(session)),
            pytest.raises(GdbError, match="Failed to delete breakpoint"),
        ):
            await _handle_tool(
                "breakpoint_delete",
                {"session_id": "s1", "breakpoint_id": 42},
            )

    @pytest.mark.asyncio
    async def test_success_returns_id(self) -> None:
        session = _make_mock_session()
        session.delete_breakpoint = AsyncMock(return_value=True)

        with patch("rr_mcp.server.get_session_manager", return_value=_make_mock_manager(session)):
            result = await _handle_tool(
                "breakpoint_delete",
                {"session_id": "s1", "breakpoint_id": 42},
            )

        assert result["breakpoint_id"] == 42
        assert "success" not in result


class TestBreakpointEnableRaisesOnFailure:
    """breakpoint_enable must raise, not return {success: False}."""

    @pytest.mark.asyncio
    async def test_failure_raises_gdb_error(self) -> None:
        session = _make_mock_session()
        session.enable_breakpoint = AsyncMock(return_value=False)

        with (
            patch("rr_mcp.server.get_session_manager", return_value=_make_mock_manager(session)),
            pytest.raises(GdbError, match="Failed to enable breakpoint"),
        ):
            await _handle_tool(
                "breakpoint_enable",
                {"session_id": "s1", "breakpoint_id": 5},
            )


class TestBreakpointDisableRaisesOnFailure:
    """breakpoint_disable must raise, not return {success: False}."""

    @pytest.mark.asyncio
    async def test_failure_raises_gdb_error(self) -> None:
        session = _make_mock_session()
        session.disable_breakpoint = AsyncMock(return_value=False)

        with (
            patch("rr_mcp.server.get_session_manager", return_value=_make_mock_manager(session)),
            pytest.raises(GdbError, match="Failed to disable breakpoint"),
        ):
            await _handle_tool(
                "breakpoint_disable",
                {"session_id": "s1", "breakpoint_id": 5},
            )


class TestCheckpointDeleteRaisesOnFailure:
    """checkpoint_delete must raise, not return {success: False}."""

    @pytest.mark.asyncio
    async def test_failure_raises_gdb_error(self) -> None:
        session = _make_mock_session()
        session.delete_checkpoint = AsyncMock(return_value=False)

        with (
            patch("rr_mcp.server.get_session_manager", return_value=_make_mock_manager(session)),
            pytest.raises(GdbError, match="Failed to delete checkpoint"),
        ):
            await _handle_tool(
                "checkpoint_delete",
                {"session_id": "s1", "checkpoint_id": 99},
            )

    @pytest.mark.asyncio
    async def test_success_returns_id(self) -> None:
        session = _make_mock_session()
        session.delete_checkpoint = AsyncMock(return_value=True)

        with patch("rr_mcp.server.get_session_manager", return_value=_make_mock_manager(session)):
            result = await _handle_tool(
                "checkpoint_delete",
                {"session_id": "s1", "checkpoint_id": 7},
            )

        assert result["checkpoint_id"] == 7
        assert "success" not in result


# ---------------------------------------------------------------------------
# New tool handlers: catch, handle_signal, find_in_memory, info, interrupt
# ---------------------------------------------------------------------------


class TestCatchHandler:
    """Test the catch tool handler."""

    @pytest.mark.asyncio
    async def test_catch_throw_success(self) -> None:
        session = _make_mock_session()
        session.catch_throw = AsyncMock(
            return_value=BreakpointData(
                number=5, type="catchpoint", enabled=True,
                address=None, file=None, line=None, function=None,
                condition=None, times=0,
            )
        )

        with patch("rr_mcp.server.get_session_manager", return_value=_make_mock_manager(session)):
            result = await _handle_tool(
                "catch",
                {"session_id": "s1", "event": "throw"},
            )

        assert result["catchpoint_id"] == 5
        assert result["event"] == "throw"

    @pytest.mark.asyncio
    async def test_catch_syscall_with_filter(self) -> None:
        session = _make_mock_session()
        session.catch_syscall = AsyncMock(
            return_value=BreakpointData(
                number=6, type="catchpoint", enabled=True,
                address=None, file=None, line=None, function=None,
                condition=None, times=0,
            )
        )

        with patch("rr_mcp.server.get_session_manager", return_value=_make_mock_manager(session)):
            result = await _handle_tool(
                "catch",
                {"session_id": "s1", "event": "syscall", "filter": "write"},
            )

        session.catch_syscall.assert_called_once_with("write")
        assert result["catchpoint_id"] == 6

    @pytest.mark.asyncio
    async def test_catch_failure_raises(self) -> None:
        session = _make_mock_session()
        session.catch_throw = AsyncMock(return_value=None)

        with (
            patch("rr_mcp.server.get_session_manager", return_value=_make_mock_manager(session)),
            pytest.raises(GdbError, match="Failed to set catchpoint"),
        ):
            await _handle_tool(
                "catch",
                {"session_id": "s1", "event": "throw"},
            )

    @pytest.mark.asyncio
    async def test_catch_unknown_event_raises(self) -> None:
        with (
            patch("rr_mcp.server.get_session_manager", return_value=_make_mock_manager(_make_mock_session())),
            pytest.raises(RrMcpError, match="Unknown catch event"),
        ):
            await _handle_tool(
                "catch",
                {"session_id": "s1", "event": "bogus"},
            )


class TestHandleSignalHandler:
    """Test the handle_signal tool handler."""

    @pytest.mark.asyncio
    async def test_configure_signal(self) -> None:
        session = _make_mock_session()
        session.handle_signal = AsyncMock(return_value="Signal SIGPIPE nostop noprint nopass")

        with patch("rr_mcp.server.get_session_manager", return_value=_make_mock_manager(session)):
            result = await _handle_tool(
                "handle_signal",
                {"session_id": "s1", "signal": "SIGPIPE", "stop": False, "pass_through": False},
            )

        assert result["signal"] == "SIGPIPE"
        assert "nostop" in result["output"]
        session.handle_signal.assert_called_once_with(
            "SIGPIPE", stop=False, pass_through=False, print_signal=None,
        )


class TestFindInMemoryHandler:
    """Test the find_in_memory tool handler."""

    @pytest.mark.asyncio
    async def test_returns_addresses(self) -> None:
        session = _make_mock_session()
        session.find_in_memory = AsyncMock(return_value=["0x1000", "0x2000"])

        with patch("rr_mcp.server.get_session_manager", return_value=_make_mock_manager(session)):
            result = await _handle_tool(
                "find_in_memory",
                {"session_id": "s1", "start": "0x0", "end": "0xffff", "pattern": "0xdeadbeef"},
            )

        assert result["addresses"] == ["0x1000", "0x2000"]
        assert result["count"] == 2


class TestInfoHandler:
    """Test the info tool handler."""

    @pytest.mark.asyncio
    async def test_returns_output(self) -> None:
        session = _make_mock_session()
        session.info = AsyncMock(return_value="some info output")

        with patch("rr_mcp.server.get_session_manager", return_value=_make_mock_manager(session)):
            result = await _handle_tool(
                "info",
                {"session_id": "s1", "subcommand": "proc mappings"},
            )

        assert result["output"] == "some info output"
        session.info.assert_called_once_with("proc mappings")


class TestInterruptHandler:
    """Test the interrupt tool handler."""

    @pytest.mark.asyncio
    async def test_success_returns_stop_result(self) -> None:
        session = _make_mock_session()
        stop = StopResult(
            reason="signal-received",
            location=Location(event=5, tick=0, function="f", file="f.c", line=1, address="0x1"),
            signal=SignalInfo(name="SIGINT", meaning="Interrupt"),
        )
        session.interrupt = AsyncMock(return_value=stop)

        with patch("rr_mcp.server.get_session_manager", return_value=_make_mock_manager(session)):
            result = await _handle_tool("interrupt", {"session_id": "s1"})

        assert result["reason"] == "signal-received"


# ---------------------------------------------------------------------------
# Guide resource
# ---------------------------------------------------------------------------


class TestGuideResource:
    """Test the rr://guide debugging guide resource."""

    @pytest.mark.asyncio
    async def test_guide_resource_returns_markdown(self) -> None:
        result = await read_resource("rr://guide")
        assert isinstance(result, str)
        assert result == _DEBUGGING_GUIDE

    @pytest.mark.asyncio
    async def test_guide_contains_key_sections(self) -> None:
        result = await read_resource("rr://guide")
        assert "## What is rr?" in result
        assert "## Debugging Workflows" in result
        assert "## Tool Selection" in result
        assert "## Common Pitfalls" in result

    @pytest.mark.asyncio
    async def test_guide_mentions_key_tools(self) -> None:
        result = await read_resource("rr://guide")
        # Should mention the most important tools
        for tool in ["session_create", "continue", "reverse_continue", "breakpoint_set",
                      "watchpoint_set", "backtrace", "checkpoint"]:
            assert tool in result, f"Guide should mention '{tool}'"
