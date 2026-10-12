import re


def slugify(text):
    """把一段文字转成 URL 用的 slug。

    规则：
    1. 全部转小写
    2. 空格和下划线变成连字符 -
    3. 只保留 a-z、0-9 和 -，其他字符（包括中文、标点）删除
    4. 连续多个 - 合并成一个
    5. 去掉首尾的 -

    例：slugify("Hello, World!") == "hello-world"
        slugify("  snake_case  and  spaces ") == "snake-case-and-spaces"
    """
    text = text.lower().replace("_", " ")
    text = re.sub(r"\s+", "-", text.strip())
    text = re.sub(r"[^a-z0-9-]", "", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")
