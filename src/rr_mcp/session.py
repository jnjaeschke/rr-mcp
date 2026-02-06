"""Session management for rr replay sessions."""

import asyncio
import uuid

from pygdbmi.gdbcontroller import GdbController

from rr_mcp.errors import GdbError, SessionNotFoundError
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


class Session:
    """An rr replay session.

    Each session wraps a single rr replay process communicating via GDB/MI.
    """

    def __init__(self, trace: str, pid: int | None = None) -> None:
        """Initialize a session.

        Args:
            trace: Path to the rr trace.
            pid: Process ID to debug (None for rr default).
        """
        self.session_id: str = str(uuid.uuid4())[:8]
        self.trace: str = trace
        self.pid: int | None = pid
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

        command.append(self.trace)

        # GdbController is synchronous, run in executor
        loop = asyncio.get_running_loop()
        controller = await loop.run_in_executor(
            None,
            lambda: GdbController(command=command),
        )

        self._gdb = GdbMi(controller)

        # Flush initial GDB output by executing a harmless command
        await self._read_until_ready()

        # Get initial location
        return await self.get_current_location()

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

    async def _read_until_ready(self) -> list[GdbMiRecord]:
        """Flush GDB's initial output by executing a harmless command."""
        if self._gdb is None:
            return []

        # Execute a harmless command to trigger GDB initialization
        return await self._gdb.execute_raw("-gdb-show version", timeout_sec=10)

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
            address=frame.address or "0x0" if frame else "0x0",
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

    async def _parse_stop_result(self, records: list[GdbMiRecord]) -> StopResult | None:
        """Parse a stop result from GDB/MI records and populate event/tick.

        Args:
            records: List of GDB/MI response records.

        Returns:
            StopResult if a stop was found, None otherwise.
        """
        # Look for stop notifications (stopped, thread-exited, exited)
        for record in records:
            if record.get("type") == "notify" and record.get("message") in (
                "stopped",
                "thread-exited",
                "exited",
            ):
                payload = record.get("payload", {})

                # Get current position
                event, tick = await self.get_current_position()

                # Extract signal info if present
                signal_info = None
                if payload.get("signal-name"):
                    signal_info = SignalInfo(
                        name=payload.get("signal-name", ""),
                        meaning=payload.get("signal-meaning", ""),
                    )

                frame = payload.get("frame", {})

                # Get reason - if not present, infer from context
                reason = payload.get("reason")
                if not reason:
                    # For reverse operations, GDB sometimes doesn't include reason
                    # Default to "end-stepping-range" which is most common
                    reason = "end-stepping-range"

                return StopResult(
                    reason=reason,
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
                payload = record.get("payload", {})
                frame = payload.get("frame", {})

                # Only create StopResult if we have frame information
                if frame:
                    event, tick = await self.get_current_position()
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
        """Step forward by source lines (into functions).

        Args:
            count: Number of steps.

        Returns:
            StopResult if execution stopped, None otherwise.
        """
        if self._gdb is None:
            return None
        self.state = SessionState.STEPPING
        response = await self._gdb.exec_step(count=count)
        result = await self._parse_stop_result(response)
        self.state = SessionState.PAUSED
        return result

    async def reverse_step(self, count: int = 1) -> StopResult | None:
        """Step backward by source lines (into functions).

        Args:
            count: Number of steps.

        Returns:
            StopResult if execution stopped, None otherwise.
        """
        if self._gdb is None:
            return None
        self.state = SessionState.STEPPING
        response = await self._gdb.exec_step(count=count, reverse=True)
        # Reverse operations are async - wait for stopped notification if not already present
        if not self._has_stopped_notification(response):
            stop_response = await self._gdb._wait_for_stop()
            response.extend(stop_response)
        result = await self._parse_stop_result(response)
        self.state = SessionState.PAUSED
        return result

    async def next(self, count: int = 1) -> StopResult | None:
        """Step forward by source lines (over functions).

        Args:
            count: Number of steps.

        Returns:
            StopResult if execution stopped, None otherwise.
        """
        if self._gdb is None:
            return None
        self.state = SessionState.STEPPING
        response = await self._gdb.exec_next(count=count)
        result = await self._parse_stop_result(response)
        self.state = SessionState.PAUSED
        return result

    async def reverse_next(self, count: int = 1) -> StopResult | None:
        """Step backward by source lines (over functions).

        Args:
            count: Number of steps.

        Returns:
            StopResult if execution stopped, None otherwise.
        """
        if self._gdb is None:
            return None
        self.state = SessionState.STEPPING
        response = await self._gdb.exec_next(count=count, reverse=True)
        # Reverse operations are async - wait for stopped notification if not already present
        if not self._has_stopped_notification(response):
            stop_response = await self._gdb._wait_for_stop()
            response.extend(stop_response)
        result = await self._parse_stop_result(response)
        self.state = SessionState.PAUSED
        return result

    async def continue_execution(self) -> StopResult | None:
        """Continue execution forward until breakpoint or end.

        Returns:
            StopResult if execution stopped, None otherwise.
        """
        if self._gdb is None:
            return None
        self.state = SessionState.RUNNING
        response = await self._gdb.exec_continue()
        result = await self._parse_stop_result(response)
        self.state = SessionState.PAUSED
        return result

    async def reverse_continue(self) -> StopResult | None:
        """Continue execution backward until breakpoint or beginning.

        Returns:
            StopResult if execution stopped, None otherwise.
        """
        if self._gdb is None:
            return None
        self.state = SessionState.RUNNING
        response = await self._gdb.exec_continue(reverse=True)
        # Reverse operations are async - wait for stopped notification if not already present
        if not self._has_stopped_notification(response):
            stop_response = await self._gdb._wait_for_stop()
            response.extend(stop_response)
        result = await self._parse_stop_result(response)
        self.state = SessionState.PAUSED
        return result

    async def finish(self) -> StopResult | None:
        """Finish executing current function (step out).

        Returns:
            StopResult if execution stopped, None otherwise.
        """
        if self._gdb is None:
            return None
        self.state = SessionState.RUNNING
        response = await self._gdb.exec_finish()
        result = await self._parse_stop_result(response)
        self.state = SessionState.PAUSED
        return result

    async def reverse_finish(self) -> StopResult | None:
        """Reverse to the start of current function (reverse step out).

        Returns:
            StopResult if execution stopped, None otherwise.
        """
        if self._gdb is None:
            return None
        self.state = SessionState.RUNNING
        response = await self._gdb.exec_finish(reverse=True)
        # Reverse operations are async - wait for stopped notification if not already present
        if not self._has_stopped_notification(response):
            stop_response = await self._gdb._wait_for_stop()
            response.extend(stop_response)
        result = await self._parse_stop_result(response)
        self.state = SessionState.PAUSED
        return result

    async def step_instruction(self, count: int = 1) -> StopResult | None:
        """Step forward by machine instructions (into calls).

        Args:
            count: Number of instructions.

        Returns:
            StopResult if execution stopped, None otherwise.
        """
        if self._gdb is None:
            return None
        self.state = SessionState.STEPPING
        response = await self._gdb.exec_step_instruction(count=count)
        result = await self._parse_stop_result(response)
        self.state = SessionState.PAUSED
        return result

    async def reverse_step_instruction(self, count: int = 1) -> StopResult | None:
        """Step backward by machine instructions (into calls).

        Args:
            count: Number of instructions.

        Returns:
            StopResult if execution stopped, None otherwise.
        """
        if self._gdb is None:
            return None
        self.state = SessionState.STEPPING
        response = await self._gdb.exec_step_instruction(count=count, reverse=True)
        # Reverse operations are async - wait for stopped notification if not already present
        if not self._has_stopped_notification(response):
            stop_response = await self._gdb._wait_for_stop()
            response.extend(stop_response)
        result = await self._parse_stop_result(response)
        self.state = SessionState.PAUSED
        return result

    async def next_instruction(self, count: int = 1) -> StopResult | None:
        """Step forward by machine instructions (over calls).

        Args:
            count: Number of instructions.

        Returns:
            StopResult if execution stopped, None otherwise.
        """
        if self._gdb is None:
            return None
        self.state = SessionState.STEPPING
        response = await self._gdb.exec_next_instruction(count=count)
        result = await self._parse_stop_result(response)
        self.state = SessionState.PAUSED
        return result

    async def reverse_next_instruction(self, count: int = 1) -> StopResult | None:
        """Step backward by machine instructions (over calls).

        Args:
            count: Number of instructions.

        Returns:
            StopResult if execution stopped, None otherwise.
        """
        if self._gdb is None:
            return None
        self.state = SessionState.STEPPING
        response = await self._gdb.exec_next_instruction(count=count, reverse=True)
        # Reverse operations are async - wait for stopped notification if not already present
        if not self._has_stopped_notification(response):
            stop_response = await self._gdb._wait_for_stop()
            response.extend(stop_response)
        result = await self._parse_stop_result(response)
        self.state = SessionState.PAUSED
        return result

    async def run_to_event(self, event: int) -> StopResult | None:
        """Run to a specific event number in the trace.

        Args:
            event: The target event number.

        Returns:
            StopResult when stopped at or near the target event.
        """
        if self._gdb is None:
            return None
        self.state = SessionState.RUNNING
        response = await self._gdb.rr_run_to_event(event)
        result = await self._parse_stop_result(response)
        self.state = SessionState.PAUSED
        return result

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
    # Stack navigation
    # -------------------------------------------------------------------------

    async def get_backtrace(
        self, max_depth: int | None = None, full: bool = False
    ) -> list[BacktraceFrameDict]:
        """Get the call stack backtrace.

        Args:
            max_depth: Maximum number of frames to return (None for all).
            full: If True, include local variables for each frame.

        Returns:
            List of stack frame dictionaries.
        """
        if self._gdb is None:
            return []

        end = max_depth - 1 if max_depth else None
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

    async def read_registers(self) -> dict[str, str]:
        """Read all CPU registers.

        Returns:
            Dictionary mapping register names to values (as hex strings).
        """
        if self._gdb is None:
            return {}

        registers = await self._gdb.data_list_register_values()
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
                for line in output.splitlines():
                    # Try to match with tick first
                    import re

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
                # It's a function name - need to resolve it
                # For now, use current location as fallback
                curr_loc = await self.get_current_location()
                if curr_loc.file is None or curr_loc.line is None:
                    return SourceLinesDict(
                        file=None,
                        start_line=0,
                        lines=[],
                        current_line=None,
                        error=f"Cannot resolve location: {location}",
                    )
                filename = curr_loc.file
                center_line = curr_loc.line

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

    def __init__(self) -> None:
        """Initialize the session manager."""
        self._sessions: dict[str, Session] = {}

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
    ) -> tuple[Session, Location]:
        """Create a new replay session.

        Args:
            trace: Path to the rr trace.
            pid: Process ID to debug (None for rr default).

        Returns:
            Tuple of (session, initial_location).
        """
        session = Session(trace=trace, pid=pid)
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
