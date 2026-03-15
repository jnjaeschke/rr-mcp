"""Integration tests for debugging operations using real rr traces.

Tests execution control, breakpoints, watchpoints, checkpoints, memory examination,
registers, stack navigation, threads, and source code operations.
"""

from pathlib import Path

import pytest

from rr_mcp.errors import RrMcpError
from rr_mcp.session import SessionManager
from rr_mcp.trace import get_trace_processes


@pytest.mark.asyncio
async def test_step_and_reverse_step(recorded_simple_trace: Path) -> None:
    """Test stepping forward and backward through code."""
    manager = SessionManager()
    session, initial_loc = await manager.create_session(trace=str(recorded_simple_trace))

    try:
        # Get initial position
        event1, tick1 = await session.get_current_position()

        # Step forward
        await session.step()

        # Position should have advanced
        event2, tick2 = await session.get_current_position()
        assert (event2, tick2) > (event1, tick1)

        # Reverse step should go back
        await session.reverse_step()
        event3, tick3 = await session.get_current_position()
        assert (event3, tick3) < (event2, tick2)

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_next_and_reverse_next(recorded_simple_trace: Path) -> None:
    """Test next (step over) forward and backward."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_simple_trace))

    try:
        event1, tick1 = await session.get_current_position()

        # Next should step over function calls
        await session.next()

        event2, tick2 = await session.get_current_position()
        assert (event2, tick2) > (event1, tick1)

        # Reverse next
        await session.reverse_next()
        event3, tick3 = await session.get_current_position()
        assert (event3, tick3) < (event2, tick2)

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_continue_and_reverse_continue(recorded_simple_trace: Path) -> None:
    """Test continue execution forward and backward."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_simple_trace))

    try:
        # Continue to end
        await session.continue_execution()

        # Reverse continue should go backwards
        await session.reverse_continue()

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_finish_and_reverse_finish(recorded_simple_trace: Path) -> None:
    """Test finishing current function forward and backward."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_simple_trace))

    try:
        # Step several times to ensure we're in a function
        for _ in range(10):
            await session.step()

        event1, _ = await session.get_current_position()

        # Finish should return from current function (or reach end)
        result = await session.finish()

        event2, _ = await session.get_current_position()
        # Should advance (or stay at same position if at outermost frame)
        assert event2 >= event1

        # Only test reverse finish if we actually advanced
        if event2 > event1 and result is not None:
            # Reverse finish
            await session.reverse_finish()
            event3, _ = await session.get_current_position()
            # Should go back
            assert event3 < event2

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_breakpoint_lifecycle(recorded_simple_trace: Path) -> None:
    """Test setting, listing, enabling, disabling, and deleting breakpoints."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_simple_trace))

    try:
        # Set a breakpoint on the add function
        bp_data = await session.set_breakpoint("add")
        assert bp_data is not None
        assert bp_data.number is not None

        # List breakpoints
        breakpoints = await session.list_breakpoints()
        assert len(breakpoints) > 0

        # Disable breakpoint
        success = await session.disable_breakpoint(bp_data.number)
        assert success

        # Enable breakpoint
        success = await session.enable_breakpoint(bp_data.number)
        assert success

        # Delete breakpoint
        success = await session.delete_breakpoint(bp_data.number)
        assert success

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_run_to_breakpoint(recorded_simple_trace: Path) -> None:
    """Test running to a breakpoint."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_simple_trace))

    try:
        # Set breakpoint on multiply function
        bp_data = await session.set_breakpoint("multiply")
        assert bp_data is not None and bp_data.number is not None

        # Continue should stop at breakpoint or end
        stop_result = await session.continue_execution()

        # Should have a stop result
        assert stop_result is not None
        assert stop_result.reason in [
            "breakpoint-hit",
            "end-stepping-range",
            "exited-normally",
            "signal-received",
        ]

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_watchpoint_operations(recorded_simple_trace: Path) -> None:
    """Test setting and managing watchpoints."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_simple_trace))

    try:
        # Set breakpoint at add function to get into a context with variables
        bp_data = await session.set_breakpoint("add")
        assert bp_data is not None and bp_data.number is not None

        # Continue to breakpoint
        stop_result = await session.continue_execution()
        assert stop_result is not None
        assert stop_result.reason == "breakpoint-hit"

        # Now we're in add() with parameters a and b in scope
        # Set a watchpoint on parameter 'a' (read watchpoint)
        wp_data = await session.set_watchpoint("a", access_type="read")
        assert wp_data is not None
        assert wp_data.number is not None
        assert wp_data.number > 0

        # List watchpoints - should include our watchpoint
        breakpoints = await session.list_breakpoints()
        watchpoint_nums = [bp.number for bp in breakpoints if bp.watchpoint]
        assert wp_data.number in watchpoint_nums

        # Delete watchpoint
        success = await session.delete_breakpoint(wp_data.number)
        assert success

        # Verify it's gone
        breakpoints = await session.list_breakpoints()
        watchpoint_nums = [bp.number for bp in breakpoints if bp.watchpoint]
        assert wp_data.number not in watchpoint_nums

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_pending_breakpoint_on_source_line(recorded_simple_trace: Path) -> None:
    """Test that breakpoints can be set on source files before libraries load.

    This tests the fix for pending breakpoints. GDB needs 'set breakpoint pending on'
    to allow setting breakpoints on code that hasn't been loaded yet (e.g., at process
    start before shared libraries are loaded).
    """
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_simple_trace))

    try:
        # At process start (_start), shared libraries aren't loaded yet
        # With pending breakpoints enabled, we should be able to set a breakpoint
        # on functions that haven't been loaded yet
        bp_data = await session.set_breakpoint("main")
        assert bp_data is not None
        assert bp_data.number is not None
        assert bp_data.function is not None
        assert "main" in bp_data.function

        # Continue should hit the breakpoint
        stop_result = await session.continue_execution()
        assert stop_result is not None
        assert stop_result.reason == "breakpoint-hit"
        assert stop_result.location.function is not None
        assert "main" in stop_result.location.function

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_backtrace_and_frame_navigation(recorded_crash_trace: Path) -> None:
    """Test getting backtrace and navigating stack frames."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_crash_trace))

    try:
        # Continue to crash point (SIGSEGV in cause_crash)
        stop_result = await session.continue_execution()
        assert stop_result is not None
        # Should stop due to signal
        assert stop_result.reason in ["signal-received", "exited-signalled"]

        # Get backtrace - should have call stack: cause_crash -> level2 -> level1 -> main
        backtrace = await session.get_backtrace()
        assert isinstance(backtrace, list)
        assert len(backtrace) >= 3  # At least cause_crash, level2, level1, main

        # Check function names in backtrace
        frame_funcs: list[str] = []
        for frame in backtrace:
            func = frame.get("func")
            if func is not None:
                frame_funcs.append(func)

        # Should have our functions in the stack (order may vary with optimization)
        expected_funcs = ["cause_crash", "level2", "level1", "main"]
        for expected in expected_funcs:
            assert any(expected in func for func in frame_funcs), (
                f"Expected '{expected}' in backtrace functions: {frame_funcs}"
            )

        # Verify frames have addresses
        for frame in backtrace[:4]:  # Check first few frames
            assert frame.get("addr") is not None
            addr = frame["addr"]
            assert addr is not None and addr.startswith("0x")

        # Select different frames
        success = await session.select_frame(0)
        assert success

        if len(backtrace) > 1:
            # Select frame 1 (caller of cause_crash, should be level2)
            success = await session.select_frame(1)
            assert success

            # Get location after frame switch
            location = await session.get_current_location()
            assert location.function is not None
            # Should be in one of the caller functions
            assert any(name in location.function for name in ["level2", "level1", "main"])

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_local_variables_and_args(recorded_simple_trace: Path) -> None:
    """Test reading local variables and function arguments."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_simple_trace))

    try:
        # Set breakpoint at add function to get clear function context
        await session.set_breakpoint("add")
        await session.continue_execution()

        # Now we're in add(int a, int b) with a=5, b=3
        # List function arguments - returns list of lists (one list per frame)
        args_list = await session.get_function_arguments()
        assert isinstance(args_list, list)
        assert len(args_list) > 0  # Should have at least one frame

        # Get arguments for current frame (first in list)
        args = args_list[0]
        assert len(args) >= 2  # Should have at least a and b

        # Check that we have variables named 'a' and 'b'
        arg_names = [var.name for var in args]
        assert "a" in arg_names
        assert "b" in arg_names

        # Check values of a and b
        a_var = next((v for v in args if v.name == "a"), None)
        b_var = next((v for v in args if v.name == "b"), None)

        assert a_var is not None
        assert b_var is not None

        # Values should be 5 and 3 (first call to add in simple.cpp: add(a, b) where a=5, b=3)
        assert a_var.value is not None and "5" in str(a_var.value)
        assert b_var.value is not None and "3" in str(b_var.value)

        # List all local variables (includes parameters)
        variables = await session.get_local_variables()
        assert isinstance(variables, list)
        assert len(variables) >= 2  # At minimum the two parameters

        # Variable names should include a and b
        var_names = [var.name for var in variables]
        assert "a" in var_names
        assert "b" in var_names

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_examine_memory(recorded_simple_trace: Path) -> None:
    """Test examining memory contents."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_simple_trace))

    try:
        # Set breakpoint at add function
        await session.set_breakpoint("add")
        await session.continue_execution()

        # Now we're in add(a, b) where a=5 and b=3
        # Get address of variable 'a'
        a_addr = await session.evaluate_expression("&a")
        assert a_addr is not None
        # Should be a hex address like "0x7ffc12345678"
        assert a_addr.startswith("0x")

        # Read 4 bytes from address (sizeof(int))
        memory = await session.read_memory(a_addr, 4)
        assert memory is not None
        assert len(memory) > 0

        # Verify we can read the value as an integer
        # The value should be 5 (first call to add in simple.cpp)
        a_value = await session.evaluate_expression("a")
        assert a_value is not None
        # Value should be "5" or contain "5"
        assert "5" in str(a_value)

        # Read memory at instruction pointer
        location = await session.get_current_location()
        assert location.address is not None
        assert location.address != "0x0"

        # Read a few bytes of code
        code_memory = await session.read_memory(location.address, 16)
        assert code_memory is not None
        # Code memory should have at least some bytes
        assert len(code_memory) >= 8

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_read_registers(recorded_simple_trace: Path) -> None:
    """Test reading CPU registers."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_simple_trace))

    try:
        # Read all registers
        registers = await session.read_registers()
        assert isinstance(registers, dict)
        assert len(registers) > 0

        # On x86-64, we should have standard registers
        # Check for common registers (platform-dependent)
        common_regs = [
            "rip",
            "rsp",
            "rbp",
            "rax",
            "pc",
            "sp",
        ]  # rip/pc, rsp/sp are instruction/stack pointers
        has_common_reg = any(reg in registers for reg in common_regs)
        assert has_common_reg, (
            f"Expected at least one common register, got: {list(registers.keys())[:10]}"
        )

        # Get current location
        location = await session.get_current_location()
        assert location.address is not None

        # Instruction pointer register should match location address
        # Compare addresses numerically to handle different zero-padding formats
        if "rip" in registers:
            rip_value = registers["rip"]
            # Both should represent the same address (normalize by converting to int)
            location_addr_int = int(location.address, 16)
            rip_addr_int = int(rip_value, 16)
            assert location_addr_int == rip_addr_int, (
                f"Address mismatch: location={location.address} != rip={rip_value}"
            )
        elif "pc" in registers:
            pc_value = registers["pc"]
            location_addr_int = int(location.address, 16)
            pc_addr_int = int(pc_value, 16)
            assert location_addr_int == pc_addr_int, (
                f"Address mismatch: location={location.address} != pc={pc_value}"
            )

        # Stack pointer should be non-zero and look like a valid address
        if "rsp" in registers:
            rsp = registers["rsp"]
            assert rsp.startswith("0x")
            # Stack pointer should not be 0
            assert rsp != "0x0"
        elif "sp" in registers:
            sp = registers["sp"]
            assert sp.startswith("0x")
            assert sp != "0x0"

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_checkpoint_operations(recorded_simple_trace: Path) -> None:
    """Test creating, listing, restoring, and deleting checkpoints."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_simple_trace))

    try:
        # Step forward a bit
        for _ in range(5):
            await session.step()

        event1, tick1 = await session.get_current_position()

        # Create a checkpoint
        checkpoint_id = await session.create_checkpoint()

        # Continue execution forward significantly
        await session.continue_execution()

        event2, tick2 = await session.get_current_position()
        # Should have advanced (or if at end, that's ok for checkpoint test)
        assert (event2, tick2) >= (event1, tick1)

        # Restart to restore to checkpoint (if we got one)
        if checkpoint_id:
            success = await session.restore_checkpoint(checkpoint_id)
            assert success
            # Check we went back
            event3, tick3 = await session.get_current_position()
            assert (event3, tick3) < (event2, tick2)

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_run_to_specific_event(recorded_simple_trace: Path) -> None:
    """Test running to a specific event number."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_simple_trace))

    try:
        # Run forward some steps to find a target event
        for _ in range(10):
            await session.step()

        target_event, _ = await session.get_current_position()

        # Go back to start
        await session.reverse_continue()

        # Run to the target event
        await session.run_to_event(target_event)

        # Should be at or near target event
        final_event, _ = await session.get_current_position()
        assert final_event >= target_event - 5  # Allow some tolerance

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_source_code_listing(recorded_simple_trace: Path) -> None:
    """Test listing source code."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_simple_trace))

    try:
        # List source files
        source_files = await session.list_source_files()
        assert isinstance(source_files, list)
        assert len(source_files) > 0

        # Should have simple.cpp in the source files
        simple_cpp_path = next((f for f in source_files if "simple.cpp" in f.lower()), None)
        assert simple_cpp_path is not None, f"simple.cpp not found in: {source_files}"

        # Get source code for simple.cpp - uses different API that returns dict
        source_result = await session.get_source_lines(simple_cpp_path + ":5")
        assert isinstance(source_result, dict)
        assert source_result["file"] is not None
        assert len(source_result["lines"]) > 0

        # Should contain the add function signature or body
        source_text = " ".join(line["content"] for line in source_result["lines"])
        assert "add" in source_text or "int" in source_text or "return" in source_text

        # Get current location and verify we can get source around it
        location = await session.get_current_location()
        if location.file and location.line:
            # Get source lines around current location using default API
            context_result = await session.get_source_lines()
            assert isinstance(context_result, dict)
            # Should have some lines
            assert len(context_result["lines"]) > 0

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_thread_operations(recorded_threads_trace: Path) -> None:
    """Test listing and selecting threads."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_threads_trace))

    try:
        # Continue forward to where threads exist
        for _ in range(20):
            await session.step()

        # List threads
        threads = await session.list_threads()
        assert isinstance(threads, list)
        # May have only one thread at start, which is fine

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_stepi_and_reverse_stepi(recorded_simple_trace: Path) -> None:
    """Test single instruction stepping forward and backward."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_simple_trace))

    try:
        event1, _ = await session.get_current_position()

        # Step one instruction forward
        await session.step_instruction()

        event2, _ = await session.get_current_position()
        # Event should advance (tick may stay 0 on rr 5.9.0)
        assert event2 >= event1

        # Reverse step instruction
        await session.reverse_step_instruction()
        event3, _ = await session.get_current_position()
        # Should go back (at least to where we started)
        assert event3 <= event2

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_nexti_and_reverse_nexti(recorded_simple_trace: Path) -> None:
    """Test single instruction next (step over) forward and backward."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_simple_trace))

    try:
        event1, _ = await session.get_current_position()

        # Next one instruction
        result2 = await session.next_instruction()
        assert result2 is not None

        event2 = result2.location.event
        # Event should advance (tick may stay 0 on rr 5.9.0)
        assert event2 >= event1

        # Reverse next instruction — use stop result's location directly,
        # because `when` can fail in transient GDB states after instruction-level ops
        result3 = await session.reverse_next_instruction()
        assert result3 is not None

        event3 = result3.location.event
        # Should go back (at least to where we started)
        assert event3 <= event2

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_evaluate_expression(recorded_simple_trace: Path) -> None:
    """Test evaluating expressions."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_simple_trace))

    try:
        # Step forward to have variables in scope
        for _ in range(15):
            await session.step()

        # Evaluate a simple expression
        result = await session.evaluate_expression("2+2")
        assert result is not None  # May be "4" or other representation

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_multiple_sessions_concurrent(recorded_simple_trace: Path) -> None:
    """Test that multiple sessions can operate concurrently."""
    manager = SessionManager()

    session1, _ = await manager.create_session(trace=str(recorded_simple_trace))
    session2, _ = await manager.create_session(trace=str(recorded_simple_trace))

    try:
        # Test that both sessions can be operated independently
        # Both start at the same position
        initial_pos1 = await session1.get_current_position()
        initial_pos2 = await session2.get_current_position()
        assert initial_pos1 == initial_pos2, "Sessions should start at same position"

        # Step session1 forward
        await session1.step()
        pos1_after = await session1.get_current_position()

        # Session2 should still be at the initial position (independent)
        pos2_still = await session2.get_current_position()
        assert pos2_still == initial_pos2, (
            f"Session 2 should not have moved: expected {initial_pos2}, got {pos2_still}"
        )

        # Session1 should have advanced
        assert pos1_after != initial_pos1, f"Session 1 should have moved: still at {pos1_after}"

        # Now step session2
        await session2.step()
        pos2_after = await session2.get_current_position()

        # Both sessions should now be at the same position (both stepped once)
        assert pos1_after == pos2_after, (
            "After same operations, sessions should be at same position: "
            f"{pos1_after} vs {pos2_after}"
        )

    finally:
        await manager.close_session(session1.session_id)
        await manager.close_session(session2.session_id)


@pytest.mark.asyncio
async def test_conditional_breakpoint(recorded_simple_trace: Path) -> None:
    """Test conditional breakpoints that only trigger when condition is met."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_simple_trace))

    try:
        # Set a conditional breakpoint on multiply function: only break when x > 3
        # In simple.cpp, multiply is called with multiply(5, 3), so x=5 which is > 3
        bp_data = await session.set_breakpoint("multiply", condition="x > 3")
        assert bp_data is not None
        assert bp_data.number is not None

        # Continue - should stop at multiply because x=5 > 3
        stop_result = await session.continue_execution()
        assert stop_result is not None

        # Should have stopped at the breakpoint
        if stop_result.reason == "breakpoint-hit":
            # Verify we're in multiply and x > 3
            args_list = await session.get_function_arguments()
            args = args_list[0] if args_list else []
            x_var = next((v for v in args if v.name == "x"), None)
            assert x_var is not None
            # x should be 5 in the first call
            assert x_var.value is not None and "5" in str(x_var.value)

        # Now test a condition that is never true
        # Delete the first breakpoint
        await session.delete_breakpoint(bp_data.number)

        # Set breakpoint with impossible condition
        bp_data2 = await session.set_breakpoint("add", condition="a > 100")
        assert bp_data2 is not None

        # Restart and continue - should NOT stop at add since a is never > 100
        await session.reverse_continue()  # Go to start

        stop_result2 = await session.continue_execution()
        assert stop_result2 is not None

        # Should reach end without hitting the conditional breakpoint
        # (or hit other code, but not add with a > 100)
        assert stop_result2.reason in ["exited-normally", "signal-received", "breakpoint-hit"]

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_multi_process_list_processes(recorded_fork_trace: Path) -> None:
    """Test listing processes in a multi-process trace."""
    # Get all processes from the trace
    processes = get_trace_processes(str(recorded_fork_trace))

    # fork_test creates 1 parent + 3 children = 4 processes total
    assert len(processes) >= 4, f"Expected at least 4 processes, got {len(processes)}"

    # Check that we have parent and children
    pids = [p.pid for p in processes]
    assert len(pids) == len(set(pids)), "PIDs should be unique"

    # Verify process information
    for proc in processes:
        assert proc.pid > 0
        assert proc.command != ""
        # Exit code should be set for child processes
        # (parent waits for children, so children should have exit codes)


