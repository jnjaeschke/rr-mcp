"""Session management for rr replay sessions."""

import asyncio
import logging
import re
import uuid

from pygdbmi.gdbcontroller import GdbController

from rr_mcp.errors import GdbError, RrMcpError, SessionNotFoundError
from rr_mcp.gdbmi import BreakpointData, GdbMi, GdbMiRecord, VariableData, _safe_int
from rr_mcp.models import (
    BacktraceFrameDict,
    CheckpointInfo,
    Location,
    SessionState,
    SignalInfo,
    SourceLineEntry,
    SourceLinesDict,
    StopResult,
    ThreadDict,
    VariableDict,
)

logger = logging.getLogger(__name__)


def _as_str(value: object) -> str:
    """Coerce a value to str.  If *value* is a list, return the last element."""
    if isinstance(value, list):
        return str(value[-1]) if value else ""
    return str(value) if value is not None else ""


# Default maximum number of concurrent sessions
DEFAULT_MAX_SESSIONS = 10


class Session:
    """An rr replay session.

    Each session wraps a single rr replay process communicating via GDB/MI.
    """

    def __init__(self, trace: str, pid: int | None = None, fork_pid: int | None = None) -> None:
        """Initialize a session.

        Args:
            trace: Path to the rr trace.
            pid: Process ID to debug (None for rr default). Requires the process
                to have called exec().
            fork_pid: Start the debug server when this PID has been fork()d.
                Use for forked-without-exec child processes (e.g. Firefox content
                processes). Mutually exclusive with pid.
        """
        if pid is not None and fork_pid is not None:
            raise RrMcpError("pid and fork_pid are mutually exclusive")
        self.session_id: str = str(uuid.uuid4())
        self.trace: str = trace
        self.pid: int | None = pid
        self.fork_pid: int | None = fork_pid
        self.state: SessionState = SessionState.PAUSED
        self._gdb: GdbMi | None = None

    async def start(self) -> Location:
        """Start the rr replay process.

        Spawns rr replay with GDB/MI interface.

        Returns:
            The initial location where execution started.
        """
        command = ["rr", "replay", "--interpreter=mi"]

        if self.pid is not None:
            command.extend(["-p", str(self.pid)])
        elif self.fork_pid is not None:
            command.extend(["-f", str(self.fork_pid)])

        command.append(self.trace)

        # GdbController is synchronous, run in executor
        loop = asyncio.get_running_loop()
        controller = await loop.run_in_executor(
            None,
            lambda: GdbController(command=command),
        )

        self._gdb = GdbMi(controller)

        try:
            # Flush initial GDB output by executing a harmless command.
            # With -f (fork_pid), rr replays the entire trace forward until the
            # target fork() event, which can take minutes for large traces.
            startup_timeout = 300 if self.fork_pid is not None else 30
            await self._read_until_ready(timeout_sec=startup_timeout)

            # Enable pretty-printers for STL containers etc.
            await self._gdb.enable_pretty_printing()

            # Enable pending breakpoints so breakpoints can be set before libraries load
            await self._gdb.execute_raw("-gdb-set breakpoint pending on")

            # Get initial location
            return await self.get_current_location()
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        """Close the session and terminate the rr process."""
        if self._gdb is not None:
            await self._gdb.close()
            self._gdb = None
        self.state = SessionState.CLOSED

    async def execute(self, command: str) -> list[GdbMiRecord]:
        """Execute a raw GDB/MI command.

        This is an escape hatch for commands not covered by the typed API.

        Args:
            command: The GDB/MI command to execute.

        Returns:
            List of response records from GDB.

        Raises:
            GdbError: If the session is not active.
        """
        if self._gdb is None or self.state == SessionState.CLOSED:
            raise GdbError("Session is not active")

        return await self._gdb.execute_raw(command)

    async def _read_until_ready(self, timeout_sec: int = 30) -> list[GdbMiRecord]:
        """Wait for rr/GDB to become ready, then flush initial output.

        For -f (fork_pid) sessions, rr replays the trace forward before starting
        GDB, which can take minutes.  This method periodically checks whether the
        rr process is still alive so that crashes (e.g. replay divergence on a
        stale trace) are detected immediately instead of after a long timeout.
        """
        if self._gdb is None:
            return []

        import time

        deadline = time.monotonic() + timeout_sec
        loop = asyncio.get_running_loop()
        all_records: list[GdbMiRecord] = []

        while True:
            # Check if the rr process died (e.g. stale trace, replay divergence)
            if not self._gdb.is_process_alive():
                rc, stderr = self._gdb.get_process_exit_info()
                # Extract the most useful lines from stderr
                error_lines = [
                    ln
                    for ln in stderr.splitlines()
                    if any(tag in ln for tag in ("[FATAL", "[ERROR", "Assertion", "divergence"))
                ]
                summary = "\n".join(error_lines) if error_lines else stderr[-2000:]
                raise RrMcpError(f"rr process exited with code {rc} before GDB started:\n{summary}")

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RrMcpError(f"Timeout waiting for rr/GDB to start after {timeout_sec} seconds")

            # Try reading output with a short timeout so we can recheck liveness
            poll_sec = min(2, int(remaining) or 1)
            try:
                records: list[GdbMiRecord] = await loop.run_in_executor(
                    None,
                    lambda t=poll_sec: self._gdb._gdb.get_gdb_response(  # type: ignore[misc,arg-type]
                        timeout_sec=t,
                        raise_error_on_timeout=True,
                    ),
                )
                all_records.extend(records)

                # Got output — GDB has started.  Send a command to flush remaining init.
                flush = await self._gdb.execute_raw("-gdb-show version", timeout_sec=10)
                all_records.extend(flush)
                return all_records
            except Exception:
                # Timeout reading — loop around to check liveness again
                pass

            # Yield to the event loop so we don't busy-spin
            await asyncio.sleep(0.2)

    async def get_current_position(self) -> tuple[int, int]:
        """Get the current event and tick position.

        Returns:
            Tuple of (event, tick).
        """
        if self._gdb is None:
            return (0, 0)
        return await self._gdb.rr_when()

    async def get_current_location(self) -> Location:
        """Get the current location (position + frame info).

        Returns:
            Current location.
        """
        if self._gdb is None:
            return Location(event=0, tick=0, function=None, file=None, line=None, address="0x0")

        event, tick = await self._gdb.rr_when()
        frame = await self._gdb.stack_info_frame()

        return Location(
            event=event,
            tick=tick,
            function=frame.function if frame else None,
            file=frame.file if frame else None,
            line=frame.line if frame else None,
            address=(frame.address or "0x0") if frame else "0x0",
        )

    def _has_stopped_notification(self, records: list[GdbMiRecord]) -> bool:
        """Check if records contain a stop-like notification.

        Args:
            records: List of GDB/MI response records.

        Returns:
            True if a stopped, exited, or thread-exited notification is present.
        """
        return any(
            record.get("type") == "notify"
            and record.get("message") in ("stopped", "thread-exited", "exited")
            for record in records
        )

    async def _exec_and_wait(
        self, response: list[GdbMiRecord], timeout_sec: int = 30
    ) -> StopResult | None:
        """Wait for stop notification if needed, then parse the stop result.

        All GDB exec commands can be asynchronous (returning ^running first).
        This method ensures we always wait for the actual stop notification.
        """
        if self._gdb is not None and not self._has_stopped_notification(response):
            stop_response = await self._gdb._wait_for_stop(timeout_sec=timeout_sec)
            response.extend(stop_response)
        return await self._parse_stop_result(response)

    async def _parse_stop_result(self, records: list[GdbMiRecord]) -> StopResult | None:
        """Parse a stop result from GDB/MI records and populate event/tick.

        Uses the LAST stopped notification when multiple are present. This
        handles cases where intermediate signals (e.g. SIGSYS in Firefox
        content processes) produce transient stops before the real stop.

        Args:
            records: List of GDB/MI response records.

        Returns:
            StopResult if a stop was found, None otherwise.
        """
        # Find the LAST stop notification — earlier ones may be transient
        # signal stops that were immediately resumed.
        last_stop: GdbMiRecord | None = None
        for record in records:
            if record.get("type") == "notify" and record.get("message") in (
                "stopped",
                "thread-exited",
                "exited",
            ):
                last_stop = record

        if last_stop is not None:
            # Handle case where payload itself might be None
            payload = last_stop.get("payload") or {}

            # Get current position — may fail in transient GDB states
            try:
                event, tick = await self.get_current_position()
            except GdbError:
                logger.debug("_parse_stop_result: rr when failed, using (0,0)")
                event, tick = 0, 0

            # Extract signal info if present
            signal_info = None
            sig_name = payload.get("signal-name")
            if sig_name:
                signal_info = SignalInfo(
                    name=_as_str(sig_name),
                    meaning=_as_str(payload.get("signal-meaning", "")),
                )

            # Handle case where frame key exists but has None value
            frame = payload.get("frame") or {}

            # Get reason - if not present, infer from context
            reason = payload.get("reason")
            if not reason:
                # For reverse operations, GDB sometimes doesn't include reason
                # Default to "end-stepping-range" which is most common
                reason = "end-stepping-range"

            return StopResult(
                reason=_as_str(reason),
                location=Location(
                    event=event,
                    tick=tick,
                    function=frame.get("func"),
                    file=frame.get("file"),
                    line=_safe_int(frame.get("line")),
                    address=frame.get("addr", "0x0"),
                ),
                signal=signal_info,
                breakpoint_id=_safe_int(payload.get("bkptno")),
            )

        # If no stopped notification found, check for "done" result with frame info
        # This can happen with some GDB configurations
        for record in records:
            if record.get("type") == "result" and record.get("message") == "done":
                # Handle case where payload itself might be None
                payload = record.get("payload") or {}
                # Handle case where frame key exists but has None value
                frame = payload.get("frame") or {}

                # Only create StopResult if we have frame information
                if frame:
                    try:
                        event, tick = await self.get_current_position()
                    except GdbError:
                        logger.debug("_parse_stop_result: rr when failed (done), using (0,0)")
                        event, tick = 0, 0
                    return StopResult(
                        reason="end-stepping-range",
                        location=Location(
                            event=event,
                            tick=tick,
                            function=frame.get("func"),
                            file=frame.get("file"),
                            line=_safe_int(frame.get("line")),
                            address=frame.get("addr", "0x0"),
                        ),
                        signal=None,
                        breakpoint_id=None,
                    )

        return None

    # -------------------------------------------------------------------------
    # Execution control
    # -------------------------------------------------------------------------

    async def step(self, count: int = 1) -> StopResult | None:
        """Step forward by source lines (into functions)."""
        if self._gdb is None:
            return None
        response = await self._gdb.exec_step(count=count)
        return await self._exec_and_wait(response)

    async def reverse_step(self, count: int = 1) -> StopResult | None:
        """Step backward by source lines (into functions)."""
        if self._gdb is None:
            return None
        response = await self._gdb.exec_step(count=count, reverse=True)
        return await self._exec_and_wait(response)

    async def next(self, count: int = 1) -> StopResult | None:
        """Step forward by source lines (over functions)."""
        if self._gdb is None:
            return None
        response = await self._gdb.exec_next(count=count)
        return await self._exec_and_wait(response)

    async def reverse_next(self, count: int = 1) -> StopResult | None:
        """Step backward by source lines (over functions)."""
        if self._gdb is None:
            return None
        response = await self._gdb.exec_next(count=count, reverse=True)
        return await self._exec_and_wait(response)

    async def continue_execution(self, timeout_sec: int = 30) -> StopResult | None:
        """Continue execution forward until breakpoint or end."""
        if self._gdb is None:
            return None
        # exec_continue returns quickly (^running), the real wait is in _exec_and_wait
        response = await self._gdb.exec_continue()
        return await self._exec_and_wait(response, timeout_sec=timeout_sec)

    async def reverse_continue(self, timeout_sec: int = 30) -> StopResult | None:
        """Continue execution backward until breakpoint or beginning."""
        if self._gdb is None:
            return None
        # exec_continue returns quickly (^running), the real wait is in _exec_and_wait
        response = await self._gdb.exec_continue(reverse=True)
        return await self._exec_and_wait(response, timeout_sec=timeout_sec)

    async def finish(self) -> StopResult | None:
        """Finish executing current function (step out)."""
        if self._gdb is None:
            return None
        response = await self._gdb.exec_finish()
        return await self._exec_and_wait(response)

    async def reverse_finish(self) -> StopResult | None:
        """Reverse to the start of current function (reverse step out)."""
        if self._gdb is None:
            return None
        response = await self._gdb.exec_finish(reverse=True)
        return await self._exec_and_wait(response)

    async def step_instruction(self, count: int = 1) -> StopResult | None:
        """Step forward by machine instructions (into calls)."""
        if self._gdb is None:
            return None
        response = await self._gdb.exec_step_instruction(count=count)
        return await self._exec_and_wait(response)

    async def reverse_step_instruction(self, count: int = 1) -> StopResult | None:
        """Step backward by machine instructions (into calls)."""
        if self._gdb is None:
            return None
        response = await self._gdb.exec_step_instruction(count=count, reverse=True)
        return await self._exec_and_wait(response)

    async def next_instruction(self, count: int = 1) -> StopResult | None:
        """Step forward by machine instructions (over calls)."""
        if self._gdb is None:
            return None
        response = await self._gdb.exec_next_instruction(count=count)
        return await self._exec_and_wait(response)

    async def reverse_next_instruction(self, count: int = 1) -> StopResult | None:
        """Step backward by machine instructions (over calls)."""
        if self._gdb is None:
            return None
        response = await self._gdb.exec_next_instruction(count=count, reverse=True)
        return await self._exec_and_wait(response)

    async def run_to_event(self, event: int) -> StopResult | None:
        """Run to a specific event number in the trace."""
        if self._gdb is None:
            return None
        response = await self._gdb.rr_run_to_event(event)
        return await self._exec_and_wait(response)

    async def interrupt(self) -> StopResult | None:
        """Interrupt a running program.

        Sends -exec-interrupt to stop the inferior. Useful when the program
        is running (e.g., during a long continue) and needs to be stopped.

        Returns:
            StopResult if the program was interrupted, None if session is inactive.
        """
        if self._gdb is None:
            return None
        response = await self._gdb.exec_interrupt()
        return await self._exec_and_wait(response, timeout_sec=5)

    # -------------------------------------------------------------------------
    # Breakpoints
    # -------------------------------------------------------------------------

    async def set_breakpoint(
        self, location: str, temporary: bool = False, condition: str | None = None
    ) -> BreakpointData | None:
        """Set a breakpoint at the specified location.

        Args:
            location: Function name, filename:line, or address (*0x123).
            temporary: If True, breakpoint is deleted after first hit.
            condition: Optional condition expression for conditional breakpoint.

        Returns:
            Breakpoint data if successful, None otherwise.
        """
        if self._gdb is None:
            return None
        return await self._gdb.break_insert(location, temporary, condition)

    async def delete_breakpoint(self, breakpoint_num: int) -> bool:
        """Delete a breakpoint.

        Args:
            breakpoint_num: The breakpoint number to delete.

        Returns:
            True if successful, False otherwise.
        """
        if self._gdb is None:
            return False
        return await self._gdb.break_delete(breakpoint_num)

    async def enable_breakpoint(self, breakpoint_num: int) -> bool:
        """Enable a breakpoint.

        Args:
            breakpoint_num: The breakpoint number to enable.

        Returns:
            True if successful, False otherwise.
        """
        if self._gdb is None:
            return False
        return await self._gdb.break_enable(breakpoint_num)

    async def disable_breakpoint(self, breakpoint_num: int) -> bool:
        """Disable a breakpoint.

        Args:
            breakpoint_num: The breakpoint number to disable.

        Returns:
            True if successful, False otherwise.
        """
        if self._gdb is None:
            return False
        return await self._gdb.break_disable(breakpoint_num)

    async def list_breakpoints(self) -> list[BreakpointData]:
        """List all breakpoints.

        Returns:
            List of breakpoint information.
        """
        if self._gdb is None:
            return []
        return await self._gdb.break_list()

    # -------------------------------------------------------------------------
    # Watchpoints
    # -------------------------------------------------------------------------

    async def set_watchpoint(
        self, expression: str, access_type: str = "write"
    ) -> BreakpointData | None:
        """Set a watchpoint on an expression.

        Args:
            expression: The expression to watch (e.g., variable name).
            access_type: "write", "read", or "access".

        Returns:
            Breakpoint data with watchpoint=True if successful, None otherwise.
        """
        if self._gdb is None:
            return None
        return await self._gdb.break_watch(expression, access_type)

    # -------------------------------------------------------------------------
    # Catch events
    # -------------------------------------------------------------------------

    async def catch_throw(self) -> BreakpointData | None:
        """Set a catchpoint for C++ throw events."""
        if self._gdb is None:
            return None
        return await self._gdb.catch_throw()

    async def catch_catch(self) -> BreakpointData | None:
        """Set a catchpoint for C++ catch events."""
        if self._gdb is None:
            return None
        return await self._gdb.catch_catch()

    async def catch_syscall(self, syscall: str | None = None) -> BreakpointData | None:
        """Set a catchpoint for syscall events."""
        if self._gdb is None:
            return None
        return await self._gdb.catch_syscall(syscall)

    async def catch_signal(self, signal: str | None = None) -> BreakpointData | None:
        """Set a catchpoint for signal events."""
        if self._gdb is None:
            return None
        return await self._gdb.catch_signal(signal)

    # -------------------------------------------------------------------------
    # Signal handling
    # -------------------------------------------------------------------------

    async def handle_signal(
        self,
        signal: str,
        stop: bool | None = None,
        pass_through: bool | None = None,
        print_signal: bool | None = None,
    ) -> str:
        """Configure how GDB handles a signal.

        Args:
            signal: Signal name (e.g., "SIGPIPE", "SIGUSR1", "all").
            stop: Whether to stop on this signal.
            pass_through: Whether to pass the signal to the program.
            print_signal: Whether to print when the signal is received.

        Returns:
            The GDB console output describing the new configuration.
        """
        if self._gdb is None:
            return ""
        return await self._gdb.handle_signal(signal, stop, pass_through, print_signal)

    # -------------------------------------------------------------------------
    # Stack navigation
    # -------------------------------------------------------------------------

    async def get_backtrace(
        self, max_depth: int | None = None, full: bool = False
    ) -> list[BacktraceFrameDict]:
        """Get the call stack backtrace.

        Args:
            max_depth: Maximum number of frames to return (None for all, 0 for none).
            full: If True, include local variables for each frame.

        Returns:
            List of stack frame dictionaries.
        """
        if self._gdb is None:
            return []

        if max_depth is not None:
            if max_depth <= 0:
                return []
            end: int | None = max_depth - 1
        else:
            end = None
        frames = await self._gdb.stack_list_frames(start=0, end=end)

        # Convert to dict format
        result: list[BacktraceFrameDict] = []
        for f in frames:
            frame_dict: BacktraceFrameDict = {
                "level": f.level,
                "func": f.function,
                "file": f.file,
                "line": f.line,
                "addr": f.address,
            }

            # Add locals if requested
            if full and f.level is not None:
                # Select this frame and get its locals
                await self._gdb.stack_select_frame(f.level)
                locals_data = await self._gdb.stack_list_variables()
                frame_dict["locals"] = [
                    VariableDict(name=v.name, value=v.value, type=v.type) for v in locals_data
                ]

            result.append(frame_dict)

        # Restore frame 0 after iteration if we selected other frames
        if full and frames:
            await self._gdb.stack_select_frame(0)

        return result

    async def select_frame(self, frame_num: int) -> bool:
        """Select a stack frame.

        Args:
            frame_num: The frame number (0 = current frame).

        Returns:
            True if successful, False otherwise.
        """
        if self._gdb is None:
            return False
        return await self._gdb.stack_select_frame(frame_num)

    async def get_local_variables(self) -> list[VariableData]:
        """Get local variables in the current frame.

        Returns:
            List of variable data with name/value/type.
        """
        if self._gdb is None:
            return []
        return await self._gdb.stack_list_variables()

    async def get_function_arguments(self) -> list[list[VariableData]]:
        """Get function arguments for frames on the stack.

        Returns:
            List of argument lists, one per frame.
        """
        if self._gdb is None:
            return []
        return await self._gdb.stack_list_arguments()

    # -------------------------------------------------------------------------
    # Memory and registers
    # -------------------------------------------------------------------------

    async def read_memory(self, address: str, size: int) -> bytes | None:
        """Read memory at an address.

        Args:
            address: Memory address as hex string (e.g., "0x12345").
            size: Number of bytes to read.

        Returns:
            Bytes read from memory, or None if failed.
        """
        if self._gdb is None:
            return None
        result = await self._gdb.data_read_memory_bytes(address, size)
        return result.contents if result else None

    async def examine_memory(
        self, address: str, count: int = 16, format_char: str = "x", unit_size: str = "w"
    ) -> list[tuple[str, str]]:
        """Examine memory with formatting (like GDB's x command).

        Args:
            address: Memory address or expression.
            count: Number of units to display.
            format_char: Format - x(hex), d(decimal), s(string), i(instruction).
            unit_size: Unit size - b(byte), h(halfword), w(word), g(giant).

        Returns:
            List of (address, value) tuples.
        """
        if self._gdb is None:
            return []
        return await self._gdb.data_examine_memory(address, count, format_char, unit_size)

    async def read_registers(self, gp_only: bool = False) -> dict[str, str]:
        """Read CPU registers.

        Args:
            gp_only: If True, only return general-purpose registers
                (rax..r15, rip, eflags) to reduce output size. Recommended
                for LLM clients to avoid huge SIMD register values.

        Returns:
            Dictionary mapping register names to values (as hex strings).
        """
        if self._gdb is None:
            return {}

        registers = await self._gdb.data_list_register_values(gp_only=gp_only)
        return {reg.name: reg.value for reg in registers}

    async def evaluate_expression(self, expression: str) -> str | None:
        """Evaluate an expression in the current context.

        Args:
            expression: The expression to evaluate.

        Returns:
            String representation of the result, or None if failed.
        """
        if self._gdb is None:
            return None
        return await self._gdb.data_evaluate_expression(expression)

    async def find_in_memory(
        self, start: str, end: str, pattern: str, size: str | None = None
    ) -> list[str]:
        """Search memory for a byte pattern.

        Args:
            start: Start address (hex string or expression).
            end: End address (hex string or expression).
            pattern: Search pattern (hex bytes, string, or expression).
            size: Optional unit size: b, h, w, or g.

        Returns:
            List of addresses where the pattern was found.
        """
        if self._gdb is None:
            return []
        return await self._gdb.find_in_memory(start, end, pattern, size)

    # -------------------------------------------------------------------------
    # Info commands
    # -------------------------------------------------------------------------

    async def info(self, subcommand: str) -> str:
        """Run a GDB 'info' subcommand.

        Args:
            subcommand: The info subcommand (e.g., "proc mappings", "shared",
                        "symbol 0x12345", "types", "signals").

        Returns:
            Console output from the command.
        """
        if self._gdb is None:
            return ""
        return await self._gdb.info_command(subcommand)

    # -------------------------------------------------------------------------
    # Checkpoints
    # -------------------------------------------------------------------------

    async def create_checkpoint(self) -> int | None:
        """Create a checkpoint at the current execution point.

        Returns:
            Checkpoint ID if successful, None otherwise.
        """
        if self._gdb is None:
            return None
        return await self._gdb.rr_checkpoint_create()

    async def restore_checkpoint(self, checkpoint_id: int) -> bool:
        """Restore execution to a checkpoint.

        Args:
            checkpoint_id: The checkpoint ID to restore.

        Returns:
            True if successful, False otherwise.
        """
        if self._gdb is None:
            return False
        return await self._gdb.rr_checkpoint_restore(checkpoint_id)

    async def delete_checkpoint(self, checkpoint_id: int) -> bool:
        """Delete a checkpoint.

        Args:
            checkpoint_id: The checkpoint ID to delete.

        Returns:
            True if successful, False otherwise.
        """
        if self._gdb is None:
            return False
        return await self._gdb.rr_checkpoint_delete(checkpoint_id)

    async def list_checkpoints(self) -> list[CheckpointInfo]:
        """List all checkpoints.

        Returns:
            List of checkpoint information.
        """
        if self._gdb is None:
            return []

        response = await self._gdb.rr_checkpoint_list()
        checkpoints: list[CheckpointInfo] = []

        for record in response:
            if record.get("type") == "console":
                payload = record.get("payload")
                if not isinstance(payload, str):
                    continue
                output = payload

                # Log the output for debugging
                if output.strip():
                    logger.debug("Checkpoint list output: %s", output)

                for line in output.splitlines():
                    # Try matching with both event and tick
                    match = re.match(r"\s*(\d+)\s+.*event\s+(\d+).*tick\s+(\d+)", line)
                    if match:
                        checkpoints.append(
                            CheckpointInfo(
                                id=int(match.group(1)),
                                event=int(match.group(2)),
                                tick=int(match.group(3)),
                            )
                        )
                    else:
                        # Fallback to just event (for older rr versions)
                        match = re.match(r"\s*(\d+)\s+.*event\s+(\d+)", line)
                        if match:
                            checkpoints.append(
                                CheckpointInfo(
                                    id=int(match.group(1)),
                                    event=int(match.group(2)),
                                    tick=0,
                                )
                            )
                        elif line.strip() and not line.startswith("Num"):
                            # Log lines that don't match either pattern (but skip header)
                            logger.warning("Could not parse checkpoint line: %s", line)

        return checkpoints

    # -------------------------------------------------------------------------
    # Threads
    # -------------------------------------------------------------------------

    async def list_threads(self) -> list[ThreadDict]:
        """List all threads in the process.

        Returns:
            List of thread information dictionaries.
        """
        if self._gdb is None:
            return []

        current, threads = await self._gdb.thread_info()

        # Convert to dict format for backwards compatibility
        result: list[ThreadDict] = []
        for t in threads:
            thread_dict: ThreadDict = {
                "id": t.id,
                "name": t.name,
                "state": t.state,
            }
            if t.frame:
                thread_dict["frame"] = {
                    "func": t.frame.function,
                    "file": t.frame.file,
                    "line": t.frame.line,
                }
            else:
                thread_dict["frame"] = None
            result.append(thread_dict)

        return result

    async def select_thread(self, thread_id: int) -> bool:
        """Select a thread.

        Args:
            thread_id: The thread ID to select.

        Returns:
            True if successful, False otherwise.
        """
        if self._gdb is None:
            return False
        result = await self._gdb.thread_select(thread_id)
        return result is not None

    # -------------------------------------------------------------------------
    # Source code
    # -------------------------------------------------------------------------

    async def list_source_files(self) -> list[str]:
        """List all source files in the program.

        Returns:
            List of source file paths.
        """
        if self._gdb is None:
            return []
        return await self._gdb.file_list_exec_source_files()

    async def resolve_source_fullpath(self, filename: str | None) -> str | None:
        """Resolve a source filename to its full path.

        Args:
            filename: Relative or partial filename.

        Returns:
            Full path, or filename if cannot be resolved.
        """
        if self._gdb is None or filename is None:
            return filename
        return await self._gdb.file_resolve_fullpath(filename)

    async def get_source_lines(
        self, location: str | None = None, lines_before: int = 5, lines_after: int = 5
    ) -> SourceLinesDict:
        """Get source code around a location.

        Args:
            location: file:line or function name, or None for current location.
            lines_before: Number of context lines before.
            lines_after: Number of context lines after.

        Returns:
            Dictionary with file, start_line, lines array, and current_line.
        """
        if self._gdb is None:
            return SourceLinesDict(file=None, start_line=0, lines=[], current_line=None)

        # Determine target location
        if location is None:
            # Use current location
            curr_loc = await self.get_current_location()
            if curr_loc.file is None or curr_loc.line is None:
                return SourceLinesDict(
                    file=curr_loc.file,
                    start_line=0,
                    lines=[],
                    current_line=None,
                )
            filename = curr_loc.file
            center_line = curr_loc.line
        else:
            # Parse location (file:line or function)
            if ":" in location:
                parts = location.rsplit(":", 1)
                filename = parts[0]
                try:
                    center_line = int(parts[1])
                except ValueError:
                    return SourceLinesDict(
                        file=None,
                        start_line=0,
                        lines=[],
                        current_line=None,
                        error=f"Invalid line number in location: {location}",
                    )
            else:
                # It's a function name - resolve via GDB
                resolved = await self._gdb.resolve_function_location(location)
                if resolved is None:
                    return SourceLinesDict(
                        file=None,
                        start_line=0,
                        lines=[],
                        current_line=None,
                        error=f"Cannot resolve function: {location}",
                    )
                filename, center_line = resolved

        # Calculate line range
        start_line = max(1, center_line - lines_before)
        end_line = center_line + lines_after

        # Get source lines
        source_lines = await self._gdb.data_list_source_lines(filename, start_line, end_line)

        return SourceLinesDict(
            file=filename,
            start_line=start_line,
            lines=[
                SourceLineEntry(line_num=line_num, content=content)
                for line_num, content in source_lines
            ],
            current_line=center_line,
        )


