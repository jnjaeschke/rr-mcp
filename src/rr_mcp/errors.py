"""Error types for rr-mcp."""


class RrMcpError(Exception):
    """Base exception for rr-mcp errors."""

    pass


class TraceNotFoundError(RrMcpError):
    """Raised when a trace cannot be found."""

    def __init__(self, trace: str) -> None:
        self.trace = trace
        super().__init__(f"Trace not found: {trace}")


class SessionNotFoundError(RrMcpError):
    """Raised when a session cannot be found."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"Session not found: {session_id}")


class RrCommandError(RrMcpError):
    """Raised when an rr command fails."""

    def __init__(self, command: str, stderr: str, returncode: int) -> None:
        self.command = command
        self.stderr = stderr
        self.returncode = returncode
        super().__init__(f"rr command failed: {command!r} (exit {returncode}): {stderr}")


class GdbError(RrMcpError):
    """Raised when a GDB/MI command fails."""

    def __init__(self, message: str, details: str | None = None) -> None:
        self.details = details
        super().__init__(message)
