"""把文章标题变成 URL 里用的 slug。"""

import re


def slugify(text):
    """小写；字母和数字（包括中文等各种语言的字母）原样保留，其他字符变成连字符；首尾不留连字符。"""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def post_url(title):
    return "/posts/" + slugify(title)
