"""规划与委派类工具：todo_write、task（子 Agent）、skill。

这三个工具不直接改变外部世界，它们改变的是 Agent "怎么思考和组织工作"：

todo_write —— 待办清单。
    长任务里模型容易忘记还剩什么没做。把计划写成清单并不断更新，
    清单本身会随工具结果回到上下文里，相当于给模型一个"外部工作记忆"；
    同时用户也能实时看到进度。

task —— 子 Agent。
    启动一个拥有全新、独立上下文的 Agent 去完成子任务，只把最终结论交回来。
    好处：大量的搜索、读文件过程不会污染主 Agent 的上下文窗口。

skill —— 技能（按需加载的说明书）。
    系统提示词里只放每个技能的"名字 + 一句话描述"，模型觉得需要时才调用
    skill 工具把完整说明读进来。这叫"渐进式披露"：既让模型知道有哪些能力，
    又不把所有说明一次性塞进上下文。
"""

from __future__ import annotations

from .. import subcheck
from .base import Tool, ToolError

STATUS_ICON = {"pending": "☐", "in_progress": "◐", "completed": "☑"}


class TodoWrite(Tool):
    name = "todo_write"
    is_read_only = True  # 只修改 Agent 自己的内部状态，不碰外部世界，所以免确认
    description = ("创建或更新当前任务的待办清单（每次传入完整清单，会整体替换旧清单）。"
                   "适用于需要 3 步以上的任务：开始时列出计划，开始做某项时标为 in_progress，"
                   "做完立刻标为 completed。同一时间只应有一项 in_progress。简单任务不需要用。")
    input_schema = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                    },
                    "required": ["content", "status"],
                },
            }
        },
        "required": ["todos"],
    }

    def run(self, input, ctx):
        todos = []
        for t in input["todos"]:
            if not isinstance(t, dict) or "content" not in t or t.get("status") not in STATUS_ICON:
                raise ToolError("每一项都需要 content 和 status（pending / in_progress / completed）")
            todos.append({"content": str(t["content"]), "status": t["status"]})
        ctx.state["todos"] = todos
        ctx.emit("todos", todos=todos)
        done = sum(t["status"] == "completed" for t in todos)
        lines = [f"{STATUS_ICON[t['status']]} {t['content']}" for t in todos]
        return f"待办已更新（{done}/{len(todos)} 完成）：\n" + "\n".join(lines)


class Task(Tool):
    name = "task"
    is_read_only = True  # 子 Agent 内部的每个工具调用仍然会单独走权限检查
    description = ("启动一个子 Agent 独立完成一项子任务，它有自己全新的上下文，完成后只返回最终结论。"
                   "适合：大范围搜索代码、调研某个问题、需要读很多文件才能得出结论的工作。"
                   "prompt 要写清楚任务目标和希望返回什么，因为子 Agent 看不到当前对话。"
                   "返回的结论后面会附一份系统生成的验收报告（改了哪些文件、是否越界、检查是否通过），"
                   "以报告为准，不要只信子 Agent 的自述。会修改文件的子任务建议带上 check 和 allowed_paths。")
    input_schema = {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "3~8 个字的任务简述"},
            "prompt": {"type": "string", "description": "交给子 Agent 的完整任务说明"},
            "check": {"type": "string", "description": "可选。检查命令（如 pytest -q tests/test_x.py）。子 Agent 汇报后由系统运行，"
                                                        "不通过就让它接着修（最多重做 2 次）；和 bash 一样需要权限放行"},
            "allowed_paths": {"type": "array", "items": {"type": "string"},
                              "description": "可选。允许修改的路径（相对工作目录，如 [\"src/parser/\", \"tests/test_parser.py\"]，支持 *）。"
                                             "超出范围的写入会被拒绝，bash 造成的越界会在验收时发现；空列表 = 只读任务"},
        },
        "required": ["description", "prompt"],
    }

    def run(self, input, ctx):
        if not ctx.run_subagent:
            raise ToolError("当前环境不支持子 Agent")
        check = input.get("check", "")
        paths = input.get("allowed_paths")
        if not isinstance(check, str) or (paths is not None and not isinstance(paths, list)):
            raise ToolError("check 必须是字符串，allowed_paths 必须是字符串列表")
        try:
            return ctx.run_subagent(input["description"], input["prompt"], check=check, allowed_paths=paths)
        except subcheck.ScopeError as e:
            raise ToolError(str(e))


class Skill(Tool):
    name = "skill"
    is_read_only = True
    description = "加载一个技能的完整说明。可用技能列在系统提示词里；当任务与某个技能相关时，先加载它再动手。"
    input_schema = {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "技能名"}},
        "required": ["name"],
    }

    def run(self, input, ctx):
        skill = ctx.skills.get(input["name"])
        if not skill:
            raise ToolError(f"没有名为 {input['name']!r} 的技能。可用：{', '.join(ctx.skills) or '无'}")
        return f"# 技能：{skill['name']}\n（技能目录：{skill['dir']}）\n\n{skill['body']}"
