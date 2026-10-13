"""经验沉淀：被自动验证打回、后来修好并通过的回合，生成一条"候选约束"，由用户确认后才写进项目记忆。

三条规矩（对应 issue #12）：
  1. 只记有证据的：候选的内容来自失败的检查输出**原文**和实际改动的文件，不让模型自己总结——
     模型的主观总结可能是错的，一旦写进记忆就会影响以后每一次会话。
  2. 先展示，再写入：候选只是提示，不确认就不会写。避免把一次性的失误固化成规则。
  3. 只在"被打回过、后来确实通过了"时产生：没通过（验证放弃）的回合没有可靠结论，不沉淀。

候选的格式是固定模板，不调用模型：
    改动后需通过 `pytest -q`（曾因「FAIL: test_x」被打回，修复涉及 src/a.py）

已知局限：它只能从"检查命令挂了"这类证据里学到东西；记不住"某个目录不能改"这种来自用户纠正的经验
（那类请直接用 `# 一句话` 追加）。未确认的候选只存在于当前会话，退出后就没了。
"""

from __future__ import annotations

import re

_EVIDENCE = re.compile(r"^(FAIL|ERROR|FAILED|E\s{2,}|AssertionError|Traceback|\S*(Error|Exception)\b[:\s]|.*\berror\b.*:\s)", re.I)
MAX_EVIDENCE_LINES = 2
MAX_LINE = 100
MAX_FILES = 3


def clean(text: str, limit: int = MAX_LINE) -> str:
    """压成单行、去掉控制字符：证据来自命令输出，里面什么都可能有，写进记忆文件前必须规整。"""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip().replace("`", "'")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def extract_evidence(output: str) -> list[str]:
    """从失败输出里挑最能说明问题的几行（原文，截断）；挑不出就取最后一行。"""
    lines = [l.strip() for l in output.splitlines() if l.strip()]
    picked: list[str] = []
    for line in lines:
        if _EVIDENCE.match(line) and not line.startswith("Traceback") and clean(line) not in picked:
            picked.append(clean(line))
        if len(picked) >= MAX_EVIDENCE_LINES:
            break
    if not picked and lines:
        picked.append(clean(lines[-1]))
    return picked


def constraint_prefix(command: str) -> str:
    return f"改动后需通过 `{clean(command, 120)}`"


def build(command: str, first_failure: str, fixed_files: list[str]) -> dict | None:
    """生成候选。没有证据（失败时一行输出都没有）就不生成。"""
    evidence = extract_evidence(first_failure)
    if not evidence:
        return None
    why = "；".join(f"「{e}」" for e in evidence)
    files = sorted(set(fixed_files))
    fixed = f"，修复涉及 {', '.join(clean(f, 80) for f in files[:MAX_FILES])}{' 等' if len(files) > MAX_FILES else ''}" if files else ""
    text = f"{constraint_prefix(command)}（曾因 {why} 被打回{fixed}）"
    return {"text": text, "key": constraint_prefix(command), "command": command, "evidence": evidence, "files": files}
