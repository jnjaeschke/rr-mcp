cmake_minimum_required(VERSION 3.15)
project(rr_mcp_test_programs CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Build with debug symbols and no optimization
set(CMAKE_CXX_FLAGS_DEBUG "-g -O0")
set(CMAKE_BUILD_TYPE Debug)

# Test programs
add_executable(crash crash.cpp)
add_executable(simple simple.cpp)
add_executable(threads threads.cpp)
add_executable(recursive recursive.cpp)
add_executable(fork_test fork_test.cpp)
add_executable(cpp_features cpp_features.cpp)
add_executable(fork_no_exec fork_no_exec.cpp)

# Link pthread for threads program
target_link_libraries(threads PRIVATE pthread)
