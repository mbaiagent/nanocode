"""PostToolUse hook 示例：把每次文件修改记录到审计日志。退出码 0，不影响流程。"""

import datetime
import json
import os
import sys

data = json.load(sys.stdin)
path = data.get("tool_input", {}).get("path", "?")
os.makedirs(".nanocode", exist_ok=True)
with open(".nanocode/edits.log", "a", encoding="utf-8") as f:
    f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {data.get('tool_name')}  {path}\n")
