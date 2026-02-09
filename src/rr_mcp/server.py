"""MCP server for rr debugging."""

import asyncio
import atexit
import json
import logging
import signal
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Resource, TextContent, Tool
from pydantic import AnyUrl

from rr_mcp.errors import GdbError, RrMcpError
from rr_mcp.gdbmi import _mi_escape
from rr_mcp.models import (
    Location,
    LocationDict,
    StopResult,
)
from rr_mcp.session import SessionManager
from rr_mcp.trace import (
    get_trace_info,
    get_trace_processes,
    list_traces,
    resolve_trace_path,
)

logger = logging.getLogger(__name__)

# Debugging guide served as an MCP resource. Agents can read rr://guide on demand.
_DEBUGGING_GUIDE = """\
# rr Debugging Guide

## What is rr?
rr is a deterministic record-and-replay debugger. It records program execution once, \
then allows unlimited replays with full reverse execution. Every replay is identical — \
same memory addresses, same timing, same thread schedules. This means bugs reproduce 100% \
of the time.

## Key Concepts

**Events**: Monotonically increasing milestones in execution (syscalls, signals, context \
switches). Use `when` to get the current event, `run_to_event` to jump to a known position. \
Events are global across all processes in a trace — use them to correlate positions across sessions.

**Sessions**: Each session is an independent rr replay with its own GDB process. You must \
create a session with `session_create` before using any debugging commands. Multiple sessions \
can run concurrently on different processes or different points in the same trace.

**Checkpoints**: Snapshots of program state you can instantly restore. Create them before \
exploring unknown code so you can quickly backtrack. Much faster than replaying from the start.

## Debugging Workflows

### Crash debugging
1. `session_create` → start session (it pauses at the beginning)
2. `continue` → run until crash (signal like SIGSEGV)
3. `backtrace` → see call stack at crash point
4. `locals` / `args` / `print` → inspect variables in each frame
5. `frame_select` → move up/down the stack to examine callers

### Finding data corruption (rr's superpower)
1. `continue` → run forward past the point where data is already corrupted
2. `print "my_var"` → confirm the bad value
3. `watchpoint_set expression="my_var" access_type="write"` → break when it changes
4. `reverse_continue` → run BACKWARD to find who last wrote to it
5. `backtrace` → see the code that corrupted it
6. Repeat: set earlier watchpoints/breakpoints, reverse_continue to trace the chain

### Finding root cause via reverse execution
1. `continue` → run to symptom (crash, assertion, wrong output)
2. `breakpoint_set location="suspicious_function"` → mark where to investigate
3. `reverse_continue` → run backward to the last call to that function
4. `locals` / `args` → examine what data was passed
5. Repeat: set breakpoints further back, reverse_continue

### Debugging C++ exceptions
1. `catch event="throw"` → break on all C++ throws
2. `continue` → run until exception is thrown
3. `backtrace` → see where exception originated
4. `reverse_step` → trace back to see what led to the throw

### Tracing I/O or syscalls
1. `catch event="syscall" filter="write"` → break on write syscalls
2. `continue` → run until write happens
3. `backtrace` → see what code triggered the I/O

### Suppressing noisy signals
1. `handle_signal signal="SIGPIPE" stop=false pass_through=false` → suppress SIGPIPE
2. Now `continue` won't stop on SIGPIPE

## Tool Selection

**step vs next**: `step` enters function calls, `next` steps over them. Use `step` when you \
need to understand a function's internals; use `next` to stay in the current function.

**step vs stepi**: `step` moves one source line, `stepi` moves one machine instruction. \
Use `stepi` only for assembly-level analysis or optimized code where source lines don't map cleanly.

**continue vs run_to_event**: `continue` runs until a breakpoint/signal/end. `run_to_event` \
jumps to a specific event number. Use `run_to_event` when you know the exact event you want.

**breakpoint vs watchpoint**: Breakpoints stop at code locations. Watchpoints stop when data \
changes. Use breakpoints when you know WHERE to look; watchpoints when you don't know WHAT CODE \
is modifying a variable.

**checkpoint vs reverse execution**: Both let you "go back", but differently. Checkpoints are \
instant snapshots you can restore to. Reverse execution rewinds step-by-step \
(respecting breakpoints and watchpoints). Create checkpoints as save points; \
use reverse execution for investigation.

## Common Pitfalls

1. **Watchpoints need scope**: Set a breakpoint first, continue to where the variable exists, \
THEN set the watchpoint.

2. **Child processes need exec()**: In multi-process traces, only processes that called exec() \
can be debugged. Use `trace_processes` to check which PIDs are valid.

3. **Close sessions when done**: Each session runs a GDB process. Close them with `session_close` \
to free resources.

4. **Use checkpoints liberally**: Before any exploratory debugging, create a checkpoint. \
It's cheap and saves you from replaying from scratch.
"""


# Global session manager
_session_manager: SessionManager | None = None


def get_session_manager() -> SessionManager:
    """Get or create the global session manager."""
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager()
    return _session_manager


def _get_version() -> str:
    """Get the rr-mcp package version, falling back to 'dev' if not installed."""
    try:
        return pkg_version("rr-mcp")
    except PackageNotFoundError:
        return "dev"


# Create the MCP server
server = Server("rr-mcp", version=_get_version())


@server.list_resources()  # type: ignore[no-untyped-call, untyped-decorator]
async def list_resources() -> list[Resource]:
    """List available resources for dynamic state."""
    resources = [
        Resource(
            uri=AnyUrl("rr://traces"),
            name="Available rr traces",
            description="Lists all rr recordings available on the system",
            mimeType="application/json",
        ),
        Resource(
            uri=AnyUrl("rr://guide"),
            name="rr debugging guide",
            description=(
                "Debugging guide with workflows, tool selection advice, and common pitfalls. "
                "Read this first if you are unfamiliar with rr."
            ),
            mimeType="text/markdown",
        ),
    ]

    # Add resources for each active session
    manager = get_session_manager()
    sessions = manager.list_sessions()
    for session_info in sessions:
        session_id = session_info.session_id
        resources.extend(
            [
                Resource(
                    uri=AnyUrl(f"rr://sessions/{session_id}"),
                    name=f"Session {session_id[:8]} state",
                    description=(
                        f"Current state of session {session_id} "
                        f"(trace: {session_info.trace}, pid: {session_info.pid})"
                    ),
                    mimeType="application/json",
                ),
                Resource(
                    uri=AnyUrl(f"rr://sessions/{session_id}/backtrace"),
                    name=f"Session {session_id[:8]} backtrace",
                    description=f"Current call stack for session {session_id}",
                    mimeType="application/json",
                ),
            ]
        )

    return resources


