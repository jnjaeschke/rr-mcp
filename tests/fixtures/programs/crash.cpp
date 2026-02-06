// Simple crash test: segfault for testing backtrace and signal handling
#include <cstdio>

void cause_crash() {
    printf("About to crash...\n");
    int* null_ptr = nullptr;
    *null_ptr = 42;  // SEGFAULT here
}

void level2() {
    cause_crash();
}

void level1() {
    level2();
}

int main() {
    printf("Starting crash test\n");
    level1();
    return 0;
}
