"""文件工具：read_file / write_file / edit_file。

三个值得注意的设计（都是从编程 Agent 的实践里总结出来的）：

1. 读文件带行号（类似 `cat -n`）。模型引用代码、定位问题时有了"坐标"。
2. 编辑用"精确字符串替换"，而不是整文件重写或 diff：
   - 模型只需输出要改的那一小段，省 token、少出错；
   - old_string 必须在文件中唯一出现，否则拒绝 —— 防止改错地方。
3. "先读后改"：没读过（或读过之后被外部改动过）的文件不允许编辑/覆盖，
   逼模型基于文件的真实内容做修改，而不是凭记忆瞎改。
4. 原子写入（#142）：写文件先写同目录临时文件、落盘，再 os.replace 换上去。
   直接 "w" 打开目标文件会先把文件截断为 0，写到一半失败原内容就丢了。
"""

from __future__ import annotations

import os
import shutil
import tempfile

from .base import Tool, ToolContext, ToolError, open_nofollow

# 猜不出就照这个顺序试，只为在报错里告诉模型"大概是哪种"，不是为了偷偷换种编码读
_GUESSES = ("gb18030", "big5", "shift_jis", "cp1252")


def _decode(raw: bytes, path: str) -> str:
    """把文件字节解成文本；不是 UTF-8 就明确报错。

    以前是 `raw.decode("utf-8", "replace")`：GBK 文件被换成一屏幕 \\ufffd，却照样
    is_error=False 地交给模型，模型对着乱码改文件（#45）。宁可让它知道读不了。
    """
    try:
        return raw.decode("utf-8-sig")          # 带 BOM 的 UTF-8：把 BOM 吃掉，别混进第一行
    except UnicodeDecodeError:
        pass
    for enc in _GUESSES:
        try:
            raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        raise ToolError(f"{path} 不是 UTF-8 编码（按 {enc} 能读通）。请先用 iconv 或编辑器把它转成 UTF-8，"
                        f"再读取或修改——按 \\ufffd 猜出来的内容只会让模型改错地方。") from None
    raise ToolError(f"{path} 不是 UTF-8 编码，也看不出是哪种编码。请转换成 UTF-8 后重试。") from None


def _mark_read(ctx: ToolContext, path: str) -> None:
    st = os.stat(path)
    # 存 (mtime_ns, size) 而不是 float mtime：粗粒度文件系统上同一秒内的修改，
    # float mtime 看不出差别，会漏检（#142）。
    ctx.state.setdefault("read_files", {})[path] = (st.st_mtime_ns, st.st_size)


def _check_fresh(ctx: ToolContext, path: str) -> None:
    seen = ctx.state.setdefault("read_files", {}).get(path)
    if seen is None:
        raise ToolError(f"修改 {path} 之前必须先用 read_file 读取它。")
    st = os.stat(path)
    if (st.st_mtime_ns, st.st_size) != seen:
        raise ToolError(f"{path} 在你读取之后被修改过了，请重新 read_file 再修改。")


def _atomic_write(path: str, text: str) -> None:
    """原子写入：先写同目录临时文件、落盘，再 os.replace 换上去。

    直接 "w" 打开目标文件会先把文件截断为 0：写到一半进程被杀、磁盘满、
    编码异常，原内容就丢了（#142）。临时文件 + os.replace 保证失败时原文件
    原封不动，临时文件随手删掉。path 是 resolve_checked 给出的真实路径，
    os.replace 不会穿过软链接。
    """
    d = os.path.dirname(path) or "."
    existed = os.path.exists(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".nanocode-", suffix=".tmp")
    try:
        # newline=""：跟 write_file/edit_file 一样原样写，不翻译换行符（#45）。
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if existed:
            shutil.copymode(path, tmp)   # 权限位随原文件
        else:
            umask = os.umask(0)
            os.umask(umask)
            os.chmod(tmp, 0o666 & ~umask)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class ReadFile(Tool):
    name = "read_file"
    path_target = True
    is_read_only = True
    description = ("读取一个文本文件，返回带行号的内容（格式：行号<TAB>内容）。"
                   "大文件可以用 offset / limit 分段读取。修改文件之前必须先读取它。")
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径，相对于工作目录或绝对路径"},
            "offset": {"type": "integer", "description": "从第几行开始（1 起），默认 1"},
            "limit": {"type": "integer", "description": "最多读多少行，默认 2000"},
        },
        "required": ["path"],
    }

    def permission_target(self, input):
        return input.get("path", "")

    def run(self, input, ctx):
        path = ctx.resolve_checked(input["path"])
        if not os.path.exists(path):
            raise ToolError(f"文件不存在：{path}")
        if os.path.isdir(path):
            raise ToolError(f"{path} 是目录，请用 glob 查看目录内容")
        with open_nofollow(path, "rb") as f:
            raw = f.read()
        if b"\x00" in raw[:8000]:
            raise ToolError(f"{path} 看起来是二进制文件，无法以文本读取")
        lines = _decode(raw, path).splitlines()
        offset = max(1, input.get("offset", 1))
        limit = input.get("limit", 2000)
        chunk = lines[offset - 1: offset - 1 + limit]
        _mark_read(ctx, path)
        if not lines:
            return "(空文件)"
        body = "\n".join(f"{i}\t{line[:2000]}" for i, line in enumerate(chunk, start=offset))
        if offset - 1 + limit < len(lines):
            body += f"\n... (共 {len(lines)} 行，用 offset={offset + limit} 继续读)"
        return body


