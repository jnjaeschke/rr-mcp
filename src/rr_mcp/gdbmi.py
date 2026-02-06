"""GDB/MI protocol interface.

This module encapsulates all GDB/MI command formatting and response parsing,
providing a clean Python API that hides the protocol details.
"""

import asyncio
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict

from pygdbmi.gdbcontroller import GdbController

if TYPE_CHECKING:
    from typing import Any


# GDB/MI response record types
class GdbMiRecord(TypedDict, total=False):
    """A GDB/MI response record from pygdbmi."""

    type: str  # "result", "notify", "console", "log", "output", "target"
    message: str  # For result records: "done", "running", "connected", "error", "exit"
    payload: dict[str, "Any"]  # Varies by command
    token: int  # Optional token
    stream: str  # For console/log records


@dataclass
class FrameData:
    """Data about a stack frame from GDB."""

    level: int | None
    function: str | None
    file: str | None
    line: int | None
    address: str | None


@dataclass
class RegisterValue:
    """A CPU register and its value."""

    name: str
    value: str


@dataclass
class MemoryBlock:
    """A block of memory read from the target."""

    address: str
    contents: bytes


@dataclass
class BreakpointData:
    """Data about a breakpoint from GDB."""

    number: int | None
    type: str | None
    enabled: bool
    address: str | None
    file: str | None
    line: int | None
    function: str | None
    condition: str | None
    times: int
    watchpoint: bool = False


@dataclass
class ThreadData:
    """Data about a thread from GDB."""

    id: int
    name: str | None
    state: str | None
    frame: FrameData | None


@dataclass
class VariableData:
    """Data about a variable from GDB."""

    name: str
    value: str | None
    type: str | None


