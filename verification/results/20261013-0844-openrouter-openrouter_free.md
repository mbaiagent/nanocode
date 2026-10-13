# Agent 任务评测：openrouter/openrouter/free

- 时间：2026-10-13 08:44　nanocode 提交：2b78584　Python 3.12.3　Linux x86_64
- 权限模式：bypass　最大步数：30　单任务超时：600s　每题重复：1
- 自动验证：关
- **通过率：4/13（31%）**

| 任务 | 通过 | 平均步数 | 工具调用 | 工具报错 | 输入 tokens | 输出 tokens | 耗时(s) | 最近一次失败原因 |
|---|---|---|---|---|---|---|---|---|
| T01-fix-bugs | 1/1 | 8.0 | 8.0 | 0.0 | 26819 | 892 | 28.8 |  |
| T02-rename | 1/1 | 7.0 | 14.0 | 0.0 | 23191 | 913 | 37.6 |  |
| T03-implement-spec | 1/1 | 7.0 | 6.0 | 0.0 | 26023 | 2674 | 35.4 |  |
| T04-off-by-one | 1/1 | 9.0 | 10.0 | 1.0 | 27444 | 1926 | 64.6 |  |
| T05-parser | 0/1 | 1.0 | 0.0 | 0.0 | 0 | 0 | 0.3 | FAIL: 隐藏测试 5 个，有失败：FAILED (errors=5) |
| T06-read-only-question | 0/1 | 1.0 | 0.0 | 0.0 | 0 | 0 | 0.4 | FAIL: 回答不完整：函数名✗ 文件✗ |
| T07-refactor | 0/1 | 1.0 | 0.0 | 0.0 | 0 | 0 | 0.4 | FAIL: 隐藏测试 2 个，有失败：FAILED (failures=1) |
| T08-cli-flags | 0/1 | 1.0 | 0.0 | 0.0 | 0 | 0 | 0.3 | FAIL: 隐藏测试 4 个，有失败：FAILED (failures=2) |
| T09-robustness | 0/1 | 1.0 | 0.0 | 0.0 | 0 | 0 | 0.2 | FAIL: 隐藏测试 4 个，有失败：FAILED (failures=2, errors=1) |
| T10-write-tests | 0/1 | 1.0 | 0.0 | 0.0 | 0 | 0 | 0.4 | FAIL: 没有找到 test_mathx.py |
| T11-signature-change | 0/1 | 1.0 | 0.0 | 0.0 | 0 | 0 | 0.4 | FAIL: 隐藏测试 9 个，有失败：FAILED (failures=2, errors=3) |
| T12-spec-edge | 0/1 | 1.0 | 0.0 | 0.0 | 0 | 0 | 0.3 | FAIL: 隐藏测试 6 个，有失败：FAILED (errors=485) |
| T13-fix-regression | 0/1 | 1.0 | 0.0 | 0.0 | 0 | 0 | 0.4 | FAIL: 隐藏测试 5 个，有失败：FAILED (failures=2) |
