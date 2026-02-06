"""MCP server for rr debugging."""

import asyncio
import json
from typing import TYPE_CHECKING

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Resource, TextContent, Tool
from pydantic import AnyUrl

from rr_mcp.errors import RrMcpError, SessionNotFoundError, TraceNotFoundError
from rr_mcp.models import (
    ErrorResponseDict,
    Location,
    LocationDict,
    SignalDict,
    StopResult,
    StopResultDict,
)
from rr_mcp.session import SessionManager
from rr_mcp.trace import (
    get_trace_info,
    get_trace_processes,
    list_traces,
    resolve_trace_path,
)

if TYPE_CHECKING:
    pass

# Global session manager
_session_manager: SessionManager | None = None


def get_session_manager() -> SessionManager:
    """Get or create the global session manager."""
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager()
    return _session_manager


# Create the MCP server
server = Server("rr-mcp")


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
                    description=f"Current state of session {session_id} (trace: {session_info.trace}, pid: {session_info.pid})",
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

    if uri.startswith("rr://sessions/"):
        parts = uri.replace("rr://sessions/", "").split("/")
        session_id = parts[0]

        session = manager.get_session(session_id)
        if session is None:
            raise SessionNotFoundError(session_id)

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
                "List all available rr recordings on the system. Use this first to discover what traces exist. "
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
                "Get detailed metadata about a specific trace including recording time and command. "
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
                "For multi-process apps: Child processes must have called exec() to be debuggable with session_create. "
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
                "Create a new replay session for debugging a specific process. This is your entry point - "
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
                        "description": "Process ID to debug (from trace_processes). Omit to use rr's default (usually the main process).",
                    },
                },
            },
        ),
        Tool(
            name="session_list",
            description="List all active replay sessions",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="session_close",
            description="End a replay session and clean up resources",
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
            name="continue_",
            description=(
                "Continue execution forward until hitting a breakpoint, signal, or program end. "
                "Use this to quickly advance to interesting points after setting breakpoints. "
                "Returns: StopResult with reason (breakpoint-hit, signal-received, exited, etc.) and location. "
                "Typical workflow: Set breakpoint → continue → inspect state at breakpoint."
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
            name="reverse_continue",
            description=(
                "Continue execution backward (rr's superpower!) until hitting a breakpoint, signal, or start. "
                "Critical for finding root causes: Run forward past a bug, set breakpoint on suspicious function, "
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
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="step",
            description=(
                "Step forward one source line, entering into function calls. "
                "Use when you want to follow execution into functions to understand their behavior. "
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
                "Step forward one source line, stepping over function calls (treat them as single operations). "
                "Use when you want to stay in the current function without diving into called functions. "
                "Contrast with 'step' which enters calls. Returns: Location after step. "
                "Example: Nexting over 'foo()' runs foo entirely and stops on the next line."
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
            description="Step forward one machine instruction",
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
            description="Step backward one machine instruction",
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
            description="Step forward one machine instruction (over calls)",
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
            description="Step backward one machine instruction (over calls)",
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
            description="Run to a specific event number (rr global time)",
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
                "Set a breakpoint to pause execution at a specific location. Essential for debugging workflows. "
                "Location formats: 'function_name', 'file.cpp:line', or '*0xaddress'. "
                "Conditional example: Set on 'malloc' with condition 'size > 1000' to catch large allocations. "
                "Use GDB expression syntax for conditions (C-like: ==, !=, &&, ||, etc.). "
                "Returns: Breakpoint data with ID (use for delete/enable/disable) and location info."
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
                        "description": "Where to break: 'function', 'file.cpp:42', or '*0x12345678'",
                    },
                    "condition": {
                        "type": "string",
                        "description": "Optional GDB expression (e.g., 'x > 10', 'ptr != NULL'). Break only when true.",
                    },
                    "temporary": {
                        "type": "boolean",
                        "description": "If true, breakpoint auto-deletes after first hit. Useful for one-time breaks.",
                    },
                },
                "required": ["session_id", "location"],
            },
        ),
        Tool(
            name="breakpoint_delete",
            description=(
                "Delete a breakpoint permanently. Get breakpoint_id from breakpoint_list or breakpoint_set return value. "
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
                "List all breakpoints and watchpoints in the session with their IDs, locations, hit counts, and enabled status. "
                "Returns: Array of breakpoint data including 'number' (ID), 'location', 'times' (hit count), 'enabled', 'watchpoint' (bool)."
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
                "Re-enable a disabled breakpoint. Useful when you temporarily disabled breakpoints to skip certain iterations."
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
                "Temporarily disable a breakpoint without deleting it. Keeps the breakpoint for later use. "
                "Useful when you want to skip certain breakpoints without losing their configuration."
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
                "Set a hardware watchpoint to break when a variable or memory location is accessed. "
                "Critical for finding when/where data gets corrupted or unexpectedly modified. "
                "Example: Set 'read' watchpoint on 'my_ptr' to find all code that reads it. "
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
                        "description": "Variable or expression to watch (e.g., 'my_var', 'obj->field', '*(int*)0x12345')",
                    },
                    "access_type": {
                        "type": "string",
                        "description": "What access to watch: 'write' (modifications only), 'read' (reads only), 'access' (any access)",
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
                "Get the call stack showing how execution reached the current point. Critical for understanding crashes. "
                "Returns: Array of frames with 'func' (function name), 'file', 'line', 'addr' (address), 'level' (0=innermost). "
                "Set full=true to include local variables for each frame (slower but comprehensive). "
                "Typical use: After hitting breakpoint or crash, call this to see the call chain."
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
                        "description": "Max frames to return (default: 20). Use higher for deep call stacks.",
                    },
                    "full": {
                        "type": "boolean",
                        "description": "If true, include local variables for each frame (verbose but detailed)",
                    },
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="print",
            description=(
                "Evaluate any expression in the current context and return its value. "
                "Supports full GDB expression syntax: variables, struct/class members, array indexing, pointer dereferencing, casts, function calls. "
                "Examples: 'my_var', 'obj->field', '*(int*)0x12345', 'strlen(str)', '(MyClass*)ptr'. "
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
                        "description": "GDB expression to evaluate (e.g., 'my_var', 'obj->x + 5', '(char*)ptr')",
                    },
                },
                "required": ["session_id", "expression"],
            },
        ),
        Tool(
            name="locals",
            description=(
                "Get all local variables in the current stack frame with their values and types. "
                "Essential for inspecting function state. Use after stepping into a function or hitting a breakpoint. "
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
                "Get function arguments for the current frame. Subset of locals, but specifically the parameters passed to the function. "
                "Returns: Array of argument variables with 'name', 'value', 'type'. "
                "Useful for understanding what data was passed to a function that crashed or behaved unexpectedly."
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
                "Switch to a different stack frame to inspect its locals/args. After getting backtrace, use this to examine callers. "
                "Frame 0 is the innermost (current) frame. Higher numbers move up the call stack toward main(). "
                "Use to inspect the state in calling functions when debugging how bad data was passed down."
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
                        "description": "Frame number: 0=current/innermost, 1=caller, 2=caller's caller, etc.",
                    },
                },
                "required": ["session_id", "frame_num"],
            },
        ),
        Tool(
            name="registers",
            description="Get CPU register values",
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
            description="Examine raw memory",
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
            description="Get current execution position (rr global time)",
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
            description="List all threads in the current process",
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
            description="Switch to a different thread",
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
            description="Create a checkpoint at the current position",
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
            description="Restore a previously created checkpoint",
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
            description="Delete a checkpoint to free resources",
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
            description="List all checkpoints in a session",
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
            description="List source code around a location",
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
            description="Get the current source file and line",
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
            description="List all source files in the program",
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
        # Escape hatch
        Tool(
            name="gdb_raw",
            description="Execute an arbitrary GDB command (escape hatch)",
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
    """Handle tool calls."""
    try:
        result = await _handle_tool(name, arguments)
        return [TextContent(type="text", text=_format_result(result))]
    except RrMcpError as e:
        return [TextContent(type="text", text=_format_error(e))]


def _get_str_arg(arguments: dict[str, object], key: str) -> str:
    """Extract and validate a string argument."""
    value = arguments[key]
    assert isinstance(value, str), f"Expected {key} to be str, got {type(value)}"
    return value


def _get_optional_str_arg(arguments: dict[str, object], key: str) -> str | None:
    """Extract and validate an optional string argument."""
    value = arguments.get(key)
    if value is None:
        return None
    assert isinstance(value, str), f"Expected {key} to be str, got {type(value)}"
    return value


def _get_int_arg(arguments: dict[str, object], key: str) -> int:
    """Extract and validate an integer argument."""
    value = arguments[key]
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    raise TypeError(f"Expected {key} to be int, got {type(value)}")


def _get_optional_int_arg(arguments: dict[str, object], key: str) -> int | None:
    """Extract and validate an optional integer argument."""
    value = arguments.get(key)
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    raise TypeError(f"Expected {key} to be int, got {type(value)}")


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
                    "size_bytes": t.size_bytes,
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
                    "event_start": p.event_start,
                    "event_end": p.event_end,
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
        session_id_obj = arguments["session_id"]
        assert isinstance(session_id_obj, str)
        await manager.close_session(session_id_obj)
        return {"success": True}

    # -------------------------------------------------------------------------
    # Execution control - using typed Session methods
    # -------------------------------------------------------------------------

    if name == "continue_":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        result = await session.continue_execution()
        return _stop_result_to_dict(result)  # type: ignore[return-value]

    if name == "reverse_continue":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        result = await session.reverse_continue()
        return _stop_result_to_dict(result)  # type: ignore[return-value]

    if name == "step":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        count = _get_optional_int_arg(arguments, "count") or 1
        result = await session.step(count=count)
        return _stop_result_to_dict(result)  # type: ignore[return-value]

    if name == "reverse_step":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        count = _get_optional_int_arg(arguments, "count") or 1
        result = await session.reverse_step(count=count)
        return _stop_result_to_dict(result)  # type: ignore[return-value]

    if name == "next":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        count = _get_optional_int_arg(arguments, "count") or 1
        result = await session.next(count=count)
        return _stop_result_to_dict(result)  # type: ignore[return-value]

    if name == "reverse_next":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        count = _get_optional_int_arg(arguments, "count") or 1
        result = await session.reverse_next(count=count)
        return _stop_result_to_dict(result)  # type: ignore[return-value]

    if name == "finish":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        result = await session.finish()
        return _stop_result_to_dict(result)  # type: ignore[return-value]

    if name == "reverse_finish":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        result = await session.reverse_finish()
        return _stop_result_to_dict(result)  # type: ignore[return-value]

    if name == "stepi":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        count = _get_optional_int_arg(arguments, "count") or 1
        result = await session.step_instruction(count=count)
        return _stop_result_to_dict(result)  # type: ignore[return-value]

    if name == "reverse_stepi":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        count = _get_optional_int_arg(arguments, "count") or 1
        result = await session.reverse_step_instruction(count=count)
        return _stop_result_to_dict(result)  # type: ignore[return-value]

    if name == "nexti":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        count = _get_optional_int_arg(arguments, "count") or 1
        result = await session.next_instruction(count=count)
        return _stop_result_to_dict(result)  # type: ignore[return-value]

    if name == "reverse_nexti":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        count = _get_optional_int_arg(arguments, "count") or 1
        result = await session.reverse_next_instruction(count=count)
        return _stop_result_to_dict(result)  # type: ignore[return-value]

    if name == "run_to_event":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        event = _get_int_arg(arguments, "event")
        result = await session.run_to_event(event)
        return _stop_result_to_dict(result)  # type: ignore[return-value]

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
            return {"success": False, "error": "Failed to set breakpoint"}

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
        return {"success": success}

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
        return {"success": success}

    if name == "breakpoint_disable":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        breakpoint_id = _get_int_arg(arguments, "breakpoint_id")
        success = await session.disable_breakpoint(breakpoint_id)
        return {"success": success}

    if name == "watchpoint_set":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        expression = _get_str_arg(arguments, "expression")
        access_type = _get_optional_str_arg(arguments, "access_type") or "write"

        wp_num = await session.set_watchpoint(expression, access_type)
        if wp_num is None:
            return {"success": False, "error": "Failed to set watchpoint"}
        return {"watchpoint_id": wp_num, "expression": expression}

    # -------------------------------------------------------------------------
    # Inspection - using typed Session methods
    # -------------------------------------------------------------------------

    if name == "backtrace":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        count = _get_optional_int_arg(arguments, "count") or 20
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
        if success:
            # Get frame info after selection
            loc = await session.get_current_location()
            return {
                "frame_num": frame_num,
                "function": loc.function,
                "file": loc.file,
                "line": loc.line,
            }
        return {"success": False, "error": "Failed to select frame"}

    if name == "registers":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        registers = await session.read_registers()
        return {"registers": registers}

    if name == "examine_memory":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        address = _get_str_arg(arguments, "address")
        count = _get_optional_int_arg(arguments, "count") or 16
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
        if success:
            loc = await session.get_current_location()
            return {
                "thread_id": thread_id,
                "frame": {
                    "function": loc.function,
                    "file": loc.file,
                    "line": loc.line,
                },
            }
        return {"success": False, "error": "Failed to select thread"}

    # -------------------------------------------------------------------------
    # Checkpoints - using typed Session methods
    # -------------------------------------------------------------------------

    if name == "checkpoint_create":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        event, tick = await session.get_current_position()
        checkpoint_id = await session.create_checkpoint()
        if checkpoint_id is None:
            return {"success": False, "error": "Failed to create checkpoint"}
        return {"checkpoint_id": checkpoint_id, "event": event, "tick": tick}

    if name == "checkpoint_restore":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        checkpoint_id = _get_int_arg(arguments, "checkpoint_id")
        success = await session.restore_checkpoint(checkpoint_id)
        if success:
            loc = await session.get_current_location()
            return {
                "checkpoint_id": checkpoint_id,
                "location": _location_to_dict(loc),
            }
        return {"success": False, "error": "Failed to restore checkpoint"}

    if name == "checkpoint_delete":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        checkpoint_id = _get_int_arg(arguments, "checkpoint_id")
        success = await session.delete_checkpoint(checkpoint_id)
        return {"success": success}

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
        lines_before = _get_optional_int_arg(arguments, "lines_before") or 5
        lines_after = _get_optional_int_arg(arguments, "lines_after") or 5
        source_result = await session.get_source_lines(location_str, lines_before, lines_after)
        return source_result  # type: ignore[return-value]

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
    # Escape hatch
    # -------------------------------------------------------------------------

    if name == "gdb_raw":
        session = manager.get_session(_get_str_arg(arguments, "session_id"))
        command = _get_str_arg(arguments, "command")
        response = await session.execute(f'-interpreter-exec console "{command}"')
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


def _stop_result_to_dict(result: StopResult | None) -> StopResultDict:
    """Convert a StopResult to a dict."""
    if result is None:
        return StopResultDict(
            reason="unknown",
            location=LocationDict(
                event=0, tick=0, function=None, file=None, line=None, address="0x0"
            ),
        )

    response: StopResultDict = StopResultDict(
        reason=result.reason,
        location=_location_to_dict(result.location),
    )

    if result.signal:
        response["signal"] = SignalDict(
            name=result.signal.name,
            meaning=result.signal.meaning,
        )

    if result.breakpoint_id is not None:
        response["breakpoint_id"] = result.breakpoint_id

    return response


def _format_result(result: dict[str, object]) -> str:
    """Format a result as JSON."""
    return json.dumps(result, indent=2)


def _format_error(error: RrMcpError) -> str:
    """Format an error as JSON."""
    error_info: dict[str, object] = {
        "code": type(error).__name__,
        "message": str(error),
    }

    if isinstance(error, TraceNotFoundError):
        error_info["trace"] = error.trace
    elif isinstance(error, SessionNotFoundError):
        error_info["session_id"] = error.session_id

    error_data: ErrorResponseDict = ErrorResponseDict(
        success=False,
        error=error_info,  # type: ignore[typeddict-item]
    )

    return json.dumps(error_data, indent=2)


def _validate_rr_available() -> None:
    """Validate that rr is installed and available."""
    import shutil

    if not shutil.which("rr"):
        raise RrMcpError(
            "rr is not installed or not in PATH. Please install rr from https://rr-project.org/"
        )


def main() -> None:
    """Run the MCP server."""
    _validate_rr_available()
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
