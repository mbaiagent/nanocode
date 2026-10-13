"""把文章标题变成 URL 里用的 slug。"""


def slugify(text):
    """小写；字母和数字（包括中文等各种语言的字母）原样保留，其他字符变成连字符；首尾不留连字符。"""
    out = []
    for ch in text.lower():
        out.append(ch if ch.isalnum() else "-")
    return "".join(out).strip("-")


def post_url(title):
    return "/posts/" + slugify(title)
