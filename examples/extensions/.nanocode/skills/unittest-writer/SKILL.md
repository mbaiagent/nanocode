---
name: unittest-writer
description: 给 Python 函数补单元测试（只用标准库 unittest，不引入 pytest）
---
# 写单元测试

1. 先读被测函数和已有的 test_*.py，沿用它们的命名和结构。
2. 每个测试只验证一件事，名字说明场景：`test_<函数>_<场景>`。
3. 至少覆盖：正常输入、边界（空、0、负数、极大值）、错误输入（用 `assertRaises`）。
4. 不访问网络，不依赖当前时间和随机数；需要文件就用 `tempfile.TemporaryDirectory()`。
5. 写完运行 `python3 -m unittest -v <文件>`，全部通过才算完成；失败就修测试或报告发现的 bug。
