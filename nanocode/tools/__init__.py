"""内置工具集。"""

from .base import Tool, ToolContext, ToolError, ToolRegistry, ToolResult, truncate
from .files import EditFile, ReadFile, WriteFile
from .planning import Skill, Task, TodoWrite
from .rag_tool import SearchDocs
from .search import Glob, Grep
from .shell import Bash
from .web import WebFetch
from .web_search import WebSearch


def default_tools() -> list[Tool]:
    return [ReadFile(), WriteFile(), EditFile(), Bash(), Glob(), Grep(), SearchDocs(), WebFetch(), WebSearch(), TodoWrite(), Task(), Skill()]


__all__ = ["Tool", "ToolContext", "ToolError", "ToolRegistry", "ToolResult", "truncate", "default_tools",
           "ReadFile", "WriteFile", "EditFile", "Bash", "Glob", "Grep", "SearchDocs", "WebFetch", "WebSearch", "TodoWrite", "Task", "Skill"]