@server.read_resource()  # type: ignore[no-untyped-call, untyped-decorator]
async def read_resource(uri: str) -> str:
    """Read a resource's current state."""
    manager = get_session_manager()

    if uri == "rr://traces":
        traces = list_traces()
        return json.dumps([{"name": t.name, "path": t.path} for t in traces], indent=2)

    if uri == "rr://guide":
        return _DEBUGGING_GUIDE

    if uri.startswith("rr://sessions/"):
        parts = uri.replace("rr://sessions/", "").split("/")
        session_id = parts[0]

        session = manager.get_session(session_id)

        if len(parts) == 1:
            # Session state
            position = await session.get_current_position()
            location = await session.get_current_location()
            return json.dumps(
                {
                    "session_id": session_id,
                    "trace": session.trace,
                    "pid": session.pid,
                    "position": {"event": position[0], "tick": position[1]},
                    "location": {
                        "function": location.function,
                        "file": location.file,
                        "line": location.line,
                        "address": location.address,
                    },
                },
                indent=2,
            )

        if parts[1] == "backtrace":
            # Current backtrace
            backtrace = await session.get_backtrace(max_depth=10)
            return json.dumps({"frames": backtrace}, indent=2)

    raise ValueError(f"Unknown resource: {uri}")


@server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
async def list_tools() -> list[Tool]:
    """List available tools."""
    return [
        # Trace management
        Tool(
            name="traces_list",
            description=(
                "List all available rr recordings on the system. "
                "Use this first to discover what traces exist. "
                "Returns: Array of traces with 'name' and 'path' fields. "
                "Tip: Check the 'rr://traces' resource for live-updated trace list."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="trace_info",
            description=(
                "Get detailed metadata about a specific trace "
                "including recording time and command. "
                "Returns: Trace summary with name, path, and metadata. "
                "Use before creating a session to understand what was recorded."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "trace": {
                        "type": "string",
                        "description": "Trace name or path (default: latest-trace)",
                    },
                },
            },
        ),
        Tool(
            name="trace_processes",
            description=(
                "List all processes in a trace with PID, parent PID, command, and exit status. "
                "Essential for multi-process debugging to identify which PID to debug. "
                "For multi-process apps: Child processes must have called "
                "exec() to be debuggable with session_create. "
                "Returns: Array of process info with pid, ppid, command, exit_code fields."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "trace": {
                        "type": "string",
                        "description": "Trace name or path (default: latest-trace)",
                    },
                },
            },
        ),
        # Session lifecycle
        Tool(
            name="session_create",
            description=(
                "Create a new replay session for debugging a specific "
                "process. This is your entry point - "
                "you must create a session before using any debugging commands. "
                "Each session is independent and can be at a different point in the trace. "
                "For multi-process traces, specify the PID from trace_processes. "
                "Returns: session_id (use for all subsequent commands) and initial location. "
                "Tip: Monitor session state via 'rr://sessions/{id}' resource."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "trace": {
                        "type": "string",
                        "description": "Trace name or path (default: latest-trace)",
                    },
                    "pid": {
                        "type": "integer",
                        "description": (
                            "Process ID to debug (from trace_processes). "
                            "Omit to use rr's default "
                            "(usually the main process)."
                        ),
                    },
                },
            },
        ),
        Tool(
            name="session_list",
            description=(
                "List all active replay sessions with their IDs, traces, and PIDs. "
                "Use to check what sessions exist before creating new ones or to find a session ID."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="session_close",
            description=(
                "End a replay session and clean up its GDB/rr process. "
                "Always close sessions when done to free system resources."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID to close",
                    },
                },
                "required": ["session_id"],
            },
        ),
        # Execution control
        Tool(
            name="continue",
            description=(
                "Continue execution forward until hitting a breakpoint, signal, or program end. "
                "Use this to quickly advance to interesting points after setting breakpoints. "
                "Returns: StopResult with reason "
                "(breakpoint-hit, signal-received, exited, etc.) "
                "and location. "
                "Typical workflow: Set breakpoint → continue → inspect state at breakpoint."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": (
                            "Timeout in seconds (default: 30). "
                            "Increase for programs that run a long "
                            "time before hitting a breakpoint."
                        ),
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="reverse_continue",
            description=(
                "Continue execution backward (rr's superpower!) "
                "until hitting a breakpoint, signal, or start. "
                "Critical for finding root causes: Run forward "
                "past a bug, set breakpoint on suspicious function, "
                "then reverse_continue to find when it was first called with bad data. "
                "Returns: StopResult with reason and location."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": (
                            "Timeout in seconds (default: 30). "
                            "Increase for programs that run a long "
                            "time before hitting a breakpoint."
                        ),
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="step",
            description=(
                "Step forward one source line, entering into function calls. "
                "Use when you want to follow execution into functions "
                "to understand their behavior. "
                "Contrast with 'next' which steps over calls. Returns: Location after step. "
                "Example: Stepping on 'foo()' enters foo's first line."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of steps (default: 1)",
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="reverse_step",
            description=(
                "Step backward one source line, entering into function calls in reverse. "
                "Useful for retracing execution when you've stepped too far forward. "
                "Returns: Location after step."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of steps (default: 1)",
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="next",
            description=(
                "Step forward one source line, stepping over function "
                "calls (treat them as single operations). "
                "Use when you want to stay in the current function "
                "without diving into called functions. "
                "Contrast with 'step' which enters calls. Returns: Location after step. "
                "Example: Nexting over 'foo()' runs foo entirely "
                "and stops on the next line."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of steps (default: 1)",
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="reverse_next",
            description=(
                "Step backward one source line, stepping over function calls in reverse. "
                "Returns: Location after step."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of steps (default: 1)",
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="finish",
            description=(
                "Run until the current function returns, then stop at the caller. "
                "Useful when you're deep in a function and want to quickly return to the caller. "
                "Returns: Location in the calling function after the call."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="reverse_finish",
            description=(
                "Run backward to the point where the current function was called (function entry). "
                "Useful for seeing the state when a function was entered. "
                "Returns: Location at function entry."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="stepi",
            description=(
                "Step forward one machine instruction. Use for "
                "low-level debugging when source-level stepping is "
                "too coarse — e.g., analyzing optimized code, "
                "examining exact instruction sequences, or "
                "debugging at the assembly level. "
                "Prefer step/next for most debugging."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of steps (default: 1)",
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="reverse_stepi",
            description=(
                "Step backward one machine instruction. Reverse counterpart of stepi. "
                "Useful for precise reverse debugging when you need "
                "to undo exactly one instruction."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of steps (default: 1)",
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="nexti",
            description=(
                "Step forward one machine instruction, stepping over call instructions. "
                "Like stepi but treats function calls as single operations. "
                "Use when you want assembly-level precision without entering called functions."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of steps (default: 1)",
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="reverse_nexti",
            description=(
                "Step backward one machine instruction, stepping over calls in reverse. "
                "Reverse counterpart of nexti."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of steps (default: 1)",
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="run_to_event",
            description=(
                "Run to a specific event number (rr global time). "
                "Events are monotonically increasing milestones in "
                "program execution (syscalls, signals, context "
                "switches). Use 'when' to get the current event, "
                "then run_to_event to jump to known positions. "
                "Powerful for navigating to previously-seen points "
                "without re-running from the start."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                    "event": {
                        "type": "integer",
                        "description": "Target event number",
                    },
                },
                "required": ["session_id", "event"],
            },
        ),
        # Breakpoints
        Tool(
            name="breakpoint_set",
            description=(
                "Set a breakpoint to pause execution at a specific "
                "location. Essential for debugging workflows. "
                "Location formats: 'function_name', "
                "'file.cpp:line', or '*0xaddress'. "
                "Conditional example: Set on 'malloc' with "
                "condition 'size > 1000' to catch large "
                "allocations. Use GDB expression syntax for "
                "conditions (C-like: ==, !=, &&, ||, etc.). "
                "Returns: Breakpoint data with ID "
                "(use for delete/enable/disable) and location info."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                    "location": {
                        "type": "string",
                        "description": (
                            "Where to break: 'function', 'file.cpp:42', or '*0x12345678'"
                        ),
                    },
                    "condition": {
                        "type": "string",
                        "description": (
                            "Optional GDB expression "
                            "(e.g., 'x > 10', 'ptr != NULL'). "
                            "Break only when true."
                        ),
                    },
                    "temporary": {
                        "type": "boolean",
                        "description": (
                            "If true, breakpoint auto-deletes "
                            "after first hit. "
                            "Useful for one-time breaks."
                        ),
                    },
                },
                "required": ["session_id", "location"],
            },
        ),
        Tool(
            name="breakpoint_delete",
            description=(
                "Delete a breakpoint permanently. Get breakpoint_id "
                "from breakpoint_list or breakpoint_set return value. "
                "Use this to clean up breakpoints you no longer need."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                    "breakpoint_id": {
                        "type": "integer",
                        "description": "Breakpoint number from breakpoint_list or breakpoint_set",
                    },
                },
                "required": ["session_id", "breakpoint_id"],
            },
        ),
        Tool(
            name="breakpoint_list",
            description=(
                "List all breakpoints and watchpoints in the session "
                "with their IDs, locations, hit counts, "
                "and enabled status. "
                "Returns: Array of breakpoint data including "
                "'number' (ID), 'location', 'times' (hit count), "
                "'enabled', 'watchpoint' (bool)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="breakpoint_enable",
            description=(
                "Re-enable a disabled breakpoint. Useful when you "
                "temporarily disabled breakpoints to skip "
                "certain iterations."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                    "breakpoint_id": {
                        "type": "integer",
                        "description": "Breakpoint ID to enable",
                    },
                },
                "required": ["session_id", "breakpoint_id"],
            },
        ),
        Tool(
            name="breakpoint_disable",
            description=(
                "Temporarily disable a breakpoint without deleting "
                "it. Keeps the breakpoint for later use. "
                "Useful when you want to skip certain breakpoints "
                "without losing their configuration."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                    "breakpoint_id": {
                        "type": "integer",
                        "description": "Breakpoint ID to disable",
                    },
                },
                "required": ["session_id", "breakpoint_id"],
            },
        ),
        Tool(
            name="watchpoint_set",
            description=(
                "Set a hardware watchpoint to break when a variable "
                "or memory location is accessed. "
                "Critical for finding when/where data gets "
                "corrupted or unexpectedly modified. "
                "Example: Set 'read' watchpoint on 'my_ptr' "
                "to find all code that reads it. "
                "Requires the variable to be in scope (set after reaching relevant code). "
                "Returns: Breakpoint data with ID and watchpoint=true flag."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                    "expression": {
                        "type": "string",
                        "description": (
                            "Variable or expression to watch "
                            "(e.g., 'my_var', 'obj->field', "
                            "'*(int*)0x12345')"
                        ),
                    },
                    "access_type": {
                        "type": "string",
                        "description": (
                            "What access to watch: 'write' "
                            "(modifications only), 'read' "
                            "(reads only), 'access' (any access)"
                        ),
                        "enum": ["write", "read", "access"],
                    },
                },
                "required": ["session_id", "expression"],
            },
        ),
        # Inspection
        Tool(
            name="backtrace",
            description=(
                "Get the call stack showing how execution reached "
                "the current point. Critical for understanding "
                "crashes. Returns: Array of frames with 'func' "
                "(function name), 'file', 'line', 'addr' "
                "(address), 'level' (0=innermost). "
                "Set full=true to include local variables for "
                "each frame (slower but comprehensive). "
                "Typical use: After hitting breakpoint or crash, "
                "call this to see the call chain."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                    "count": {
                        "type": "integer",
                        "description": (
                            "Max frames to return (default: 20). Use higher for deep call stacks."
                        ),
                    },
                    "full": {
                        "type": "boolean",
                        "description": (
                            "If true, include local variables for each frame (verbose but detailed)"
                        ),
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="print",
            description=(
                "Evaluate any expression in the current context and return its value. "
                "Supports full GDB expression syntax: variables, "
                "struct/class members, array indexing, pointer "
                "dereferencing, casts, function calls. "
                "Examples: 'my_var', 'obj->field', "
                "'*(int*)0x12345', 'strlen(str)', "
                "'(MyClass*)ptr'. "
                "Returns: String representation of the value. "
                "Must be stopped at a location where the expression is in scope."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                    "expression": {
                        "type": "string",
                        "description": (
                            "GDB expression to evaluate "
                            "(e.g., 'my_var', 'obj->x + 5', "
                            "'(char*)ptr')"
                        ),
                    },
                },
                "required": ["session_id", "expression"],
            },
        ),
        Tool(
            name="locals",
            description=(
                "Get all local variables in the current stack frame with their values and types. "
                "Essential for inspecting function state. "
                "Use after stepping into a function or "
                "hitting a breakpoint. "
                "Returns: Array of variables with 'name', 'value', 'type' fields. "
                "Note: Only shows variables in scope at current line."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="args",
            description=(
                "Get function arguments for the current frame. "
                "Subset of locals, but specifically the "
                "parameters passed to the function. "
                "Returns: Array of argument variables with 'name', 'value', 'type'. "
                "Useful for understanding what data was passed to "
                "a function that crashed or behaved unexpectedly."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="frame_select",
            description=(
                "Switch to a different stack frame to inspect its "
                "locals/args. After getting backtrace, use this "
                "to examine callers. "
                "Frame 0 is the innermost (current) frame. "
                "Higher numbers move up the call stack toward "
                "main(). Use to inspect the state in calling "
                "functions when debugging how bad data was "
                "passed down."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                    "frame_num": {
                        "type": "integer",
                        "description": (
                            "Frame number: 0=current/innermost, 1=caller, 2=caller's caller, etc."
                        ),
                    },
                },
                "required": ["session_id", "frame_num"],
            },
        ),
        Tool(
            name="registers",
            description=(
                "Get CPU register values (rax, rbx, rsp, rip, etc.). "
                "Useful for low-level debugging: examining return "
                "values (rax), stack pointer (rsp), instruction "
                "pointer (rip), or function arguments in "
                "registers (rdi, rsi, rdx, rcx). "
                "Returns: Dictionary mapping register name to hex value."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="examine_memory",
            description=(
                "Examine raw memory at an address. Use to inspect "
                "data structures, buffers, or memory regions "
                "that aren't easily accessible via 'print'. "
                "Common uses: dump a buffer ('format=s'), "
                "view a vtable ('format=a'), disassemble code "
                "at an address ('format=i'). Address can be a "
                "hex value, register ('$rsp'), or expression "
                "('ptr+offset')."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                    "address": {
                        "type": "string",
                        "description": "Address or expression",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of units (default: 16)",
                    },
                    "format": {
                        "type": "string",
                        "description": (
                            "Format: x(hex), d(decimal), s(string), i(instruction) (default: x)"
                        ),
                        "enum": ["x", "d", "s", "i", "u", "t", "f", "a", "c"],
                    },
                    "unit_size": {
                        "type": "string",
                        "description": (
                            "Unit size: b(byte), h(halfword), w(word), g(giant) (default: w)"
                        ),
                        "enum": ["b", "h", "w", "g"],
                    },
                },
                "required": ["session_id", "address"],
            },
        ),
        Tool(
            name="when",
            description=(
                "Get current execution position as an rr event "
                "number (global time). Events are shared across "
                "all processes in a trace, so you can correlate "
                "positions across sessions. Use with "
                "run_to_event to bookmark and return to "
                "positions. "
                "Returns: event number and tick (fine-grained position within event)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                },
                "required": ["session_id"],
            },
        ),
        # Threads
        Tool(
            name="threads_list",
            description=(
                "List all threads in the current process with their IDs and current stack frames. "
                "Use to identify which thread is responsible for a crash or unexpected behavior. "
                "After listing, use thread_select to switch to a specific thread for inspection."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="thread_select",
            description=(
                "Switch to a different thread for inspection. "
                "After switching, backtrace/locals/args/print "
                "all operate in that thread's context. "
                "Use threads_list first to see available "
                "thread IDs."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                    "thread_id": {
                        "type": "integer",
                        "description": "Thread ID to select",
                    },
                },
                "required": ["session_id", "thread_id"],
            },
        ),
        # Checkpoints (rr-specific)
        Tool(
            name="checkpoint_create",
            description=(
                "Create a checkpoint (snapshot) at the current position. Checkpoints let you "
                "instantly return to this exact state later with checkpoint_restore — much faster "
                "than re-running from the start. Create checkpoints before exploring unknown code, "
                "so you can quickly backtrack if you go too far."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="checkpoint_restore",
            description=(
                "Restore a previously created checkpoint, instantly "
                "jumping back to that exact program state. "
                "All memory, registers, and execution state "
                "are restored. Use when you've explored past "
                "the point of interest and want to try a "
                "different approach."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                    "checkpoint_id": {
                        "type": "integer",
                        "description": "Checkpoint ID to restore",
                    },
                },
                "required": ["session_id", "checkpoint_id"],
            },
        ),
        Tool(
            name="checkpoint_delete",
            description=(
                "Delete a checkpoint to free resources. Each checkpoint consumes memory "
                "for the saved program state. Delete checkpoints you no longer need."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                    "checkpoint_id": {
                        "type": "integer",
                        "description": "Checkpoint ID to delete",
                    },
                },
                "required": ["session_id", "checkpoint_id"],
            },
        ),
        Tool(
            name="checkpoint_list",
            description=(
                "List all checkpoints in a session with their IDs "
                "and the event/location where they were created. "
                "Use to see what restore points are available."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                },
                "required": ["session_id"],
            },
        ),
        # Source
        Tool(
            name="source_list",
            description=(
                "List source code around a location. Use to see "
                "the code at the current stop point or at any "
                "file:line or function. Essential for "
                "understanding what code will execute next when "
                "stepping. Returns lines with line numbers and "
                "highlights the current line."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                    "location": {
                        "type": "string",
                        "description": "file:line or function name (default: current location)",
                    },
                    "lines_before": {
                        "type": "integer",
                        "description": "Context lines before (default: 5)",
                    },
                    "lines_after": {
                        "type": "integer",
                        "description": "Context lines after (default: 5)",
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="source_path",
            description=(
                "Get the current source file path and line number. "
                "Use to determine exactly where execution is "
                "stopped without fetching surrounding code."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="source_files",
            description=(
                "List all source files known to the debugger. "
                "Useful for finding the correct file path when setting breakpoints by file:line."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                },
                "required": ["session_id"],
            },
        ),
        # Advanced
        Tool(
            name="interrupt",
            description=(
                "Interrupt a running program to pause execution. "
                "Use if a continue or reverse_continue is "
                "taking too long (e.g., no breakpoint hit). "
                "Returns a StopResult with the current location."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="catch",
            description=(
                "Set a catchpoint to break on C++ exceptions or syscalls. "
                "Types: 'throw' (C++ throw), 'catch' (C++ catch), "
                "'syscall' (optionally filtered), "
                "'signal' (optionally filtered). "
                "Catching 'throw' is powerful for finding where exceptions originate. "
                "Catching 'syscall' with filter='write' can trace I/O. "
                "Use with continue to run until the event occurs, then inspect with backtrace."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                    "event": {
                        "type": "string",
                        "description": "Event type: 'throw', 'catch', 'syscall', or 'signal'",
                        "enum": ["throw", "catch", "syscall", "signal"],
                    },
                    "filter": {
                        "type": "string",
                        "description": "Optional filter (syscall name/number or signal name)",
                    },
                },
                "required": ["session_id", "event"],
            },
        ),
        Tool(
            name="handle_signal",
            description=(
                "Configure how GDB handles a specific signal (stop, pass to program, print). "
                "Example: handle_signal('SIGPIPE', stop=False, "
                "pass_through=False) to suppress SIGPIPE."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                    "signal": {
                        "type": "string",
                        "description": "Signal name (e.g., 'SIGPIPE', 'SIGUSR1', 'all')",
                    },
                    "stop": {
                        "type": "boolean",
                        "description": "Whether to stop on this signal",
                    },
                    "pass_through": {
                        "type": "boolean",
                        "description": "Whether to pass the signal to the program",
                    },
                    "print": {
                        "type": "boolean",
                        "description": "Whether to print when the signal is received",
                    },
                },
                "required": ["session_id", "signal"],
            },
        ),
        Tool(
            name="find_in_memory",
            description=(
                "Search memory for a byte pattern between two addresses. "
                "Use to find specific values in memory, locate "
                "string occurrences, or scan the stack/heap. "
                "Get address ranges from 'registers' "
                "(e.g., rsp for stack) or 'info' with "
                "'proc mappings'. "
                "Returns list of addresses where the pattern was found."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                    "start": {
                        "type": "string",
                        "description": "Start address (hex or expression)",
                    },
                    "end": {
                        "type": "string",
                        "description": "End address (hex or expression)",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Search pattern (hex bytes, string, or expression)",
                    },
                    "size": {
                        "type": "string",
                        "description": (
                            "Unit size: 'b' (byte), 'h' (halfword), 'w' (word), 'g' (giant)"
                        ),
                        "enum": ["b", "h", "w", "g"],
                    },
                },
                "required": ["session_id", "start", "end", "pattern"],
            },
        ),
        Tool(
            name="info",
            description=(
                "Run a GDB 'info' subcommand for process/symbol introspection. "
                "Examples: 'proc mappings', 'shared', 'symbol 0x12345', 'types', 'signals'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                    "subcommand": {
                        "type": "string",
                        "description": (
                            "Info subcommand (e.g., 'proc mappings', 'shared', 'signals')"
                        ),
                    },
                },
                "required": ["session_id", "subcommand"],
            },
        ),
        # Escape hatch
        Tool(
            name="gdb_raw",
            description=(
                "Execute an arbitrary GDB command (escape hatch). "
                "WARNING: GDB supports 'shell' commands that execute on the host system."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID",
                    },
                    "command": {
                        "type": "string",
                        "description": "GDB command to execute",
                    },
                },
                "required": ["session_id", "command"],
            },
        ),
    ]


