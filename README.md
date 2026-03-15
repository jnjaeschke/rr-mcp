# rr-mcp

MCP (Model Context Protocol) server for orchestrating multi-process [rr](https://rr-project.org/) debugging sessions.

## Overview

rr-mcp enables AI agents (like Claude) to debug multi-process applications using rr's record-and-replay capabilities. It provides:

- **48 specialized debugging tools** with rich descriptions and usage context
- **Dynamic MCP resources** that expose live session state (`rr://sessions/{id}`)
- **Concurrent replay sessions** for debugging different processes simultaneously
- **Full reverse execution** — step, continue, and finish in both directions
- **Multi-process support** — debug exec'd processes (`pid`) or forked-without-exec children (`fork_pid`)
- **Conditional and temporary breakpoints**, watchpoints, and catchpoints (throw/catch/syscall/signal)

**New to rr debugging?** See [RR_DEBUGGING_GUIDE.md](RR_DEBUGGING_GUIDE.md) for concepts, workflows, and best practices.

## Requirements

- Linux (rr only runs on Linux)
- Python 3.11+
- [rr](https://rr-project.org/) installed and in PATH
- An rr recording to debug

## Installation

### Claude Code

```bash
claude mcp add rr-mcp -- uvx rr-mcp
```

### Other MCP clients

Any MCP client that supports stdio servers can run `uvx rr-mcp` (or `pipx run rr-mcp`).

### Installing from source

**For development** (recommended - no reinstall needed after changes):

```bash
git clone https://github.com/jnjaeschke/rr-mcp && cd rr-mcp
uv sync  # Install dependencies

# Configure Claude Code to run from source
claude mcp add rr-mcp -- uv run --directory /absolute/path/to/rr-mcp rr-mcp
```

**For production use**:

```bash
git clone https://github.com/jnjaeschke/rr-mcp && cd rr-mcp
uv tool install .

# Configure Claude Code
claude mcp add rr-mcp -- rr-mcp
```

For other MCP clients, use the appropriate command (`uv run ...` for development or `rr-mcp` for production).

## Usage

Once installed, the server loads automatically when you start your MCP client. Verify it's working by asking:

> "List available rr traces"

Claude will use the `traces_list` tool to show recordings in your `_RR_TRACE_DIR` (defaults to `~/.local/share/rr`).

### MCP Resources

- `rr://guide` — Debugging guide with workflows, tool selection advice, and common pitfalls
- `rr://traces` — Dynamic list of available recordings
- `rr://sessions/{id}` — Current session state (position, location)
- `rr://sessions/{id}/backtrace` — Live call stack for a session

### Tools

48 debugging tools organized into categories. Each tool includes rich descriptions with usage context, parameter documentation, return value format specifications, and workflow guidance.

#### Trace Management (3 tools)

| Tool | Description |
|------|-------------|
| `traces_list` | List all available rr recordings on the system |
| `trace_info` | Get metadata about a trace (creation time, binary, etc.) |
| `trace_processes` | List processes in a trace with PIDs and exec info |

#### Session Lifecycle (3 tools)

| Tool | Description |
|------|-------------|
| `session_create` | Create a replay session for a specific process. Supports `pid` (exec'd processes) and `fork_pid` (forked-without-exec children) |
| `session_list` | List all active replay sessions |
| `session_close` | End a session and free its resources |

#### Execution Control (14 tools)

| Tool | Description |
|------|-------------|
| `continue` / `reverse_continue` | Run forward/backward until breakpoint, signal, or end |
| `step` / `reverse_step` | Step by source lines, into functions |
| `next` / `reverse_next` | Step by source lines, over functions |
| `finish` / `reverse_finish` | Run to end/start of current function |
| `stepi` / `reverse_stepi` | Step by machine instructions, into calls |
| `nexti` / `reverse_nexti` | Step by machine instructions, over calls |
| `run_to_event` | Jump to a specific rr event number |
| `interrupt` | Stop a running program |

#### Breakpoints & Watchpoints (7 tools)

| Tool | Description |
|------|-------------|
| `breakpoint_set` | Set breakpoints by function, file:line, or address. Supports conditional (`condition`) and temporary breakpoints. Pending breakpoints are enabled automatically for unloaded code |
| `breakpoint_delete` | Delete a breakpoint by number |
| `breakpoint_list` | List all breakpoints with their state |
| `breakpoint_enable` / `breakpoint_disable` | Toggle breakpoints on/off |
| `watchpoint_set` | Break on memory writes, reads, or access to an expression |
| `catch` | Catchpoints for C++ `throw`/`catch`, syscalls (optionally filtered by name), and signals |

#### Inspection (14 tools)

| Tool | Description |
|------|-------------|
| `backtrace` | Call stack with optional `full` mode (includes locals per frame) |
| `print` | Evaluate an expression in the current context |
| `locals` | Local variables in the current frame |
| `args` | Function arguments across stack frames |
| `frame_select` | Switch to a different stack frame |
| `registers` | CPU registers (`gp_only` flag filters to general-purpose registers) |
| `examine_memory` | Formatted memory dump (like GDB's `x` command) |
| `when` | Current rr event and tick position |
| `threads_list` / `thread_select` | List and switch between threads |
| `checkpoint_create` | Save execution position for instant return later |
| `checkpoint_restore` | Jump back to a saved checkpoint |
| `checkpoint_delete` / `checkpoint_list` | Manage checkpoints |

#### Signal Handling & Memory Search (2 tools)

| Tool | Description |
|------|-------------|
| `handle_signal` | Configure how GDB handles a signal (stop, pass, print) |
| `find_in_memory` | Search memory range for a byte pattern |

#### Source & Advanced (5 tools)

| Tool | Description |
|------|-------------|
| `source_list` | Show source code around a location with context lines |
| `source_path` | Resolve a filename to its full path |
| `source_files` | List all source files in the program |
| `info` | GDB `info` subcommands (proc mappings, shared libs, symbols, types, signals) |
| `gdb_raw` | Execute an arbitrary GDB command (escape hatch) |

All tools include detailed descriptions visible to Claude. See tool definitions in [src/rr_mcp/server.py](src/rr_mcp/server.py).

## Example Session

```text
Agent: Let me list the available traces.
[calls traces_list]

Agent: I see firefox-0. Let me check what processes are in it.
[calls trace_processes with trace="firefox-0"]

Agent: The main process is PID 12345, and there's a content process
       PID 12350 that crashed with SIGSEGV. Let me debug the content
       process — it forked without exec, so I need fork_pid.
[calls session_create with trace="firefox-0", fork_pid=12350]

Agent: Let me see where we are and get the crash backtrace.
[calls backtrace with session_id="abc123"]

Agent: Frame #3 in HandleEvent dereferences a null mWidget pointer.
       Let me see the source code around that line.
[calls source_list with location="HandleEvent.cpp:142"]

Agent: I'll set a watchpoint on mWidget to find where it was set to null,
       then reverse-continue to catch the moment it happened.
[calls watchpoint_set with expression="this->mWidget", access_type="write"]
[calls reverse_continue]

Agent: The watchpoint triggered at event 84021 in Widget::Destroy().
       Let me check who called this.
[calls backtrace]

Agent: I'll save this position as a checkpoint so I can return here later,
       then continue investigating the caller.
[calls checkpoint_create]
```

## Development

```bash
# Install dev dependencies
uv sync --all-extras

# Run tests (parallel execution with pytest-xdist)
uv run pytest -n auto

# Run tests (sequential)
uv run pytest

# Run linter
uv run ruff check src/ tests/

# Run type checker
uv run mypy src/
```

## License

MIT