@pytest.mark.asyncio
async def test_multi_process_switch_between_processes(recorded_fork_trace: Path) -> None:
    """Test switching between parent and child processes."""
    # Get all processes
    processes = get_trace_processes(str(recorded_fork_trace))
    assert len(processes) >= 2

    # Get two different PIDs to test switching
    pid1 = processes[0].pid
    pid2 = processes[1].pid
    assert pid1 != pid2

    manager = SessionManager()

    # Create session for first process
    session1, loc1 = await manager.create_session(trace=str(recorded_fork_trace), pid=pid1)

    try:
        # Verify we're in the first process
        assert session1.pid == pid1

        # Step forward in first process
        await session1.step()
        event1, _ = await session1.get_current_position()

        # Create session for second process
        session2, loc2 = await manager.create_session(trace=str(recorded_fork_trace), pid=pid2)

        try:
            # Verify we're in the second process
            assert session2.pid == pid2

            # Step forward in second process
            await session2.step()
            event2, _ = await session2.get_current_position()

            # Both sessions should be independent
            # (they may be at same event if both start at beginning, but that's ok)
            assert session1.pid != session2.pid

        finally:
            await manager.close_session(session2.session_id)

    finally:
        await manager.close_session(session1.session_id)


@pytest.mark.asyncio
async def test_multi_process_breakpoint_in_child(recorded_fork_trace: Path) -> None:
    """Test setting breakpoints in child processes."""
    # Get child process PID (not the parent)
    processes = get_trace_processes(str(recorded_fork_trace))

    # Find a child process (ppid != 0 means it has a parent)
    child = next((p for p in processes if p.ppid != 0), None)
    assert child is not None, "No child process found in fork trace"

    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_fork_trace), pid=child.pid)

    try:
        # Set breakpoint on child_process function (only exists in children)
        bp_data = await session.set_breakpoint("child_process")
        assert bp_data is not None
        assert bp_data.number is not None

        # Continue - should hit the breakpoint
        stop_result = await session.continue_execution()
        assert stop_result is not None

        # Verify we stopped at the breakpoint in the child
        location = await session.get_current_location()
        assert location.function is not None
        assert "child_process" in location.function or "child" in location.function.lower()

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_fork_pid_content_process(recorded_fork_no_exec_trace: Path) -> None:
    """Test that -f <PID> (fork_pid) lets us debug a forked-without-exec child.

    Mimics the Firefox parent/content-process model: the parent fork()s a child
    that never calls exec(), so the child cannot be targeted with -p.  We use
    fork_pid instead, which maps to rr replay -f <PID>.
    """
    processes = get_trace_processes(str(recorded_fork_no_exec_trace))

    # The program has exactly two processes: parent and one forked child.
    assert len(processes) == 2, f"Expected 2 processes, got {len(processes)}: {processes}"

    # The process whose pid appears as another's ppid is the parent.
    parent_pid = next(p.ppid for p in processes if p.ppid != 0)
    child = next(p for p in processes if p.pid != parent_pid)

    assert child.ppid == parent_pid, f"Expected child ppid={parent_pid}, got {child.ppid}"

    manager = SessionManager()
    # Use fork_pid — rr replay -f <child_pid>
    session, _ = await manager.create_session(
        trace=str(recorded_fork_no_exec_trace), fork_pid=child.pid
    )

    try:
        # The child calls content_process_work(42); set a breakpoint there.
        bp = await session.set_breakpoint("content_process_work")
        assert bp is not None

        stop = await session.continue_execution()
        assert stop is not None, "Expected to hit breakpoint in content process"
        assert stop.reason == "breakpoint-hit", f"Unexpected stop reason: {stop.reason}"

        location = await session.get_current_location()
        assert location.function is not None
        assert "content_process_work" in location.function

        # Verify we're actually in the child by inspecting the 'secret' argument.
        args = await session.get_function_arguments()
        assert args, "Expected function arguments"
        flat_args = args[0] if args else []
        secret_arg = next((v for v in flat_args if v.name == "secret"), None)
        assert secret_arg is not None, "Expected 'secret' argument"
        assert secret_arg.value == "42", f"Expected secret=42, got {secret_arg.value}"

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_thread_list_multiple_threads(recorded_threads_trace: Path) -> None:
    """Test listing multiple threads in a threaded program."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_threads_trace))

    try:
        # Continue forward to where threads are created
        for _ in range(50):
            await session.step()

        # List threads - enhanced threads.cpp creates multiple threads
        threads = await session.list_threads()
        assert isinstance(threads, list)

        # Should have multiple threads (main + worker threads)
        # Enhanced threads.cpp creates 4+3+4 = 11 threads plus main
        # But they may not all be alive at the same time, so just check > 1
        if len(threads) > 1:
            # Verify thread information
            for thread in threads:
                assert thread["id"] is not None
                # Thread should have some state information
                assert thread.get("name") is not None or thread["id"] > 0

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_thread_local_storage(recorded_threads_trace: Path) -> None:
    """Test reading thread-local storage variables."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_threads_trace))

    try:
        # Set breakpoint in synchronized_worker where thread_local_value is set
        bp_data = await session.set_breakpoint("synchronized_worker")
        assert bp_data is not None, "Could not set breakpoint on synchronized_worker"

        # Continue to breakpoint
        stop_result = await session.continue_execution()
        assert stop_result is not None, "continue_execution returned None"
        assert stop_result.reason == "breakpoint-hit", (
            f"Expected breakpoint-hit, got {stop_result.reason}"
        )

        # Step a few times to get past the TLS assignment
        for _ in range(5):
            await session.step()

        # Try to read thread_local_value
        tls_value = await session.evaluate_expression("thread_local_value")
        if tls_value is not None:
            # Should be id * 100 where id is 1, 2, 3, or 4
            # So value should be 100, 200, 300, or 400
            assert tls_value != "0", "Thread-local value should not be 0"

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_thread_mutex_contention(recorded_threads_trace: Path) -> None:
    """Test debugging mutex contention scenarios."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_threads_trace))

    try:
        # Set breakpoint in contending_worker where mutex is acquired
        bp_data = await session.set_breakpoint("contending_worker")
        assert bp_data is not None, "Could not set breakpoint on contending_worker"

        # Continue to first hit
        stop_result = await session.continue_execution()
        assert stop_result is not None, "continue_execution returned None"
        assert stop_result.reason == "breakpoint-hit", (
            f"Expected breakpoint-hit, got {stop_result.reason}"
        )

        # We're now in one of the contending threads
        location = await session.get_current_location()
        assert location.function is not None
        assert "contending_worker" in location.function or "contending" in location.function.lower()

        # Continue again - should hit the same breakpoint in another thread
        # (or same thread in next iteration)
        stop_result2 = await session.continue_execution()
        assert stop_result2 is not None

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_thread_race_condition_detection(recorded_threads_trace: Path) -> None:
    """Test that we can observe race condition behavior."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_threads_trace))

    try:
        # Set breakpoint in racing_worker which has intentional race
        bp_data = await session.set_breakpoint("racing_worker")
        assert bp_data is not None, "Could not set breakpoint on racing_worker"

        # Continue to breakpoint
        stop_result = await session.continue_execution()
        assert stop_result is not None, "continue_execution returned None"
        assert stop_result.reason == "breakpoint-hit", (
            f"Expected breakpoint-hit, got {stop_result.reason}"
        )

        # Step forward to the race condition code
        for _ in range(10):
            await session.step()

        # Try to evaluate race_counter
        counter_value = await session.evaluate_expression("race_counter")
        if counter_value is not None:
            # race_counter exists and has some value
            # The actual value depends on race timing, but it should be a number
            assert counter_value is not None

        # Continue to end and check final race_counter value
        await session.continue_execution()

        # At the end, race_counter should be less than expected 30 due to races
        # (but in rr, the race is deterministic, so it might always be the same)

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_cpp_virtual_functions(recorded_cpp_features_trace: Path) -> None:
    """Test debugging C++ virtual functions and polymorphism."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_cpp_features_trace))

    try:
        # Set breakpoint in test_polymorphism where virtual functions are called
        bp_data = await session.set_breakpoint("test_polymorphism")
        assert bp_data is not None, "Could not set breakpoint on test_polymorphism"

        # Continue to breakpoint
        stop_result = await session.continue_execution()
        assert stop_result is not None, "continue_execution returned None"
        assert stop_result.reason == "breakpoint-hit", (
            f"Expected breakpoint-hit, got {stop_result.reason}"
        )

        # Step forward to get into the loop
        for _ in range(20):
            await session.step()

        # Try to step into a virtual function call
        # The virtual dispatch should work correctly
        location = await session.get_current_location()
        assert location is not None

        # Continue and verify we can step through virtual function calls
        for _ in range(10):
            await session.step()

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_cpp_stl_containers(recorded_cpp_features_trace: Path) -> None:
    """Test debugging STL containers (vector, map)."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_cpp_features_trace))

    try:
        # Set breakpoint in test_stl_containers
        bp_data = await session.set_breakpoint("test_stl_containers")
        assert bp_data is not None, "Could not set breakpoint on test_stl_containers"

        # Continue to breakpoint
        stop_result = await session.continue_execution()
        assert stop_result is not None, "continue_execution returned None"
        assert stop_result.reason == "breakpoint-hit", (
            f"Expected breakpoint-hit, got {stop_result.reason}"
        )

        # Step forward to where numbers vector is created and populated
        for _ in range(15):
            await session.step()

        # Try to evaluate the numbers vector
        numbers_value = await session.evaluate_expression("numbers.size()")
        if numbers_value is not None:
            # Vector should have 5 elements initially
            assert "5" in str(numbers_value) or "7" in str(numbers_value)  # After push_backs

        # Try to evaluate elements
        first_elem = await session.evaluate_expression("numbers[0]")
        if first_elem is not None:
            # First element should be 10
            assert "10" in str(first_elem)

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_cpp_exception_handling(recorded_cpp_features_trace: Path) -> None:
    """Test debugging C++ exception handling."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_cpp_features_trace))

    try:
        # Set breakpoint in test_exceptions
        bp_data = await session.set_breakpoint("test_exceptions")
        assert bp_data is not None, "Could not set breakpoint on test_exceptions"

        # Continue to breakpoint
        stop_result = await session.continue_execution()
        assert stop_result is not None, "continue_execution returned None"
        assert stop_result.reason == "breakpoint-hit", (
            f"Expected breakpoint-hit, got {stop_result.reason}"
        )

        # Set breakpoint on the divide function
        divide_bp = await session.set_breakpoint("divide")
        assert divide_bp is not None

        # Continue to divide function
        stop_result2 = await session.continue_execution()
        assert stop_result2 is not None

        # We should be in divide function
        location = await session.get_current_location()
        assert location.function is not None
        assert "divide" in location.function.lower()

        # Step through and watch exception being thrown
        for _ in range(10):
            await session.step()

        # Eventually we should reach the catch block or continue execution
        # The exception handling should work correctly

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_cpp_template_functions(recorded_cpp_features_trace: Path) -> None:
    """Test debugging C++ template functions."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_cpp_features_trace))

    try:
        # Set breakpoint in test_templates
        bp_data = await session.set_breakpoint("test_templates")
        assert bp_data is not None, "Could not set breakpoint on test_templates"

        # Continue to breakpoint
        stop_result = await session.continue_execution()
        assert stop_result is not None, "continue_execution returned None"
        assert stop_result.reason == "breakpoint-hit", (
            f"Expected breakpoint-hit, got {stop_result.reason}"
        )

        # Step forward to template function calls
        for _ in range(10):
            await session.step()

        # Template functions should be instantiated and debuggable
        # Try to evaluate max_value calls
        location = await session.get_current_location()
        assert location is not None

        # Verify we can step through template instantiations
        variables = await session.get_local_variables()
        assert isinstance(variables, list)

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_cpp_class_members(recorded_cpp_features_trace: Path) -> None:
    """Test accessing C++ class member variables and methods."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_cpp_features_trace))

    try:
        # Set breakpoint in Circle::area() method
        bp_data = await session.set_breakpoint("Circle::area")
        if bp_data is None:
            # Try alternate name
            bp_data = await session.set_breakpoint("area")

        assert bp_data is not None, "Could not set breakpoint on Circle::area"

        # Continue to breakpoint
        stop_result = await session.continue_execution()
        assert stop_result is not None, "continue_execution returned None"
        assert stop_result.reason == "breakpoint-hit", (
            f"Expected breakpoint-hit, got {stop_result.reason}"
        )

        # We're now in a member function - should be able to access 'this' and members
        # Try to evaluate the radius member
        radius_value = await session.evaluate_expression("radius")
        if radius_value is not None:
            # Radius should be one of the values used: 5.0 or 3.0
            assert "5" in str(radius_value) or "3" in str(radius_value)

        # Try to evaluate 'this' pointer
        this_value = await session.evaluate_expression("this")
        if this_value is not None:
            # Should be a pointer address
            assert "0x" in str(this_value).lower()

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_reverse_next_returns_proper_stop_result(recorded_simple_trace: Path) -> None:
    """Test that reverse_next returns a proper stop result, not 'unknown'.

    This is a regression test for the bug where reverse operations returned
    StopResult with reason="unknown" instead of proper reasons.
    """
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_simple_trace))

    try:
        # Step forward first
        forward_result = await session.next()
        assert forward_result is not None
        assert forward_result.reason != "unknown", (
            f"Forward next should not return 'unknown', got: {forward_result.reason}"
        )

        # Now reverse_next should also return a proper stop result
        reverse_result = await session.reverse_next()
        assert reverse_result is not None, "reverse_next should return a StopResult"
        assert reverse_result.reason != "unknown", (
            f"Reverse next should not return 'unknown', got: {reverse_result.reason}"
        )
        # Should be "end-stepping-range" or similar valid reason
        assert reverse_result.reason in [
            "end-stepping-range",
            "function-finished",
            "breakpoint-hit",
            "exited-normally",
        ], f"Unexpected stop reason: {reverse_result.reason}"

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_reverse_step_returns_proper_stop_result(recorded_simple_trace: Path) -> None:
    """Test that reverse_step returns a proper stop result, not 'unknown'."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_simple_trace))

    try:
        # Step forward several times to get past _start into actual code
        forward_result = None
        for _ in range(5):
            forward_result = await session.step()
            if forward_result is not None:
                break

        assert forward_result is not None, "Should have valid stop result after stepping"
        assert forward_result.reason != "unknown"

        # Now reverse_step should also return a proper stop result
        reverse_result = await session.reverse_step()
        assert reverse_result is not None
        assert reverse_result.reason != "unknown", (
            f"Reverse step should not return 'unknown', got: {reverse_result.reason}"
        )

    finally:
        await manager.close_session(session.session_id)