class GdbMi:
    """Low-level GDB/MI protocol interface.

    This class encapsulates all GDB/MI commands and their response parsing,
    providing typed Python methods instead of raw command strings.
    """

    def __init__(self, controller: GdbController) -> None:
        """Initialize with a GDB controller.

        Args:
            controller: The pygdbmi GdbController to use.
        """
        self._gdb = controller
        self._lock = asyncio.Lock()
        self._register_names: list[str] | None = None

    async def execute_raw(self, command: str, timeout_sec: int = 30) -> list[GdbMiRecord]:
        """Execute a raw GDB/MI command.

        This is the escape hatch for commands not covered by the typed API.

        Args:
            command: The GDB/MI command string.
            timeout_sec: Timeout in seconds.

        Returns:
            List of GDB/MI response records.
        """
        async with self._lock:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                lambda: self._gdb.write(command, timeout_sec=timeout_sec),
            )
            # pygdbmi returns list[dict[Any, Any]], we assert it matches our structure
            return result  # type: ignore[return-value]

    async def _wait_for_stop(self, timeout_sec: int = 30) -> list[GdbMiRecord]:
        """Wait for a stopped notification from GDB.

        This is needed for asynchronous operations (like reverse commands) that
        return "running" immediately but send "stopped" later.

        Args:
            timeout_sec: Timeout in seconds.

        Returns:
            List of GDB/MI response records up to and including the stop.
        """
        async with self._lock:
            loop = asyncio.get_running_loop()
            # Read responses until we get a stopped notification
            result = await loop.run_in_executor(
                None,
                lambda: self._gdb.get_gdb_response(
                    timeout_sec=timeout_sec, raise_error_on_timeout=True
                ),
            )
            return result  # type: ignore[return-value]

    async def close(self) -> None:
        """Close the GDB connection."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._gdb.exit)

    # -------------------------------------------------------------------------
    # Execution control
    # -------------------------------------------------------------------------

    async def exec_continue(self, reverse: bool = False) -> list[GdbMiRecord]:
        """Continue execution.

        Args:
            reverse: If True, continue backward.

        Returns:
            GDB/MI response records (caller should parse for stop event).
        """
        cmd = "-exec-continue"
        if reverse:
            cmd += " --reverse"
        return await self.execute_raw(cmd)

    async def exec_step(self, count: int = 1, reverse: bool = False) -> list[GdbMiRecord]:
        """Step by source lines (into functions).

        Args:
            count: Number of steps.
            reverse: If True, step backward.

        Returns:
            GDB/MI response records.
        """
        cmd = f"-exec-step {count}"
        if reverse:
            cmd = f"-exec-step --reverse {count}"
        return await self.execute_raw(cmd)

    async def exec_next(self, count: int = 1, reverse: bool = False) -> list[GdbMiRecord]:
        """Step by source lines (over functions).

        Args:
            count: Number of steps.
            reverse: If True, step backward.

        Returns:
            GDB/MI response records.
        """
        cmd = f"-exec-next {count}"
        if reverse:
            cmd = f"-exec-next --reverse {count}"
        return await self.execute_raw(cmd)

    async def exec_finish(self, reverse: bool = False) -> list[GdbMiRecord]:
        """Execute until current function returns.

        Args:
            reverse: If True, go backward to function entry.

        Returns:
            GDB/MI response records.
        """
        cmd = "-exec-finish"
        if reverse:
            cmd += " --reverse"
        return await self.execute_raw(cmd)

    async def exec_step_instruction(
        self, count: int = 1, reverse: bool = False
    ) -> list[GdbMiRecord]:
        """Step by machine instructions (into calls).

        Args:
            count: Number of instructions.
            reverse: If True, step backward.

        Returns:
            GDB/MI response records.
        """
        cmd = f"-exec-step-instruction {count}"
        if reverse:
            cmd = f"-exec-step-instruction --reverse {count}"
        return await self.execute_raw(cmd)

    async def exec_next_instruction(
        self, count: int = 1, reverse: bool = False
    ) -> list[GdbMiRecord]:
        """Step by machine instructions (over calls).

        Args:
            count: Number of instructions.
            reverse: If True, step backward.

        Returns:
            GDB/MI response records.
        """
        cmd = f"-exec-next-instruction {count}"
        if reverse:
            cmd = f"-exec-next-instruction --reverse {count}"
        return await self.execute_raw(cmd)

    # -------------------------------------------------------------------------
    # rr-specific commands
    # -------------------------------------------------------------------------

    async def rr_when(self) -> tuple[int, int]:
        """Get current rr event and tick.

        Returns:
            Tuple of (event, tick).
        """
        response = await self.execute_raw('-interpreter-exec console "when"')

        for record in response:
            if record.get("type") == "console":
                payload = record.get("payload", "")
                if not isinstance(payload, str):
                    continue
                output = payload
                event_match = re.search(r"event:\s*(\d+)", output, re.IGNORECASE)
                if event_match:
                    event = int(event_match.group(1))
                    tick_match = re.search(r"tick:\s*(\d+)", output, re.IGNORECASE)
                    tick = int(tick_match.group(1)) if tick_match else 0
                    return (event, tick)

        return (0, 0)

    async def rr_run_to_event(self, event: int) -> list[GdbMiRecord]:
        """Run to a specific rr event number.

        Args:
            event: Target event number.

        Returns:
            GDB/MI response records.

        Note:
            Uses console interpreter because 'run' is an rr-specific command,
            not a standard GDB command. There is no MI equivalent.
        """
        return await self.execute_raw(f'-interpreter-exec console "run {event}"')

    async def rr_checkpoint_create(self) -> int | None:
        """Create an rr checkpoint at the current position.

        Returns:
            Checkpoint ID, or None if parsing failed.
        """
        response = await self.execute_raw('-interpreter-exec console "checkpoint"')

        for record in response:
            if record.get("type") == "console":
                output = str(record.get("payload", ""))
                match = re.search(r"checkpoint\s+(\d+)", output, re.IGNORECASE)
                if match:
                    return int(match.group(1))
        return None

    async def rr_checkpoint_restore(self, checkpoint_id: int) -> bool:
        """Restore an rr checkpoint.

        Args:
            checkpoint_id: The checkpoint to restore.

        Returns:
            True if command was sent (doesn't guarantee success).
        """
        response = await self.execute_raw(f'-interpreter-exec console "restart {checkpoint_id}"')
        return len(response) > 0

    async def rr_checkpoint_delete(self, checkpoint_id: int) -> bool:
        """Delete an rr checkpoint.

        Args:
            checkpoint_id: The checkpoint to delete.

        Returns:
            True if command was sent.
        """
        response = await self.execute_raw(
            f'-interpreter-exec console "delete checkpoint {checkpoint_id}"'
        )
        return len(response) > 0

    async def rr_checkpoint_list(self) -> list[GdbMiRecord]:
        """List all rr checkpoints.

        Returns:
            Raw GDB/MI response records.
        """
        return await self.execute_raw('-interpreter-exec console "info checkpoints"')

    # -------------------------------------------------------------------------
    # Breakpoints and watchpoints
    # -------------------------------------------------------------------------

    async def break_insert(
        self,
        location: str,
        temporary: bool = False,
        condition: str | None = None,
    ) -> BreakpointData | None:
        """Insert a breakpoint.

        Args:
            location: Where to break (function, file:line, or *address).
            temporary: If True, delete after first hit.
            condition: Optional condition expression.

        Returns:
            Breakpoint data, or None if failed.
        """
        cmd = "-break-insert"
        if temporary:
            cmd += " -t"
        if condition:
            cmd += f' -c "{condition}"'
        cmd += f" {location}"

        response = await self.execute_raw(cmd)

        for record in response:
            if record.get("type") == "result" and record.get("message") == "done":
                bkpt = record.get("payload", {}).get("bkpt", {})
                return BreakpointData(
                    number=_safe_int(bkpt.get("number")),
                    type=bkpt.get("type"),
                    enabled=bkpt.get("enabled") == "y",
                    address=bkpt.get("addr"),
                    file=bkpt.get("file"),
                    line=_safe_int(bkpt.get("line")),
                    function=bkpt.get("func"),
                    condition=bkpt.get("cond"),
                    times=_safe_int(bkpt.get("times")) or 0,
                )
        return None

    async def break_delete(self, breakpoint_num: int) -> bool:
        """Delete a breakpoint.

        Args:
            breakpoint_num: Breakpoint number to delete.

        Returns:
            True if successful.
        """
        response = await self.execute_raw(f"-break-delete {breakpoint_num}")
        return any(r.get("type") == "result" and r.get("message") == "done" for r in response)

    async def break_enable(self, breakpoint_num: int) -> bool:
        """Enable a breakpoint.

        Args:
            breakpoint_num: Breakpoint number to enable.

        Returns:
            True if successful.
        """
        response = await self.execute_raw(f"-break-enable {breakpoint_num}")
        return any(r.get("type") == "result" and r.get("message") == "done" for r in response)

    async def break_disable(self, breakpoint_num: int) -> bool:
        """Disable a breakpoint.

        Args:
            breakpoint_num: Breakpoint number to disable.

        Returns:
            True if successful.
        """
        response = await self.execute_raw(f"-break-disable {breakpoint_num}")
        return any(r.get("type") == "result" and r.get("message") == "done" for r in response)

    async def break_list(self) -> list[BreakpointData]:
        """List all breakpoints.

        Returns:
            List of breakpoint data.
        """
        response = await self.execute_raw("-break-list")
        breakpoints = []

        for record in response:
            if record.get("type") == "result" and record.get("message") == "done":
                table = record.get("payload", {}).get("BreakpointTable", {})
                body = table.get("body", [])
                for bkpt in body:
                    bp_type = bkpt.get("type", "")
                    # Check if it's a watchpoint based on type
                    is_watchpoint = "watchpoint" in bp_type.lower() if bp_type else False
                    breakpoints.append(
                        BreakpointData(
                            number=_safe_int(bkpt.get("number")),
                            type=bp_type,
                            enabled=bkpt.get("enabled") == "y",
                            address=bkpt.get("addr"),
                            file=bkpt.get("file"),
                            line=_safe_int(bkpt.get("line")),
                            function=bkpt.get("func"),
                            condition=bkpt.get("cond"),
                            times=_safe_int(bkpt.get("times")) or 0,
                            watchpoint=is_watchpoint,
                        )
                    )

        return breakpoints

    async def break_watch(
        self, expression: str, access_type: str = "write"
    ) -> BreakpointData | None:
        """Set a watchpoint.

        Args:
            expression: Expression to watch.
            access_type: "write", "read", or "access".

        Returns:
            Breakpoint data with watchpoint=True, or None if failed.

        Raises:
            ValueError: If access_type is invalid.
        """
        type_map = {"write": "", "read": "-r", "access": "-a"}
        if access_type not in type_map:
            raise ValueError(
                f"Invalid access_type: {access_type}. Must be one of: {', '.join(type_map.keys())}"
            )

        type_flag = type_map[access_type]
        cmd = f"-break-watch {type_flag} {expression}".strip()

        response = await self.execute_raw(cmd)

        for record in response:
            if record.get("type") == "result" and record.get("message") == "done":
                payload = record.get("payload", {})
                # Try different watchpoint key names that GDB uses
                wpt = (
                    payload.get("wpt")
                    or payload.get("hw-wpt")  # hardware write watchpoint
                    or payload.get("hw-rwpt")  # hardware read watchpoint
                    or payload.get("hw-awpt")  # hardware access watchpoint
                    or payload.get("bkpt")
                    or {}
                )

                return BreakpointData(
                    number=_safe_int(wpt.get("number")),
                    type=wpt.get("type"),
                    enabled=wpt.get("enabled") == "y",
                    address=wpt.get("addr"),
                    file=wpt.get("file"),
                    line=_safe_int(wpt.get("line")),
                    function=wpt.get("func"),
                    condition=wpt.get("cond"),
                    times=_safe_int(wpt.get("times")) or 0,
                    watchpoint=True,
                )
        return None

    # -------------------------------------------------------------------------
    # Stack inspection
    # -------------------------------------------------------------------------

    async def stack_info_frame(self) -> FrameData | None:
        """Get info about the current stack frame.

        Returns:
            Frame data, or None if not available.
        """
        response = await self.execute_raw("-stack-info-frame")

        for record in response:
            if record.get("type") == "result" and record.get("message") == "done":
                frame = record.get("payload", {}).get("frame", {})
                return FrameData(
                    level=_safe_int(frame.get("level")),
                    function=frame.get("func"),
                    file=frame.get("file"),
                    line=_safe_int(frame.get("line")),
                    address=frame.get("addr"),
                )
        return None

    async def stack_list_frames(self, start: int = 0, end: int | None = None) -> list[FrameData]:
        """List stack frames.

        Args:
            start: First frame index (0 = innermost).
            end: Last frame index (inclusive), or None for all.

        Returns:
            List of frame data.
        """
        cmd = f"-stack-list-frames {start} {end}" if end is not None else "-stack-list-frames"

        response = await self.execute_raw(cmd)
        frames = []

        for record in response:
            if record.get("type") == "result" and record.get("message") == "done":
                stack = record.get("payload", {}).get("stack", [])
                for frame_data in stack:
                    frame = frame_data.get("frame", frame_data)
                    frames.append(
                        FrameData(
                            level=_safe_int(frame.get("level")),
                            function=frame.get("func"),
                            file=frame.get("file"),
                            line=_safe_int(frame.get("line")),
                            address=frame.get("addr"),
                        )
                    )

        return frames

    async def stack_select_frame(self, frame_num: int) -> bool:
        """Select a stack frame.

        Args:
            frame_num: Frame number (0 = innermost).

        Returns:
            True if successful.
        """
        response = await self.execute_raw(f"-stack-select-frame {frame_num}")
        return any(r.get("type") == "result" and r.get("message") == "done" for r in response)

    async def stack_list_variables(self, print_values: str = "simple-values") -> list[VariableData]:
        """List local variables in current frame.

        Args:
            print_values: How to print values ("no-values", "all-values", "simple-values").

        Returns:
            List of variable data.
        """
        response = await self.execute_raw(f"-stack-list-variables --{print_values}")
        variables = []

        for record in response:
            if record.get("type") == "result" and record.get("message") == "done":
                var_list = record.get("payload", {}).get("variables", [])
                for var in var_list:
                    variables.append(
                        VariableData(
                            name=var.get("name", ""),
                            value=var.get("value"),
                            type=var.get("type"),
                        )
                    )

        return variables

    async def stack_list_arguments(
        self, print_values: int = 1, frame: int | None = None
    ) -> list[list[VariableData]]:
        """List function arguments for stack frames.

        Args:
            print_values: 0=no values, 1=all values, 2=simple values.
            frame: Specific frame, or None for all frames.

        Returns:
            List of argument lists, one per frame.
        """
        cmd = f"-stack-list-arguments {print_values}"
        if frame is not None:
            cmd += f" {frame} {frame}"

        response = await self.execute_raw(cmd)
        result = []

        for record in response:
            if record.get("type") == "result" and record.get("message") == "done":
                stack_args = record.get("payload", {}).get("stack-args", [])
                for frame_args in stack_args:
                    args = frame_args.get("args", [])
                    frame_vars = []
                    for arg in args:
                        frame_vars.append(
                            VariableData(
                                name=arg.get("name", ""),
                                value=arg.get("value"),
                                type=arg.get("type"),
                            )
                        )
                    result.append(frame_vars)

        return result

    # -------------------------------------------------------------------------
    # Data inspection
    # -------------------------------------------------------------------------

    async def data_evaluate_expression(self, expression: str) -> str | None:
        """Evaluate an expression.

        Args:
            expression: Expression to evaluate.

        Returns:
            String value, or None if failed.
        """
        response = await self.execute_raw(f'-data-evaluate-expression "{expression}"')

        for record in response:
            if record.get("type") == "result" and record.get("message") == "done":
                return record.get("payload", {}).get("value")
        return None

    async def data_read_memory_bytes(self, address: str, size: int) -> MemoryBlock | None:
        """Read memory bytes.

        Args:
            address: Memory address (hex string or expression).
            size: Number of bytes to read.

        Returns:
            Memory block, or None if failed.
        """
        response = await self.execute_raw(f"-data-read-memory-bytes {address} {size}")

        for record in response:
            if record.get("type") == "result" and record.get("message") == "done":
                memory = record.get("payload", {}).get("memory", [])
                if memory:
                    block = memory[0]
                    contents_hex = block.get("contents", "")
                    return MemoryBlock(
                        address=block.get("begin", address),
                        contents=bytes.fromhex(contents_hex) if contents_hex else b"",
                    )
        return None

    async def data_examine_memory(
        self, address: str, count: int = 16, format_char: str = "x", unit_size: str = "w"
    ) -> list[tuple[str, str]]:
        """Examine memory with formatting (like GDB's x command).

        Args:
            address: Memory address or expression.
            count: Number of units to display.
            format_char: Format - x(hex), d(decimal), s(string), i(instruction), etc.
            unit_size: Unit size - b(byte), h(halfword), w(word), g(giant/8bytes).

        Returns:
            List of (address, value) tuples.
        """
        # Map unit_size to GDB's size specifiers
        size_map = {"b": "b", "h": "h", "w": "w", "g": "g"}
        size = size_map.get(unit_size, "w")

        # Use console x command (no direct MI equivalent)
        cmd = f'-interpreter-exec console "x/{count}{format_char}{size} {address}"'
        response = await self.execute_raw(cmd)

        results: list[tuple[str, str]] = []
        for record in response:
            if record.get("type") == "console":
                payload = record.get("payload")
                if not isinstance(payload, str):
                    continue
                output = payload
                # Parse output like "0x12345: 0xdeadbeef  0xcafebabe"
                for line in output.splitlines():
                    # Match address at start
                    match = re.match(r"(0x[0-9a-fA-F]+):\s+(.*)", line)
                    if match:
                        addr = match.group(1)
                        values_str = match.group(2)
                        # Split values
                        values = values_str.split()
                        for val in values:
                            if val:  # Skip empty
                                results.append((addr, val))
                                # Update address for next value
                                try:
                                    addr_int = int(addr, 16)
                                    unit_bytes = {"b": 1, "h": 2, "w": 4, "g": 8}.get(size, 4)
                                    addr_int += unit_bytes
                                    addr = f"0x{addr_int:x}"
                                except ValueError:
                                    pass

        return results

    async def data_list_register_names(self) -> list[str]:
        """Get the names of all registers.

        Returns:
            List of register names (index corresponds to register number).
        """
        if self._register_names is not None:
            return self._register_names

        response = await self.execute_raw("-data-list-register-names")

        for record in response:
            if record.get("type") == "result" and record.get("message") == "done":
                payload = record.get("payload", {})
                if isinstance(payload, dict):
                    names_raw = payload.get("register-names", [])
                    if isinstance(names_raw, list):
                        names = [str(n) if n else "" for n in names_raw]
                        self._register_names = names
                        return names

        return []

    async def data_list_register_values(self, format_char: str = "x") -> list[RegisterValue]:
        """Get values of all registers.

        Args:
            format_char: Format for values ("x"=hex, "d"=decimal, etc.).

        Returns:
            List of register values with names.
        """
        # Get names first (cached after first call)
        names = await self.data_list_register_names()

        response = await self.execute_raw(f"-data-list-register-values {format_char}")
        registers = []

        for record in response:
            if record.get("type") == "result" and record.get("message") == "done":
                values = record.get("payload", {}).get("register-values", [])
                for reg in values:
                    number = reg.get("number")
                    value = reg.get("value")
                    if number is not None and value is not None:
                        idx = int(number)
                        name = names[idx] if idx < len(names) else f"r{idx}"
                        if name:  # Skip empty register names
                            registers.append(RegisterValue(name=name, value=value))

        return registers

    # -------------------------------------------------------------------------
    # Thread operations
    # -------------------------------------------------------------------------

    async def thread_info(self) -> tuple[int | None, list[ThreadData]]:
        """Get thread information.

        Returns:
            Tuple of (current_thread_id, list of threads).
        """
        response = await self.execute_raw("-thread-info")
        threads = []
        current_thread = None

        for record in response:
            if record.get("type") == "result" and record.get("message") == "done":
                payload = record.get("payload", {})
                current_thread = _safe_int(payload.get("current-thread-id"))

                for thread in payload.get("threads", []):
                    frame_data = thread.get("frame", {})
                    frame = None
                    if frame_data:
                        frame = FrameData(
                            level=_safe_int(frame_data.get("level")),
                            function=frame_data.get("func"),
                            file=frame_data.get("file"),
                            line=_safe_int(frame_data.get("line")),
                            address=frame_data.get("addr"),
                        )

                    threads.append(
                        ThreadData(
                            id=_safe_int(thread.get("id")) or 0,
                            name=thread.get("name"),
                            state=thread.get("state"),
                            frame=frame,
                        )
                    )

        return (current_thread, threads)

    async def thread_select(self, thread_id: int) -> FrameData | None:
        """Select a thread.

        Args:
            thread_id: Thread ID to select.

        Returns:
            Frame data for the selected thread, or None.
        """
        response = await self.execute_raw(f"-thread-select {thread_id}")

        for record in response:
            if record.get("type") == "result" and record.get("message") == "done":
                frame = record.get("payload", {}).get("frame", {})
                if frame:
                    return FrameData(
                        level=_safe_int(frame.get("level")),
                        function=frame.get("func"),
                        file=frame.get("file"),
                        line=_safe_int(frame.get("line")),
                        address=frame.get("addr"),
                    )
        return None

    # -------------------------------------------------------------------------
    # Source files
    # -------------------------------------------------------------------------

    async def file_list_exec_source_files(self) -> list[str]:
        """List all source files.

        Returns:
            List of full paths to source files.
        """
        response = await self.execute_raw("-file-list-exec-source-files")
        files = []

        for record in response:
            if record.get("type") == "result" and record.get("message") == "done":
                file_list = record.get("payload", {}).get("files", [])
                for file_info in file_list:
                    if isinstance(file_info, dict):
                        fullname = file_info.get("fullname")
                        if fullname:
                            files.append(fullname)

        return files

    async def data_list_source_lines(
        self, filename: str, start_line: int, end_line: int
    ) -> list[tuple[int, str]]:
        """List source code lines from a file.

        Args:
            filename: Source file path.
            start_line: First line to read.
            end_line: Last line to read.

        Returns:
            List of (line_number, content) tuples.
        """
        # GDB doesn't have a direct MI command for this, use console
        response = await self.execute_raw(
            f'-interpreter-exec console "list {filename}:{start_line},{end_line}"'
        )

        lines = []
        for record in response:
            if record.get("type") == "console":
                payload = record.get("payload", "")
                if not isinstance(payload, str):
                    continue
                output = payload
                # Parse output like "123\tcode here"
                for line in output.splitlines():
                    # Look for line numbers at start
                    match = re.match(r"(\d+)\s+(.*)", line)
                    if match:
                        line_num = int(match.group(1))
                        content = match.group(2)
                        lines.append((line_num, content))

        return lines

    async def file_resolve_fullpath(self, filename: str | None) -> str | None:
        """Resolve a filename to its full path.

        Args:
            filename: Relative or partial filename.

        Returns:
            Full absolute path, or None if cannot be resolved.
        """
        if filename is None:
            return None

        # Try to find in source file list
        files = await self.file_list_exec_source_files()

        # Exact match
        for f in files:
            if f == filename:
                return f

        # Suffix match (e.g., "foo.cpp" matches "/path/to/foo.cpp")
        for f in files:
            if f.endswith("/" + filename) or f.endswith(filename):
                return f

        # Fallback: return as-is
        return filename


def _safe_int(value: object) -> int | None:
    """Safely convert a value to int, returning None if not possible."""
    if value is None:
        return None
    try:
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            return int(value)
        # Try to convert other types
        return int(str(value))
    except (ValueError, TypeError):
        return None