@server.call_tool()  # type: ignore[untyped-decorator]
async def call_tool(name: str, arguments: dict[str, object]) -> list[TextContent]:
    """Handle tool calls.

    Exceptions propagate to the MCP framework, which converts them to
    CallToolResult with isError=True automatically.
    """
    result = await _handle_tool(name, arguments)
    return [TextContent(type="text", text=_format_result(result))]


def _get_str_arg(arguments: dict[str, object], key: str) -> str:
    """Extract a string argument (already validated by MCP schema)."""
    return str(arguments[key])


def _get_optional_str_arg(arguments: dict[str, object], key: str) -> str | None:
    """Extract an optional string argument."""
    value = arguments.get(key)
    return str(value) if value is not None else None


def _get_int_arg(arguments: dict[str, object], key: str) -> int:
    """Extract and validate an integer argument."""
    value = arguments[key]
    if isinstance(value, bool):
        raise TypeError(f"Expected {key} to be int, got bool")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != int(value):
            raise TypeError(f"Expected {key} to be int, got non-integer float {value}")
        return int(value)
    if isinstance(value, str):
        return int(value)
    raise TypeError(f"Expected {key} to be int, got {type(value)}")


def _get_optional_int_arg(arguments: dict[str, object], key: str) -> int | None:
    """Extract and validate an optional integer argument."""
    value = arguments.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError(f"Expected {key} to be int, got bool")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != int(value):
            raise TypeError(f"Expected {key} to be int, got non-integer float {value}")
        return int(value)
    if isinstance(value, str):
        return int(value)
    raise TypeError(f"Expected {key} to be int, got {type(value)}")


