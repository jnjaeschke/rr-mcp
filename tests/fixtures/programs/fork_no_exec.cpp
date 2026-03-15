// Simulates the Firefox parent/content-process model:
// parent fork()s a child that never calls exec() — it stays in the same
// memory image and does its own work.  This is what rr's -f flag is for.
#include <cstdio>
#include <sys/wait.h>
#include <unistd.h>

// A function that only the content (child) process ever calls.
// Having a distinct name makes breakpoint placement straightforward.
int content_process_work(int secret) {
    int result = secret * 2 + 1;
    printf("content: secret=%d result=%d\n", secret, result);
    return result;
}

// A function that only the parent process ever calls.
int parent_process_work(int value) {
    int result = value + 100;
    printf("parent: value=%d result=%d\n", value, result);
    return result;
}

int main() {
    printf("parent: PID=%d starting\n", getpid());

    pid_t child_pid = fork();

    if (child_pid < 0) {
        perror("fork");
        return 1;
    }

    if (child_pid == 0) {
        // Child — no exec(), stays in this memory image
        printf("content: PID=%d PPID=%d\n", getpid(), getppid());
        int r = content_process_work(42);
        printf("content: done, result=%d\n", r);
        _exit(r % 256);
    }

    // Parent
    int r = parent_process_work(7);
    printf("parent: waiting for content process %d\n", child_pid);

    int status;
    waitpid(child_pid, &status, 0);
    if (WIFEXITED(status)) {
        printf("parent: content process exited with %d\n", WEXITSTATUS(status));
    }

    printf("parent: done, result=%d\n", r);
    return 0;
}
