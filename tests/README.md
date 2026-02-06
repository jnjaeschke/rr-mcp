# rr-mcp Tests

This directory contains automated tests for the rr-mcp server.

## Test Strategy

Tests use **on-demand trace recording**: C++ test programs are compiled and recorded as rr traces during test setup. This ensures tests always work with the current version of rr.

### Test Programs

Located in `fixtures/programs/`:

- **crash.cpp**: Simple segfault for testing backtrace and signal handling
- **simple.cpp**: Basic execution control (step, next, continue)
- **threads.cpp**: Multi-threaded program for thread management tests
- **recursive.cpp**: Deep callstacks for frame selection and backtrace tests
- **fork_test.cpp**: Multi-process program for testing process switching
- **cpp_features.cpp**: C++ language features (virtual functions, STL, templates, exceptions)

### Test Structure

- **conftest.py**: Pytest fixtures for:
  - Building test programs with CMake
  - Managing temporary trace directories
  - Recording traces on-demand
  - Cleanup after tests

- **test_trace.py**: Unit tests for trace discovery and metadata extraction
- **test_trace_integration.py**: Integration tests for real trace operations
- **test_session.py**: Unit tests for session management (mocked)
- **test_session_integration.py**: Integration tests for session lifecycle
- **test_operations_integration.py**: Integration tests for all debugging operations (step, breakpoints, etc.)

## Requirements

- rr must be installed and in PATH
- If rr is not available, tests will fail immediately

## Running Tests

```bash
# Install dev dependencies
uv sync --all-extras

# Run all tests
uv run pytest

# Run specific test file
uv run pytest tests/test_trace.py

# Run with coverage
uv run pytest --cov=rr_mcp --cov-report=html

# Verbose output
uv run pytest -vv

# Stop on first failure
uv run pytest -x
```

## Test Lifecycle

1. **Session setup**: Check if rr is available (fail if not)
2. **Build programs**: Compile all C++ test programs (once per session)
3. **Per-test setup**: Create temporary trace directory
4. **Record trace**: Record program execution when fixture is used
5. **Run test**: Execute test assertions
6. **Cleanup**: Remove temporary traces

## Adding New Tests

1. **Add test program** (if needed):

   ```cpp
   // tests/fixtures/programs/mynew.cpp
   #include <cstdio>

   int main() {
       printf("Test something specific\n");
       return 0;
   }
   ```

2. **Update CMakeLists.txt**:

   ```cmake
   # Add to tests/fixtures/programs/CMakeLists.txt
   add_executable(mynew mynew.cpp)
   ```

3. **Add fixture** in `conftest.py`:

   ```python
   @pytest.fixture
   def recorded_mynew_trace(
       build_programs: None,
       programs_dir: Path,
       temp_trace_dir: Path,
   ) -> Path:
       program = programs_dir / "mynew"
       return record_trace(program, temp_trace_dir, "mynew-trace")
   ```

4. **Write tests**:

   ```python
   def test_mynew_feature(recorded_mynew_trace: Path) -> None:
       # Test uses the recorded trace
       info = get_trace_info(str(recorded_mynew_trace))
       assert ...
   ```

## CI Integration

For CI environments, ensure:

- rr is installed (Ubuntu: `apt-get install rr`)
- Sufficient disk space for traces
- x86-64 architecture (rr requirement)

## Performance Notes

- First test run: Slow (builds programs + records traces)
- Subsequent runs: Fast (programs cached, traces recorded fresh)
- Each test gets its own temporary trace directory
- Traces are automatically cleaned up after tests
