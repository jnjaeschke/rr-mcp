// Recursive program for testing deep callstacks and frame selection
#include <cstdio>

int fibonacci(int n) {
    if (n <= 1) {
        return n;
    }
    return fibonacci(n - 1) + fibonacci(n - 2);
}

int factorial(int n) {
    if (n <= 1) {
        return 1;
    }
    return n * factorial(n - 1);
}

int main() {
    printf("Starting recursive test\n");

    int fib = fibonacci(8);
    printf("fibonacci(8) = %d\n", fib);

    int fact = factorial(5);
    printf("factorial(5) = %d\n", fact);

    printf("Done\n");
    return 0;
}
