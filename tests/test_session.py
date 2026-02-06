"""Tests for session lifecycle management."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rr_mcp.errors import SessionNotFoundError
from rr_mcp.models import Location, SessionState
from rr_mcp.session import Session, SessionManager


class TestSessionManager:
    """Tests for SessionManager."""

    @pytest.fixture
    def manager(self) -> SessionManager:
        """Create a fresh session manager."""
        return SessionManager()

    def test_initial_state(self, manager: SessionManager) -> None:
        """SessionManager should start with no sessions."""
        assert manager.list_sessions() == []

    @pytest.mark.asyncio
    async def test_create_session(self, manager: SessionManager) -> None:
        """Should create a session with unique ID."""
        with patch.object(Session, "start", new_callable=AsyncMock) as mock_start:
            mock_start.return_value = Location(
                event=0,
                tick=0,
                address="0x0",
                function=None,
                file=None,
                line=None,
            )

            session, location = await manager.create_session(trace="/fake/trace", pid=1234)

            assert session.session_id is not None
            assert session.trace == "/fake/trace"
            assert session.pid == 1234
            assert session.state == SessionState.PAUSED
            assert location.event >= 0
            mock_start.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_multiple_sessions(self, manager: SessionManager) -> None:
        """Should support multiple concurrent sessions."""
        with patch.object(Session, "start", new_callable=AsyncMock):
            session1, _ = await manager.create_session(trace="/trace1", pid=1001)
            session2, _ = await manager.create_session(trace="/trace1", pid=1002)
            session3, _ = await manager.create_session(trace="/trace2", pid=2001)

            sessions = manager.list_sessions()
            assert len(sessions) == 3
            assert session1.session_id != session2.session_id
            assert session2.session_id != session3.session_id

    @pytest.mark.asyncio
    async def test_get_session(self, manager: SessionManager) -> None:
        """Should retrieve session by ID."""
        with patch.object(Session, "start", new_callable=AsyncMock):
            session, _ = await manager.create_session(trace="/fake/trace", pid=1234)

            retrieved = manager.get_session(session.session_id)
            assert retrieved is session

    def test_get_session_not_found(self, manager: SessionManager) -> None:
        """Should raise SessionNotFoundError for invalid ID."""
        with pytest.raises(SessionNotFoundError):
            manager.get_session("nonexistent-id")

    @pytest.mark.asyncio
    async def test_close_session(self, manager: SessionManager) -> None:
        """Should close and remove session."""
        with (
            patch.object(Session, "start", new_callable=AsyncMock),
            patch.object(Session, "close", new_callable=AsyncMock) as mock_close,
        ):
            session, _ = await manager.create_session(trace="/fake/trace", pid=1234)
            session_id = session.session_id

            await manager.close_session(session_id)

            mock_close.assert_called_once()
            assert manager.list_sessions() == []

            with pytest.raises(SessionNotFoundError):
                manager.get_session(session_id)

    @pytest.mark.asyncio
    async def test_close_all_sessions(self, manager: SessionManager) -> None:
        """Should close all sessions."""
        with (
            patch.object(Session, "start", new_callable=AsyncMock),
            patch.object(Session, "close", new_callable=AsyncMock) as mock_close,
        ):
            await manager.create_session(trace="/trace1", pid=1001)
            await manager.create_session(trace="/trace1", pid=1002)
            await manager.create_session(trace="/trace2", pid=2001)

            await manager.close_all()

            assert mock_close.call_count == 3
            assert manager.list_sessions() == []


class TestSession:
    """Tests for Session class."""

    @pytest.fixture
    def mock_gdb_controller(self) -> MagicMock:
        """Create a mock GdbController."""
        controller = MagicMock()
        controller.write = MagicMock(
            return_value=[{"type": "result", "message": "done", "payload": None}]
        )
        return controller

    def test_session_initial_state(self) -> None:
        """Session should start in PAUSED state (before start)."""
        session = Session(trace="/fake/trace", pid=1234)
        assert session.state == SessionState.PAUSED
        assert session.trace == "/fake/trace"
        assert session.pid == 1234
        assert session.session_id is not None

    @pytest.mark.asyncio
    async def test_session_start_spawns_rr(self) -> None:
        """Session.start should spawn rr replay process."""
        session = Session(trace="/fake/trace", pid=1234)

        with patch("rr_mcp.session.GdbController") as MockGdbController:
            mock_instance = MagicMock()
            mock_instance.write = MagicMock(return_value=[{"type": "result", "message": "done"}])
            MockGdbController.return_value = mock_instance

            await session.start()

            # Should have spawned rr with correct arguments
            MockGdbController.assert_called_once()
            call_args = MockGdbController.call_args
            command = call_args[1]["command"]
            assert "rr" in command
            assert "replay" in command
            assert "/fake/trace" in command
            assert "-p" in command
            assert "1234" in command

    @pytest.mark.asyncio
    async def test_session_close(self) -> None:
        """Session.close should terminate the gdb process."""
        session = Session(trace="/fake/trace", pid=1234)

        with patch("rr_mcp.session.GdbController") as MockGdbController:
            mock_instance = MagicMock()
            mock_instance.write = MagicMock(return_value=[{"type": "result", "message": "done"}])
            mock_instance.exit = MagicMock()
            MockGdbController.return_value = mock_instance

            await session.start()
            await session.close()

            mock_instance.exit.assert_called_once()
            assert session.state == SessionState.CLOSED
