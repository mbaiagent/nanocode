def parse_env(text):
    """解析 .env 文件内容，返回 dict。

    - 每行 KEY=VALUE，键和值两边的空白去掉
    - 空行和以 # 开头的行忽略
    - 行首可以有 "export "，忽略它
    - 值可以用双引号或单引号包起来，引号去掉，引号里的 # 是普通字符
    - 没有引号的值，" #" 之后是注释，要去掉
    - 没有 = 的行忽略
    """
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        out[key] = value
    return out