class WriteFile(Tool):
    name = "write_file"
    path_target = True
    description = ("创建新文件，或完整覆盖已有文件（覆盖前必须先 read_file）。"
                   "修改已有文件的一部分时，优先使用 edit_file。会自动创建父目录。")
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "content": {"type": "string", "description": "完整的文件内容"},
        },
        "required": ["path", "content"],
    }

    def permission_target(self, input):
        return input.get("path", "")

    def run(self, input, ctx):
        path = ctx.resolve_checked(input["path"])
        existed = os.path.exists(path)
        if existed:
            _check_fresh(ctx, path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if existed:
            _check_fresh(ctx, path)   # 紧挨着写入前再校验一次（#142）
        _atomic_write(path, input["content"])
        _mark_read(ctx, path)
        n = input["content"].count("\n") + 1
        return f"{'已覆盖' if existed else '已创建'} {path}（{n} 行）"


class EditFile(Tool):
    name = "edit_file"
    path_target = True
    description = ("在文件中把 old_string 精确替换为 new_string。old_string 必须与文件内容逐字符一致"
                   "（包括缩进），并且在文件中唯一；不唯一时请多带几行上下文，或设置 replace_all=true。"
                   "编辑前必须先 read_file。注意不要把 read_file 输出里的行号前缀带进来。")
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_string": {"type": "string", "description": "要被替换的原文"},
            "new_string": {"type": "string", "description": "替换后的新文本"},
            "replace_all": {"type": "boolean", "description": "替换所有出现，默认 false"},
        },
        "required": ["path", "old_string", "new_string"],
    }

    def permission_target(self, input):
        return input.get("path", "")

    def run(self, input, ctx):
        path = ctx.resolve_checked(input["path"])
        if not os.path.exists(path):
            raise ToolError(f"文件不存在：{path}（新建文件请用 write_file）")
        # 同样 newline=""：读入时保留文件原本的换行符，写回时才不会把 LF 文件整篇改成
        # CRLF（Windows 默认），产出一大片无意义的 diff（#45）。
        # 先解编码再查"有没有读过"：非 UTF-8 的文件根本读不进来，此时报"必须先 read_file"
        # 会把人引到一条走不通的路上（原来这里是 UnicodeDecodeError 直接穿出去）。
        with open_nofollow(path, "rb") as f:
            raw = f.read()
        if b"\x00" in raw[:8000]:
            raise ToolError(f"{path} 看起来是二进制文件，不能用 edit_file 修改")
        text = _decode(raw, path)
        _check_fresh(ctx, path)
        old, new = input["old_string"], input["new_string"]
        if old == new:
            raise ToolError("old_string 和 new_string 相同，没有需要修改的内容")
        count = text.count(old)
        if count == 0 and "\r\n" in text and "\n" in old:
            # 文件是 CRLF，模型给的 old_string 一般只有 \n：换行符对不上就永远改不动，
            # 按 CRLF 再匹配一次。反过来的方向（文件是 LF）不受影响。
            crlf_old = old.replace("\r\n", "\n").replace("\n", "\r\n")
            crlf_new = new.replace("\r\n", "\n").replace("\n", "\r\n")
            if text.count(crlf_old):
                old, new, count = crlf_old, crlf_new, text.count(crlf_old)
        if count == 0:
            raise ToolError("在文件中找不到 old_string。请确认缩进和空白完全一致，必要时重新 read_file。")
        if count > 1 and not input.get("replace_all"):
            raise ToolError(f"old_string 在文件中出现了 {count} 次。请带上更多上下文让它唯一，或设置 replace_all=true。")
        text = text.replace(old, new) if input.get("replace_all") else text.replace(old, new, 1)
        _check_fresh(ctx, path)   # 紧挨着写入前再校验一次（#142）
        _atomic_write(path, text)
        _mark_read(ctx, path)
        line = text[: text.find(new)].count("\n") + 1 if new else 0
        return f"已修改 {path}（替换 {count if input.get('replace_all') else 1} 处，约第 {line} 行）"
