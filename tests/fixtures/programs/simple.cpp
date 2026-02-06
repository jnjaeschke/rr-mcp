// Simple program for testing basic execution control (step, next, etc.)
#include <cstdio>

int add(int a, int b) {
    return a + b;
}

int multiply(int x, int y) {
    int result = 0;
    for (int i = 0; i < y; i++) {
        result = add(result, x);
    }
    return result;
}

int main() {
    printf("Starting simple test\n");

    int a = 5;
    int b = 3;
    int sum = add(a, b);
    printf("Sum: %d\n", sum);

    int product = multiply(a, b);
    printf("Product: %d\n", product);

    printf("Done\n");
    return 0;
}