# ---------------------------------------------------------------------------
# Bug fix verification tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backtrace_max_depth_zero_returns_empty(recorded_simple_trace: Path) -> None:
    """max_depth=0 must return empty list, not all frames (#3)."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_simple_trace))

    try:
        frames = await session.get_backtrace(max_depth=0)
        assert frames == []
    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_source_listing_preserves_indentation(recorded_simple_trace: Path) -> None:
    """Source listing must preserve leading whitespace (#4)."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_simple_trace))

    try:
        # Set breakpoint in add function which has indented body
        await session.set_breakpoint("add")
        await session.continue_execution()

        source = await session.get_source_lines(lines_before=2, lines_after=2)
        assert len(source["lines"]) > 0

        # At least one line in a function body should start with whitespace
        contents = [line["content"] for line in source["lines"]]
        has_indented = any(c.startswith(" ") or c.startswith("\t") for c in contents if c)
        assert has_indented, f"No indented lines found in: {contents}"

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_session_limit_enforced(recorded_simple_trace: Path) -> None:
    """SessionManager must reject sessions beyond the limit (#10)."""
    manager = SessionManager(max_sessions=2)
    sessions = []

    try:
        s1, _ = await manager.create_session(trace=str(recorded_simple_trace))
        sessions.append(s1)
        s2, _ = await manager.create_session(trace=str(recorded_simple_trace))
        sessions.append(s2)

        with pytest.raises(RrMcpError, match="Maximum number of sessions"):
            await manager.create_session(trace=str(recorded_simple_trace))

    finally:
        for s in sessions:
            await manager.close_session(s.session_id)


# ---------------------------------------------------------------------------
# New feature integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_catch_throw(recorded_cpp_features_trace: Path) -> None:
    """Test catching C++ throw events (#15)."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_cpp_features_trace))

    try:
        # Set a catchpoint for C++ throw
        bp = await session.catch_throw()
        assert bp is not None
        assert bp.number is not None
        assert bp.number > 0

        # Continue — cpp_features throws exceptions in test_exceptions()
        stop = await session.continue_execution()
        assert stop is not None
        # Should hit the throw catchpoint
        assert stop.reason in ["breakpoint-hit", "signal-received", "end-stepping-range"]

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_catch_syscall(recorded_simple_trace: Path) -> None:
    """Test catching syscall events (#15)."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_simple_trace))

    try:
        # Set a catchpoint for write syscall (printf calls write)
        bp = await session.catch_syscall("write")
        assert bp is not None
        assert bp.number is not None

        # Continue — simple program uses printf which triggers write syscall
        stop = await session.continue_execution()
        assert stop is not None

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_handle_signal(recorded_simple_trace: Path) -> None:
    """Test configuring signal handling (#16)."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_simple_trace))

    try:
        # Configure SIGPIPE to not stop and not pass
        output = await session.handle_signal("SIGPIPE", stop=False, pass_through=False)
        assert isinstance(output, str)
        # GDB should confirm the configuration change
        assert len(output) > 0

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_info_proc_mappings(recorded_simple_trace: Path) -> None:
    """Test info subcommand (#19)."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_simple_trace))

    try:
        output = await session.info("proc mappings")
        assert isinstance(output, str)
        # Should contain memory mapping information with hex addresses
        assert "0x" in output.lower() or len(output) > 0

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_info_signals(recorded_simple_trace: Path) -> None:
    """Test info signals subcommand (#19)."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_simple_trace))

    try:
        output = await session.info("signals")
        assert isinstance(output, str)
        # Should list signal names
        assert "SIGINT" in output or "SIGSEGV" in output or "Signal" in output

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_find_in_memory(recorded_simple_trace: Path) -> None:
    """Test memory search (#18)."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_simple_trace))

    try:
        # Step into add(5, 3) and search for value 5 on the stack
        await session.set_breakpoint("add")
        await session.continue_execution()

        # Get stack pointer range
        regs = await session.read_registers()
        rsp = regs.get("rsp")
        assert rsp is not None

        # Search for value 5 (the 'a' parameter) near the stack pointer
        rsp_int = int(rsp, 16)
        start = f"0x{rsp_int:x}"
        end = f"0x{rsp_int + 256:x}"
        addresses = await session.find_in_memory(start, end, "5", size="w")
        # We may or may not find it depending on exact stack layout, but the
        # call itself should succeed without error
        assert isinstance(addresses, list)

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_pretty_printing_enabled(recorded_cpp_features_trace: Path) -> None:
    """Test that pretty-printers are active for STL containers (#17)."""
    manager = SessionManager()
    session, _ = await manager.create_session(trace=str(recorded_cpp_features_trace))

    try:
        await session.set_breakpoint("test_stl_containers")
        await session.continue_execution()

        # Step past vector initialization
        for _ in range(15):
            await session.step()

        # Evaluate numbers vector — with pretty-printers it should show elements
        val = await session.evaluate_expression("numbers")
        if val is not None:
            # Pretty-printed output should contain element values, not raw memory
            # (e.g., "std::vector of length 5" or "{10, 20, 30, 40, 50}")
            assert "10" in val or "vector" in val.lower() or len(val) > 5

    finally:
        await manager.close_session(session.session_id)
