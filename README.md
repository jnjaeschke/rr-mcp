# rr-mcp

MCP (Model Context Protocol) server for orchestrating multi-process [rr](https://rr-project.org/) debugging sessions.

## Overview

rr-mcp enables AI agents (like Claude) to debug multi-process applications using rr's record-and-replay capabilities. It provides:

- **48 specialized debugging tools** with rich descriptions and usage context
- **Dynamic MCP resources** that expose live session state (`rr://sessions/{id}`)
- **Concurrent replay sessions** for debugging different processes simultaneously
- **Full reverse execution** - rr's killer feature for finding root causes
- **Comprehensive documentation** embedded in tool descriptions for AI understanding

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

### Claude Desktop

Add to your [MCP config](https://modelcontextprotocol.io/quickstart/user):

```json
{
  "mcpServers": {
    "rr-mcp": {
      "command": "uvx",
      "args": ["rr-mcp"]
    }
  }
}
```

### Other MCP clients

Any MCP client that supports stdio servers can run `uvx rr-mcp` (or `pipx run rr-mcp`).

## Usage

Once installed, the server loads automatically when you start your MCP client. Verify it's working by asking:

> "List available rr traces"

Claude will use the `traces_list` tool to show recordings in your `_RR_TRACE_DIR` (defaults to `~/.local/share/rr`).

### Available Tools & Resources

**48 debugging tools** organized into categories. Each tool includes:

- Rich descriptions with usage context
- Parameter documentation with examples
- Return value format specifications
- Workflow guidance and best practices

#### MCP Resources

- `rr://guide` - Debugging guide with workflows, tool selection advice, and common pitfalls
- `rr://traces` - Dynamic list of available recordings
- `rr://sessions/{id}` - Current session state (position, location)
- `rr://sessions/{id}/backtrace` - Live call stack for a session

#### Tool Categories

- **Trace Management** (3 tools): Discover and inspect recordings
- **Session Lifecycle** (3 tools): Create and manage replay sessions
- **Execution Control** (14 tools): Step, continue, finish (forward & reverse), run to event, interrupt
- **Breakpoints & Watchpoints** (7 tools): Set, list, enable, disable, catchpoints
- **Inspection** (14 tools): Backtrace, locals, args, registers, memory, threads, checkpoints
- **Signal & Memory** (2 tools): Signal handling, memory search
- **Source & Advanced** (5 tools): List source code, info queries, raw GDB commands

All tools include detailed descriptions visible to Claude. See tool definitions in [src/rr_mcp/server.py](src/rr_mcp/server.py).

## Example Session

```text
Agent: Let me list the available traces.
[calls traces_list]

Agent: I see firefox-0. Let me check what processes are in it.
[calls trace_processes with trace="firefox-0"]

Agent: Process 12350 crashed with SIGSEGV. Let me debug it.
[calls session_create with trace="firefox-0", pid=12350]

Agent: Let me see where we crashed.
[calls backtrace with session_id="abc123"]

Agent: I want to find where this pointer became null.
[calls breakpoint_set with location="*0x7f1234567890"]
[calls reverse_continue]
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