def _get_int_arg_with_default(arguments: dict[str, object], key: str, default: int) -> int:
    """Extract an optional integer argument, returning default if absent.

    Unlike `_get_optional_int_arg(...) or default`, this correctly preserves
    explicit zero values instead of replacing them with the default.
    """
    value = _get_optional_int_arg(arguments, key)
    return value if value is not None else default


async def _handle_tool(name: str, arguments: dict[str, object]) -> dict[str, object]:
    """Dispatch tool calls to handlers."""
    manager = get_session_manager()

    # -------------------------------------------------------------------------
    # Trace management
    # -------------------------------------------------------------------------

    if name == "traces_list":
        traces = list_traces()
        return {
            "traces": [
                {
                    "name": t.name,
                    "path": t.path,
                    "created_at": t.created_at.isoformat(),
                }
                for t in traces
            ]
        }

    if name == "trace_info":
        trace_obj = arguments.get("trace")
        trace = str(trace_obj) if trace_obj is not None else None
        info = get_trace_info(trace)
        return {
            "trace": info.name,
            "path": info.path,
            "total_events": info.total_events,
            "total_time_ns": info.total_time_ns,
            "recording_time": info.recording_time.isoformat(),
        }

    if name == "trace_processes":
        trace_obj = arguments.get("trace")
        trace = str(trace_obj) if trace_obj is not None else None
        processes = get_trace_processes(trace)
        return {
            "processes": [
                {
                    "pid": p.pid,
                    "ppid": p.ppid,
                    "exit_code": p.exit_code,
                    "command": p.command,
                    "args": p.args,
                }
                for p in processes
            ]
        }

    # -------------------------------------------------------------------------
    # Session lifecycle
    # -------------------------------------------------------------------------

    if name == "session_create":
        trace_obj = arguments.get("trace")
        trace = str(trace_obj) if trace_obj is not None else None
        pid_obj = arguments.get("pid")
        pid = int(pid_obj) if pid_obj is not None and isinstance(pid_obj, (int, str)) else None
        trace_path = str(resolve_trace_path(trace))

        session, initial_location = await manager.create_session(trace=trace_path, pid=pid)
        return {
            "session_id": session.session_id,
            "trace": session.trace,
            "pid": session.pid,
            "state": session.state.value,
            "initial_location": _location_to_dict(initial_location),
        }

    if name == "session_list":
        sessions = manager.list_sessions()
        return {
            "sessions": [
                {
                    "session_id": s.session_id,
                    "trace": s.trace,
                    "pid": s.pid,
                    "state": s.state.value,
                }
                for s in sessions
            ]
        }

    if name == "session_close":
        await manager.close_session(_get_str_arg(arguments, "session_id"))
        return {"success": True}

    # -------------------------------------------------------------------------
    # Execution control - using typed Session methods
    # -------------------------------------------------------------------------

    if name == "continue":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        timeout = _get_int_arg_with_default(arguments, "timeout", 30)
        result = await session.continue_execution(timeout_sec=timeout)
        return _stop_result_to_dict(result)

    if name == "reverse_continue":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        timeout = _get_int_arg_with_default(arguments, "timeout", 30)
        result = await session.reverse_continue(timeout_sec=timeout)
        return _stop_result_to_dict(result)

    if name == "step":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        count = _get_int_arg_with_default(arguments, "count", 1)
        result = await session.step(count=count)
        return _stop_result_to_dict(result)

    if name == "reverse_step":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        count = _get_int_arg_with_default(arguments, "count", 1)
        result = await session.reverse_step(count=count)
        return _stop_result_to_dict(result)

    if name == "next":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        count = _get_int_arg_with_default(arguments, "count", 1)
        result = await session.next(count=count)
        return _stop_result_to_dict(result)

    if name == "reverse_next":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        count = _get_int_arg_with_default(arguments, "count", 1)
        result = await session.reverse_next(count=count)
        return _stop_result_to_dict(result)

    if name == "finish":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        result = await session.finish()
        return _stop_result_to_dict(result)

    if name == "reverse_finish":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        result = await session.reverse_finish()
        return _stop_result_to_dict(result)

    if name == "stepi":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        count = _get_int_arg_with_default(arguments, "count", 1)
        result = await session.step_instruction(count=count)
        return _stop_result_to_dict(result)

    if name == "reverse_stepi":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        count = _get_int_arg_with_default(arguments, "count", 1)
        result = await session.reverse_step_instruction(count=count)
        return _stop_result_to_dict(result)

    if name == "nexti":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        count = _get_int_arg_with_default(arguments, "count", 1)
        result = await session.next_instruction(count=count)
        return _stop_result_to_dict(result)

    if name == "reverse_nexti":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        count = _get_int_arg_with_default(arguments, "count", 1)
        result = await session.reverse_next_instruction(count=count)
        return _stop_result_to_dict(result)

    if name == "run_to_event":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        event = _get_int_arg(arguments, "event")
        result = await session.run_to_event(event)
        return _stop_result_to_dict(result)

    # -------------------------------------------------------------------------
    # Breakpoints - using typed Session methods
    # -------------------------------------------------------------------------

    if name == "breakpoint_set":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        location = _get_str_arg(arguments, "location")
        condition = _get_optional_str_arg(arguments, "condition")
        temporary = bool(arguments.get("temporary", False))

        bp_data = await session.set_breakpoint(location, temporary, condition)
        if bp_data is None or bp_data.number is None:
            raise GdbError(f"Failed to set breakpoint at: {location}")

        # Build locations array (may be single location)
        locations = []
        if bp_data.address or bp_data.file or bp_data.function:
            locations.append(
                {
                    "address": bp_data.address,
                    "file": bp_data.file,
                    "line": bp_data.line,
                    "function": bp_data.function,
                }
            )

        return {
            "breakpoint_id": bp_data.number,
            "locations": locations,
        }

    if name == "breakpoint_delete":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        breakpoint_id = _get_int_arg(arguments, "breakpoint_id")
        success = await session.delete_breakpoint(breakpoint_id)
        if not success:
            raise GdbError(f"Failed to delete breakpoint {breakpoint_id}")
        return {"breakpoint_id": breakpoint_id}

    if name == "breakpoint_list":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        breakpoints = await session.list_breakpoints()
        return {
            "breakpoints": [
                {
                    "breakpoint_id": bp.number,
                    "type": bp.type,
                    "enabled": bp.enabled,
                    "file": bp.file,
                    "line": bp.line,
                    "function": bp.function,
                    "condition": bp.condition,
                    "hit_count": bp.times,
                }
                for bp in breakpoints
            ]
        }

    if name == "breakpoint_enable":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        breakpoint_id = _get_int_arg(arguments, "breakpoint_id")
        success = await session.enable_breakpoint(breakpoint_id)
        if not success:
            raise GdbError(f"Failed to enable breakpoint {breakpoint_id}")
        return {"breakpoint_id": breakpoint_id}

    if name == "breakpoint_disable":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        breakpoint_id = _get_int_arg(arguments, "breakpoint_id")
        success = await session.disable_breakpoint(breakpoint_id)
        if not success:
            raise GdbError(f"Failed to disable breakpoint {breakpoint_id}")
        return {"breakpoint_id": breakpoint_id}

    if name == "watchpoint_set":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        expression = _get_str_arg(arguments, "expression")
        access_type = _get_optional_str_arg(arguments, "access_type") or "write"

        wp_data = await session.set_watchpoint(expression, access_type)
        if wp_data is None or wp_data.number is None:
            raise GdbError(f"Failed to set watchpoint on: {expression}")
        return {"watchpoint_id": wp_data.number, "expression": expression}

    # -------------------------------------------------------------------------
    # Inspection - using typed Session methods
    # -------------------------------------------------------------------------

    if name == "backtrace":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        count = _get_int_arg_with_default(arguments, "count", 20)
        full = bool(arguments.get("full", False))
        frames = await session.get_backtrace(max_depth=count, full=full)
        return {
            "frames": [
                {
                    "frame_num": f.get("level"),
                    "function": f.get("func"),
                    "file": f.get("file"),
                    "line": f.get("line"),
                    "address": f.get("addr"),
                    "locals": f.get("locals"),  # Will be None if not full
                }
                for f in frames
            ]
        }

    if name == "print":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        expression = _get_str_arg(arguments, "expression")
        value = await session.evaluate_expression(expression)
        return {"expression": expression, "value": value, "type": None}

    if name == "locals":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        variables = await session.get_local_variables()
        return {"locals": [{"name": v.name, "value": v.value, "type": v.type} for v in variables]}

    if name == "args":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        frame_args = await session.get_function_arguments()
        # Return args for current frame (first in list)
        if frame_args:
            return {
                "args": [{"name": a.name, "value": a.value, "type": a.type} for a in frame_args[0]]
            }
        return {"args": []}

    if name == "frame_select":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        frame_num = _get_int_arg(arguments, "frame_num")
        success = await session.select_frame(frame_num)
        if not success:
            raise GdbError(f"Failed to select frame {frame_num}")
        loc = await session.get_current_location()
        return {
            "frame_num": frame_num,
            "function": loc.function,
            "file": loc.file,
            "line": loc.line,
        }

    if name == "registers":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        registers = await session.read_registers()
        return {"registers": registers}

    if name == "examine_memory":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        address = _get_str_arg(arguments, "address")
        count = _get_int_arg_with_default(arguments, "count", 16)
        format_char = _get_optional_str_arg(arguments, "format") or "x"
        unit_size = _get_optional_str_arg(arguments, "unit_size") or "w"
        data = await session.examine_memory(address, count, format_char, unit_size)
        return {
            "address": address,
            "data": data,
        }

    if name == "when":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        event, tick = await session.get_current_position()
        return {"event": event, "tick": tick}

    # -------------------------------------------------------------------------
    # Threads - using typed Session methods
    # -------------------------------------------------------------------------

    if name == "threads_list":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        threads = await session.list_threads()
        return {
            "threads": [
                {
                    "thread_id": t.get("id"),
                    "name": t.get("name"),
                    "state": t.get("state"),
                    "frame": t.get("frame"),
                }
                for t in threads
            ]
        }

    if name == "thread_select":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        thread_id = _get_int_arg(arguments, "thread_id")
        success = await session.select_thread(thread_id)
        if not success:
            raise GdbError(f"Failed to select thread {thread_id}")
        loc = await session.get_current_location()
        return {
            "thread_id": thread_id,
            "frame": {
                "function": loc.function,
                "file": loc.file,
                "line": loc.line,
            },
        }

    # -------------------------------------------------------------------------
    # Checkpoints - using typed Session methods
    # -------------------------------------------------------------------------

    if name == "checkpoint_create":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        event, tick = await session.get_current_position()
        checkpoint_id = await session.create_checkpoint()
        if checkpoint_id is None:
            raise GdbError("Failed to create checkpoint")
        return {"checkpoint_id": checkpoint_id, "event": event, "tick": tick}

    if name == "checkpoint_restore":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        checkpoint_id = _get_int_arg(arguments, "checkpoint_id")
        success = await session.restore_checkpoint(checkpoint_id)
        if not success:
            raise GdbError(f"Failed to restore checkpoint {checkpoint_id}")
        loc = await session.get_current_location()
        return {
            "checkpoint_id": checkpoint_id,
            "location": _location_to_dict(loc),
        }

    if name == "checkpoint_delete":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        checkpoint_id = _get_int_arg(arguments, "checkpoint_id")
        success = await session.delete_checkpoint(checkpoint_id)
        if not success:
            raise GdbError(f"Failed to delete checkpoint {checkpoint_id}")
        return {"checkpoint_id": checkpoint_id}

    if name == "checkpoint_list":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        checkpoints = await session.list_checkpoints()
        return {
            "checkpoints": [
                {
                    "checkpoint_id": cp.id,
                    "event": cp.event,
                    "tick": cp.tick,
                }
                for cp in checkpoints
            ]
        }

    # -------------------------------------------------------------------------
    # Source - using typed Session methods
    # -------------------------------------------------------------------------

    if name == "source_list":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        location_str = _get_optional_str_arg(arguments, "location")
        lines_before = _get_int_arg_with_default(arguments, "lines_before", 5)
        lines_after = _get_int_arg_with_default(arguments, "lines_after", 5)
        source_result = await session.get_source_lines(location_str, lines_before, lines_after)
        return dict(source_result)

    if name == "source_path":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        loc = await session.get_current_location()
        fullpath = await session.resolve_source_fullpath(loc.file)
        return {
            "file": loc.file,
            "line": loc.line,
            "fullpath": fullpath,
        }

    if name == "source_files":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        files = await session.list_source_files()
        return {"files": files}

    # -------------------------------------------------------------------------
    # Advanced tools
    # -------------------------------------------------------------------------

    if name == "interrupt":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        result = await session.interrupt()
        return _stop_result_to_dict(result)

    if name == "catch":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        catch_event = _get_str_arg(arguments, "event")
        filter_arg = _get_optional_str_arg(arguments, "filter")

        catch_methods = {
            "throw": lambda: session.catch_throw(),
            "catch": lambda: session.catch_catch(),
            "syscall": lambda: session.catch_syscall(filter_arg),
            "signal": lambda: session.catch_signal(filter_arg),
        }
        method = catch_methods.get(catch_event)
        if method is None:
            raise RrMcpError(f"Unknown catch event type: {catch_event}")

        bp_data = await method()
        if bp_data is None or bp_data.number is None:
            raise GdbError(f"Failed to set catchpoint for: {catch_event}")
        return {"catchpoint_id": bp_data.number, "event": catch_event}

    if name == "handle_signal":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        sig = _get_str_arg(arguments, "signal")
        stop = arguments.get("stop")
        pass_through = arguments.get("pass_through")
        print_sig = arguments.get("print")
        output = await session.handle_signal(
            sig,
            stop=bool(stop) if stop is not None else None,
            pass_through=bool(pass_through) if pass_through is not None else None,
            print_signal=bool(print_sig) if print_sig is not None else None,
        )
        return {"signal": sig, "output": output}

    if name == "find_in_memory":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        start = _get_str_arg(arguments, "start")
        end = _get_str_arg(arguments, "end")
        pattern = _get_str_arg(arguments, "pattern")
        size = _get_optional_str_arg(arguments, "size")
        addresses = await session.find_in_memory(start, end, pattern, size)
        return {"addresses": addresses, "count": len(addresses)}

    if name == "info":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        subcommand = _get_str_arg(arguments, "subcommand")
        output = await session.info(subcommand)
        return {"output": output}

    # -------------------------------------------------------------------------
    # Escape hatch
    # -------------------------------------------------------------------------

    if name == "gdb_raw":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        command = _get_str_arg(arguments, "command")
        escaped = _mi_escape(command)
        response = await session.execute(f'-interpreter-exec console "{escaped}"')
        output_lines: list[str] = []
        for record in response:
            if record.get("type") == "console":
                payload = record.get("payload", "")
                if isinstance(payload, str):
                    output_lines.append(payload)
        return {"output": "".join(output_lines)}

    raise RrMcpError(f"Unknown tool: {name}")


