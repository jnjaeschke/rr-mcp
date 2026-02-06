# rr Debugging Guide for AI Agents

## What is rr?

rr is a deterministic record-and-replay debugger. It records program execution once, then allows unlimited replays with full reverse execution capabilities. Think of it as a time machine for debugging.

## Key Concepts

### Events and Ticks

- **Event**: A high-level execution milestone (syscall, signal, etc.). Advancing events moves through the program's major operations.
- **Tick**: Fine-grained instruction counter within an event. Not always available (rr 5.9.0 limitation: tick often shows as 0).
- Use events for navigation, not ticks (ticks are unreliable in many rr versions).

### Deterministic Replay

- Every replay is **identical** - same memory addresses, same timing, same thread schedules.
- This means you can reproduce bugs 100% of the time, unlike live debugging where race conditions or timing may vary.
- Set breakpoints, examine state, then rewind and try different breakpoints - the execution is always the same.

### Multi-Process Debugging

- **Critical limitation**: You can only debug processes that called `exec()`.
- Pure `fork()` without `exec()` creates processes that rr cannot attach to with `-p <PID>`.
- Use `trace_processes` to identify which PIDs are debuggable.

## Typical Debugging Workflows

### Workflow 1: Debugging a Crash

```
1. traces_list → Find available recordings
2. trace_info → Verify it's the right trace
3. session_create → Start debugging (omit pid for main process)
4. continue_ → Run until crash
5. backtrace → See call stack at crash
6. locals / args → Inspect variables in each frame
7. frame_select → Move up/down stack to examine callers
8. print → Evaluate specific expressions
```

### Workflow 2: Finding When Data Corrupts

```
1. session_create → Start session
2. continue_ → Run to point where data is already corrupted
3. backtrace → Confirm you're at the bad state
4. print "my_var" → See corrupted value
5. reverse_continue → Go back to start
6. watchpoint_set expression="my_var" access_type="write" → Break on modifications
7. continue_ → Run forward until watchpoint hits
8. backtrace → See who wrote the bad value
9. reverse_continue → Find earlier writes if needed
```

### Workflow 3: Multi-Process Debugging

```
1. trace_processes → List all processes (check that child has ppid != 0)
2. session_create trace="my-trace" pid=<child_pid> → Debug specific process
3. Set breakpoints in child-specific code
4. continue_ → Run until breakpoint
5. Create second session with different PID to compare states
```

### Workflow 4: Finding Root Cause via Reverse Execution

```
1. session_create → Start
2. continue_ → Run to symptom (crash, assertion, wrong output)
3. breakpoint_set location="suspicious_function" → Mark where to stop
4. reverse_continue → Run backward until breakpoint
5. print / locals → Examine state when function was called
6. step / reverse_step → Step through function in reverse to see data flow
7. Repeat: set earlier breakpoints, reverse_continue, inspect
```

## Tool Selection Guide

### When to Step vs Continue

- **step/next**: Use for detailed line-by-line analysis. Step when you need to understand exactly what each line does.
- **continue**: Use to quickly reach breakpoints. Much faster than stepping thousands of lines.
- **step vs next**: Use `step` to enter function calls, `next` to treat functions as single operations.
- **finish**: Use when you're deep in a function and want to quickly return to the caller.

### When to Use Breakpoints vs Watchpoints

- **Breakpoint**: Stop at a specific code location. Use when you know where to look.
- **Watchpoint**: Stop when data changes. Use when you don't know what code is modifying a variable.
- **Conditional breakpoints**: Combine both - stop at location only when data matches condition.

### When to Use Reverse Execution

- **reverse_continue**: Finding the cause of bad state. Run forward past bug, set breakpoint on suspect, run backward.
- **reverse_step/next**: When you stepped too far forward and want to back up.
- **reverse_finish**: Find where a function was called (function entry point).

## Response Format Reference

### StopResult (from execution commands)

```json
{
  "reason": "end-stepping-range" | "breakpoint-hit" | "signal-received" | "exited-normally" | "exited-signalled",
  "location": {
    "event": 1234,
    "tick": 0,
    "function": "my_function",
    "file": "/path/to/file.cpp",
    "line": 42,
    "address": "0x12345678"
  },
  "signal": {"name": "SIGSEGV", "meaning": "Segmentation fault"},  // if signal-received
  "breakpoint_id": 1  // if breakpoint-hit
}
```

**Common reason values:**

- `"end-stepping-range"` - Normal step/next completed
- `"breakpoint-hit"` - Hit a breakpoint (breakpoint_id will be set)
- `"signal-received"` - Program received signal (signal will be set)
- `"exited-normally"` - Program exited successfully
- `"exited-signalled"` - Program exited due to signal

### Backtrace Frame

```json
{
  "level": 0,  // 0 = innermost
  "func": "function_name",
  "file": "/path/to/file.cpp",
  "line": 42,
  "addr": "0x12345678",
  "locals": [...]  // only if full=true
}
```

### Variable

```json
{
  "name": "my_var",
  "value": "42",
  "type": "int"
}
```

## Common Pitfalls

1. **Not creating a session first**: All debugging commands require a session_id. Create with `session_create`.

2. **Expecting ticks to change**: In rr 5.9.0, ticks are often 0. Use events for navigation.

3. **Setting watchpoints before variables are in scope**: Watchpoints need variables to exist. Set breakpoint first, continue to that location, then set watchpoint.

4. **Forgetting processes need exec()**: If debugging child processes fails with "never exec()ed", the child must call exec() to be debuggable.

5. **Not using reverse execution**: rr's killer feature! When debugging, think "I can always go backward" and use it freely.

6. **Setting too many breakpoints**: Each breakpoint slows execution. Use conditional breakpoints or delete unused ones.

## Best Practices

1. **Start broad, then narrow**: Use `continue` with breakpoints to quickly reach interesting areas, then use `step` for detailed analysis.

2. **Use resources for monitoring**: Check `rr://sessions/{id}` resource to see current state without calling tools.

3. **Leverage determinism**: If a test fails, set aggressive breakpoints and watchpoints - replay cost is zero.

4. **Think backward**: When you find a bug, ask "what caused this?" and use reverse execution to find out.

5. **Multiple sessions**: Create concurrent sessions at different points in the trace to compare states.

6. **Clean up**: Close sessions when done to free resources.

## Quick Reference

### Essential First Steps

```
traces_list                              # What recordings exist?
trace_processes trace="name"             # What processes? (for multi-process)
session_create trace="name" [pid=123]    # Start debugging
```

### Navigation Commands

```
continue_ / reverse_continue             # Run to breakpoint/signal/end
step / reverse_step                      # Line-by-line into calls
next / reverse_next                      # Line-by-line over calls
finish / reverse_finish                  # Until function returns/called
```

### Inspection Commands

```
backtrace [full=true]                    # Call stack [+locals]
locals                                   # Variables in current frame
args                                     # Function parameters
print expression="my_var"                # Evaluate expression
frame_select frame_num=1                 # Switch to caller frame
```

### Breakpoints

```
breakpoint_set location="func"           # Stop at function
breakpoint_set location="file.cpp:42"    # Stop at line
breakpoint_set location="func" condition="x>10"  # Conditional
watchpoint_set expression="my_var" access_type="write"  # Stop on write
```

### Cleanup

```
breakpoint_list                          # See all breakpoints
breakpoint_delete breakpoint_id=1        # Remove breakpoint
session_close session_id="..."           # End session
```
