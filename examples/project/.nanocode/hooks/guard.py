"""PreToolUse hook 示例：拦截危险命令。

stdin 收到 {"event": "PreToolUse", "tool_name": "bash", "tool_input": {"command": ...}}
退出码 2 = 拦截，stderr 会作为原因反馈给模型。
"""

import json
import re
import sys

data = json.load(sys.stdin)
cmd = data.get("tool_input", {}).get("command", "")

DANGEROUS = [
    (r"\brm\s+-[a-zA-Z]*r", "递归删除"),
    (r"git\s+push\s+.*--force", "强制推送"),
    (r"git\s+reset\s+--hard", "丢弃本地修改"),
    (r"\bcurl\b.*\|\s*(ba)?sh", "下载并直接执行脚本"),
]
for pattern, reason in DANGEROUS:
    if re.search(pattern, cmd):
        print(f"命令被安全 hook 拦截（{reason}）：{cmd}\n如果确实需要，请让用户自己手动执行。", file=sys.stderr)
        sys.exit(2)
sys.exit(0)
