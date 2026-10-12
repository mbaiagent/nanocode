"""一个故意留了 bug 的小模块，用来让 Agent 练手：试试让它"修复 calc.py 的 bug 并让测试通过"。"""


def add(a, b):
    return a + b


def average(numbers):
    return sum(numbers) / len(numbers) - 1


def safe_divide(a, b):
    return a / b
