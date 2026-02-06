// Multi-threaded program for testing thread management
#include <cstdio>
#include <pthread.h>
#include <unistd.h>

// Global shared data for testing data races and synchronization
int shared_counter = 0;
int race_counter = 0;  // Intentionally unsynchronized
pthread_mutex_t counter_mutex = PTHREAD_MUTEX_INITIALIZER;
pthread_mutex_t contended_mutex = PTHREAD_MUTEX_INITIALIZER;

// Thread-local storage
__thread int thread_local_value = 0;

// Worker that increments shared counter with proper synchronization
void* synchronized_worker(void* arg) {
    int id = *(int*)arg;
    thread_local_value = id * 100;

    printf("Thread %d starting (TLS=%d)\n", id, thread_local_value);

    for (int i = 0; i < 5; i++) {
        pthread_mutex_lock(&counter_mutex);
        shared_counter++;
        int current = shared_counter;
        pthread_mutex_unlock(&counter_mutex);

        printf("Thread %d: shared_counter=%d\n", id, current);
        usleep(1000);
    }

    printf("Thread %d done (TLS=%d)\n", id, thread_local_value);
    return nullptr;
}

// Worker that creates intentional data race
void* racing_worker(void* arg) {
    int id = *(int*)arg;

    for (int i = 0; i < 10; i++) {
        // Intentional race - no synchronization
        int temp = race_counter;
        usleep(100);  // Make race window more visible
        race_counter = temp + 1;
    }

    return nullptr;
}

// Worker that contends for a mutex
void* contending_worker(void* arg) {
    int id = *(int*)arg;

    for (int i = 0; i < 3; i++) {
        pthread_mutex_lock(&contended_mutex);
        printf("Thread %d got mutex, iteration %d\n", id, i);
        usleep(5000);  // Hold mutex for a while
        pthread_mutex_unlock(&contended_mutex);
        usleep(1000);
    }

    return nullptr;
}

int main() {
    printf("Starting thread test\n");

    // Test 1: Synchronized workers with thread-local storage
    {
        pthread_t threads[4];
        int ids[4] = {1, 2, 3, 4};

        printf("\n=== Test 1: Synchronized workers ===\n");
        for (int i = 0; i < 4; i++) {
            pthread_create(&threads[i], nullptr, synchronized_worker, &ids[i]);
        }

        for (int i = 0; i < 4; i++) {
            pthread_join(threads[i], nullptr);
        }

        printf("Final shared_counter=%d (expected 20)\n", shared_counter);
    }

    // Test 2: Data race
    {
        pthread_t threads[3];
        int ids[3] = {5, 6, 7};

        printf("\n=== Test 2: Data race ===\n");
        race_counter = 0;
        for (int i = 0; i < 3; i++) {
            pthread_create(&threads[i], nullptr, racing_worker, &ids[i]);
        }

        for (int i = 0; i < 3; i++) {
            pthread_join(threads[i], nullptr);
        }

        printf("Final race_counter=%d (expected 30, likely less due to race)\n", race_counter);
    }

    // Test 3: Mutex contention
    {
        pthread_t threads[4];
        int ids[4] = {8, 9, 10, 11};

        printf("\n=== Test 3: Mutex contention ===\n");
        for (int i = 0; i < 4; i++) {
            pthread_create(&threads[i], nullptr, contending_worker, &ids[i]);
        }

        for (int i = 0; i < 4; i++) {
            pthread_join(threads[i], nullptr);
        }
    }

    printf("\nAll threads done\n");
    return 0;
}
