import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from checklib import args, finish, last_line, run_tests, task_dir  # noqa: E402

work, _ = args()
if not os.path.isfile(os.path.join(work, "test_mathx.py")):
    finish(False, "没有找到 test_mathx.py")
original = os.path.join(task_dir(), "files", "mathx.py")
shutil.copy(original, os.path.join(work, "mathx.py"))          # 用原版实现跑，防止改了被测代码
ok, out, n = run_tests(work, "test_mathx.py")
if not ok:
    finish(False, "测试在正确实现上失败：" + last_line(out))
if n < 6:
    finish(False, f"只有 {n} 个测试，要求至少 6 个")
# 变异测试：把实现换成故意写错的版本，好的测试应该能发现每一个
missed = []
hidden = os.path.join(task_dir(), "hidden")
for name in sorted(os.listdir(hidden)):
    shutil.copy(os.path.join(hidden, name), os.path.join(work, "mathx.py"))
    caught, _, _ = run_tests(work, "test_mathx.py")
    if caught:
        missed.append(name)
shutil.copy(original, os.path.join(work, "mathx.py"))
total = len(os.listdir(hidden))
finish(not missed, f"{n} 个测试，发现了 {total - len(missed)}/{total} 个注入的 bug" + (f"，漏掉 {missed}" if missed else ""))
