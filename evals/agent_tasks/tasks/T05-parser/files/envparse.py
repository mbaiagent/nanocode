def parse_env(text):
    """解析 .env 文件内容，返回 dict。

    - 每行 KEY=VALUE，键和值两边的空白去掉
    - 空行和以 # 开头的行忽略
    - 行首可以有 "export "，忽略它
    - 值可以用双引号或单引号包起来，引号去掉，引号里的 # 是普通字符
    - 没有引号的值，" #" 之后是注释，要去掉
    - 没有 = 的行忽略
    """
    raise NotImplementedError
