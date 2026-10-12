def add(a, b):
    return a + b


def average(numbers):
    if not numbers:
        return 0
    return sum(numbers) / len(numbers)


def safe_divide(a, b):
    if b == 0:
        return None
    return a / b
