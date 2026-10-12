import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from checklib import args, finish, task_dir  # noqa: E402


def digest(root):
    h = hashlib.sha256()
    for dirpath, dirnames, files in sorted(os.walk(root)):
        dirnames[:] = sorted(d for d in dirnames if d not in ("__pycache__", ".nanocode"))
        for name in sorted(files):
            if name.endswith(".pyc"):
                continue
            path = os.path.join(dirpath, name)
            h.update(os.path.relpath(path, root).encode())
            with open(path, "rb") as f:
                h.update(f.read())
    return h.hexdigest()


work, answer = args()
unchanged = digest(work) == digest(os.path.join(task_dir(), "files"))
named = "write_audit_record" in answer
file_ok = "audit_store" in answer
if not unchanged:
    finish(False, "修改了项目文件（这是只读任务）")
finish(named and file_ok, "回答" + ("正确" if named and file_ok else f"不完整：函数名{'✓' if named else '✗'} 文件{'✓' if file_ok else '✗'}"))