class SessionManager:
    """Manages multiple rr replay sessions."""

    def __init__(self, max_sessions: int = DEFAULT_MAX_SESSIONS) -> None:
        """Initialize the session manager.

        Args:
            max_sessions: Maximum number of concurrent sessions allowed.
        """
        self._sessions: dict[str, Session] = {}
        self._max_sessions = max_sessions

    def list_sessions(self) -> list[Session]:
        """List all active sessions.

        Returns:
            List of active sessions.
        """
        return list(self._sessions.values())

    def get_session(self, session_id: str) -> Session:
        """Get a session by ID.

        Args:
            session_id: The session ID.

        Returns:
            The session.

        Raises:
            SessionNotFoundError: If the session doesn't exist.
        """
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFoundError(session_id)
        return session

    async def create_session(
        self,
        trace: str,
        pid: int | None = None,
        fork_pid: int | None = None,
    ) -> tuple[Session, Location]:
        """Create a new replay session.

        Args:
            trace: Path to the rr trace.
            pid: Process ID to debug (None for rr default). Requires exec().
            fork_pid: Start debug server when this PID is fork()d. For
                forked-without-exec child processes. Mutually exclusive with pid.

        Returns:
            Tuple of (session, initial_location).

        Raises:
            RrMcpError: If the maximum number of sessions is reached.
        """
        if len(self._sessions) >= self._max_sessions:
            raise RrMcpError(
                f"Maximum number of sessions ({self._max_sessions}) reached. "
                "Close an existing session before creating a new one."
            )

        session = Session(trace=trace, pid=pid, fork_pid=fork_pid)
        initial_location = await session.start()
        self._sessions[session.session_id] = session
        return (session, initial_location)

    async def close_session(self, session_id: str) -> None:
        """Close a session.

        Args:
            session_id: The session ID to close.

        Raises:
            SessionNotFoundError: If the session doesn't exist.
        """
        session = self.get_session(session_id)
        await session.close()
        del self._sessions[session_id]

    async def close_all(self) -> None:
        """Close all sessions."""
        for session in list(self._sessions.values()):
            await session.close()
        self._sessions.clear()