# -----------------------------------------------------------------------------
# Helper functions for formatting responses
# -----------------------------------------------------------------------------


def _location_to_dict(location: Location) -> LocationDict:
    """Convert a Location to a dict."""
    return LocationDict(
        event=location.event,
        tick=location.tick,
        function=location.function,
        file=location.file,
        line=location.line,
        address=location.address,
    )


def _stop_result_to_dict(result: StopResult | None) -> dict[str, object]:
    """Convert a StopResult to a dict.

    Raises:
        RrMcpError: If result is None (no stop event received).
    """
    if result is None:
        raise RrMcpError("No stop event received from debugger")

    response: dict[str, object] = {
        "reason": result.reason,
        "location": _location_to_dict(result.location),
    }

    if result.signal:
        response["signal"] = {
            "name": result.signal.name,
            "meaning": result.signal.meaning,
        }

    if result.breakpoint_id is not None:
        response["breakpoint_id"] = result.breakpoint_id

    return response


def _format_result(result: dict[str, object]) -> str:
    """Format a result as JSON."""
    return json.dumps(result, indent=2)


def _validate_rr_available() -> None:
    """Validate that rr is installed and available."""
    import shutil

    if not shutil.which("rr"):
        raise RrMcpError(
            "rr is not installed or not in PATH. Please install rr from https://rr-project.org/"
        )


def _sync_cleanup() -> None:
    """Synchronous cleanup: kill any remaining GDB/rr child processes.

    Registered via atexit so it runs even on unhandled exceptions or signals.
    """
    manager = _session_manager
    if manager is None:
        return
    for session in manager.list_sessions():
        # Access the underlying GdbController and kill its process
        gdb = session._gdb
        if gdb is None:
            continue
        try:
            proc = gdb._gdb.gdb_process
            if proc and proc.poll() is None:
                proc.kill()
        except Exception:
            pass


def main() -> None:
    """Run the MCP server."""
    _validate_rr_available()

    # Register atexit handler to kill orphaned GDB processes
    atexit.register(_sync_cleanup)

    # Re-raise SIGTERM as SystemExit so the async finally block runs
    def _handle_sigterm(_signum: int, _frame: object) -> None:
        raise SystemExit(1)

    signal.signal(signal.SIGTERM, _handle_sigterm)

    asyncio.run(_run_server())


async def _run_server() -> None:
    """Run the server with stdio transport."""
    async with stdio_server() as (read_stream, write_stream):
        try:
            await server.run(read_stream, write_stream, server.create_initialization_options())
        finally:
            # Clean up all sessions on shutdown
            manager = get_session_manager()
            await manager.close_all()
