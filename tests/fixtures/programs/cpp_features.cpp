// C++ features test: classes, virtual functions, exceptions, STL
#include <cstdio>
#include <vector>
#include <map>
#include <string>
#include <memory>
#include <stdexcept>

// Base class with virtual functions
class Shape {
public:
    virtual ~Shape() = default;

    virtual double area() const = 0;
    virtual const char* name() const = 0;

    void print_info() const {
        printf("%s: area = %.2f\n", name(), area());
    }
};

// Derived classes
class Circle : public Shape {
private:
    double radius;

public:
    explicit Circle(double r) : radius(r) {}

    double area() const override {
        return 3.14159 * radius * radius;
    }

    const char* name() const override {
        return "Circle";
    }

    double get_radius() const { return radius; }
};

class Rectangle : public Shape {
private:
    double width;
    double height;

public:
    Rectangle(double w, double h) : width(w), height(h) {}

    double area() const override {
        return width * height;
    }

    const char* name() const override {
        return "Rectangle";
    }

    double get_width() const { return width; }
    double get_height() const { return height; }
};

// Template function
template<typename T>
T max_value(T a, T b) {
    return (a > b) ? a : b;
}

// Function that throws exceptions
double divide(double a, double b) {
    if (b == 0.0) {
        throw std::invalid_argument("Division by zero");
    }
    return a / b;
}

// Function using STL containers
void test_stl_containers() {
    printf("\n=== STL Containers Test ===\n");

    // Vector
    std::vector<int> numbers = {10, 20, 30, 40, 50};
    printf("Vector contents: ");
    for (int num : numbers) {
        printf("%d ", num);
    }
    printf("\n");

    // Map
    std::map<std::string, int> scores;
    scores["alice"] = 95;
    scores["bob"] = 87;
    scores["charlie"] = 92;

    printf("Map contents:\n");
    for (const auto& pair : scores) {
        printf("  %s: %d\n", pair.first.c_str(), pair.second);
    }

    // Test vector operations
    numbers.push_back(60);
    numbers.push_back(70);
    printf("Vector size: %zu, capacity: %zu\n", numbers.size(), numbers.capacity());

    int sum = 0;
    for (int num : numbers) {
        sum += num;
    }
    printf("Sum of vector elements: %d\n", sum);
}

// Function testing polymorphism
void test_polymorphism() {
    printf("\n=== Polymorphism Test ===\n");

    // Create shapes using smart pointers
    std::vector<std::unique_ptr<Shape>> shapes;
    shapes.push_back(std::make_unique<Circle>(5.0));
    shapes.push_back(std::make_unique<Rectangle>(4.0, 6.0));
    shapes.push_back(std::make_unique<Circle>(3.0));

    // Call virtual functions through base class pointers
    for (const auto& shape : shapes) {
        shape->print_info();
    }

    double total_area = 0.0;
    for (const auto& shape : shapes) {
        total_area += shape->area();
    }
    printf("Total area: %.2f\n", total_area);
}

// Function testing exceptions
void test_exceptions() {
    printf("\n=== Exception Handling Test ===\n");

    try {
        double result1 = divide(10.0, 2.0);
        printf("10.0 / 2.0 = %.2f\n", result1);

        double result2 = divide(5.0, 0.0);  // Will throw
        printf("5.0 / 0.0 = %.2f\n", result2);  // Should not reach here
    } catch (const std::invalid_argument& e) {
        printf("Caught exception: %s\n", e.what());
    }

    // Nested try-catch
    try {
        try {
            throw std::runtime_error("Inner exception");
        } catch (const std::runtime_error& e) {
            printf("Caught inner exception: %s\n", e.what());
            throw;  // Re-throw
        }
    } catch (const std::exception& e) {
        printf("Caught re-thrown exception: %s\n", e.what());
    }
}

// Function testing templates
void test_templates() {
    printf("\n=== Template Test ===\n");

    int max_int = max_value(10, 20);
    printf("max(10, 20) = %d\n", max_int);

    double max_double = max_value(3.14, 2.71);
    printf("max(3.14, 2.71) = %.2f\n", max_double);
}

int main() {
    printf("Starting C++ features test\n");

    try {
        test_stl_containers();
        test_polymorphism();
        test_templates();
        test_exceptions();

        printf("\nAll tests completed\n");
        return 0;
    } catch (const std::exception& e) {
        printf("Unhandled exception: %s\n", e.what());
        return 1;
    }
}
