// Multi-process program for testing fork/exec debugging
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <unistd.h>
#include <sys/wait.h>

// Function called in both parent and child
int compute_value(int base, int multiplier) {
    return base * multiplier;
}

// Child process function
void child_process(int child_num) {
    printf("Child %d: PID=%d, PPID=%d\n", child_num, getpid(), getppid());

    int result = compute_value(child_num, 10);
    printf("Child %d: computed value=%d\n", child_num, result);

    // Do some work
    int sum = 0;
    for (int i = 0; i < 5; i++) {
        sum += compute_value(i, child_num);
        printf("Child %d: iteration %d, sum=%d\n", child_num, i, sum);
    }

    printf("Child %d: final sum=%d\n", child_num, sum);
    _exit(sum % 256);  // Use _exit to avoid flushing parent's buffers
}

int main(int argc, char** argv) {
    // Check if we're running as a child process
    if (argc == 3 && strcmp(argv[1], "--child") == 0) {
        int child_num = atoi(argv[2]);
        child_process(child_num);
        return 0;
    }

    printf("Parent: PID=%d starting\n", getpid());

    pid_t pids[3];
    int parent_sum = 0;

    // Fork 3 children
    for (int i = 0; i < 3; i++) {
        pid_t pid = fork();

        if (pid < 0) {
            perror("fork failed");
            return 1;
        } else if (pid == 0) {
            // Child process - exec ourselves with child flag
            char child_num_str[16];
            snprintf(child_num_str, sizeof(child_num_str), "%d", i + 1);
            execl(argv[0], argv[0], "--child", child_num_str, nullptr);
            // If exec fails, print error and exit
            perror("exec failed");
            _exit(1);
        } else {
            // Parent process
            pids[i] = pid;
            printf("Parent: forked child %d with PID=%d\n", i + 1, pid);

            // Do some work in parent too
            parent_sum += compute_value(i, 5);
        }
    }

    // Parent waits for all children
    printf("Parent: waiting for children, current sum=%d\n", parent_sum);

    for (int i = 0; i < 3; i++) {
        int status;
        pid_t pid = wait(&status);

        if (WIFEXITED(status)) {
            int exit_code = WEXITSTATUS(status);
            printf("Parent: child PID=%d exited with code=%d\n", pid, exit_code);
            parent_sum += exit_code;
        } else if (WIFSIGNALED(status)) {
            printf("Parent: child PID=%d killed by signal=%d\n", pid, WTERMSIG(status));
        }
    }

    printf("Parent: all children done, final sum=%d\n", parent_sum);
    printf("Parent: exiting\n");
    return 0;
}
