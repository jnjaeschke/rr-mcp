"""Integration tests for session management using real rr traces."""

from pathlib import Path

import pytest

from rr_mcp.session import SessionManager


@pytest.mark.asyncio
async def test_create_session(recorded_simple_trace: Path) -> None:
    """Test creating a replay session."""
    manager = SessionManager()

    session, initial_location = await manager.create_session(trace=str(recorded_simple_trace))

    try:
        # Check session was created
        assert session.session_id is not None
        assert len(session.session_id) > 0
        assert session.trace == str(recorded_simple_trace)

        # Check initial location
        assert initial_location.event >= 0
        assert initial_location.address is not None

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_list_sessions(recorded_simple_trace: Path) -> None:
    """Test listing active sessions."""
    manager = SessionManager()

    # Initially no sessions
    assert len(manager.list_sessions()) == 0

    # Create a session
    session1, _ = await manager.create_session(trace=str(recorded_simple_trace))
    assert len(manager.list_sessions()) == 1

    # Create another session
    session2, _ = await manager.create_session(trace=str(recorded_simple_trace))
    assert len(manager.list_sessions()) == 2

    # Close sessions
    await manager.close_session(session1.session_id)
    assert len(manager.list_sessions()) == 1

    await manager.close_session(session2.session_id)
    assert len(manager.list_sessions()) == 0


@pytest.mark.asyncio
async def test_get_session(recorded_simple_trace: Path) -> None:
    """Test getting a session by ID."""
    from rr_mcp.errors import SessionNotFoundError

    manager = SessionManager()

    session, _ = await manager.create_session(trace=str(recorded_simple_trace))

    try:
        # Should be able to get the session
        retrieved = manager.get_session(session.session_id)
        assert retrieved == session

        # Non-existent session should raise error
        with pytest.raises(SessionNotFoundError):
            manager.get_session("nonexistent-id")

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_session_get_current_position(recorded_simple_trace: Path) -> None:
    """Test getting current event/tick position."""
    manager = SessionManager()

    session, _ = await manager.create_session(trace=str(recorded_simple_trace))

    try:
        event, tick = await session.get_current_position()

        # Should have valid event number
        assert event >= 0
        assert tick >= 0

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_session_execute_command(recorded_simple_trace: Path) -> None:
    """Test executing a GDB/MI command."""
    manager = SessionManager()

    session, _ = await manager.create_session(trace=str(recorded_simple_trace))

    try:
        # Execute a simple command
        response = await session.execute("-stack-info-frame")

        # Should get a response
        assert isinstance(response, list)
        assert len(response) > 0

    finally:
        await manager.close_session(session.session_id)


@pytest.mark.asyncio
async def test_close_all_sessions(recorded_simple_trace: Path) -> None:
    """Test closing all sessions at once."""
    manager = SessionManager()

    # Create multiple sessions
    await manager.create_session(trace=str(recorded_simple_trace))
    await manager.create_session(trace=str(recorded_simple_trace))
    await manager.create_session(trace=str(recorded_simple_trace))

    assert len(manager.list_sessions()) == 3

    # Close all
    await manager.close_all()

    assert len(manager.list_sessions()) == 0
