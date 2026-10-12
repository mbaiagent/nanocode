# 验证交接说明（给执行验证的 Agent）

这个目录用来交接"只能在真实环境里做"的验证工作：连接真实模型、打开浏览器、跑完整评测。
开发环境连不上外部模型服务，所以这些步骤需要你在本机完成，并把结果写回这里。

## 你要做什么

1. 按顺序完成 [TASKS.md](TASKS.md) 里的 V01 到 V12，**一次只做一项**。
2. 每完成一项，把结果写进 `results/Vxx.md`（照 [results/_TEMPLATE.md](results/_TEMPLATE.md) 的格式），并更新 [results/SUMMARY.md](results/SUMMARY.md) 里对应的一行。
3. 每完成一项就提交并推送一次（见下面的"提交方式"），这样即使中途停下，已完成的结果也能被看到。

## 规则（重要）

- **不要修改项目源码**（`nanocode/`、`tests/`、`evals/agent_tasks/tasks/`、`examples/`、`docs/`）。发现 bug 时，把复现步骤、实际输出、预期输出写进结果文件，标记为 FAIL，然后继续下一项。修复由开发方来做。
- **只允许写入**：`verification/results/` 和 `evals/agent_tasks/results/`。
- **绝不能把 API Key、令牌、密码写进任何文件或提交信息。** Key 只放在环境变量里。粘贴命令输出前检查一遍，出现 `sk-`、`Bearer`、`api_key` 字样的内容要替换成 `<已隐藏>`。
- 所有实验都在**临时目录**里进行，不要在你自己的重要项目目录里运行 nanocode。
- 输出很长时，只粘贴关键的 20~60 行，并说明省略了什么。不要编造或"修饰"输出，原样粘贴。
- 某一项因为条件不具备做不了（例如没有 Anthropic 的 Key），标记为 **BLOCKED** 并写明缺什么，继续下一项。
- 一项里有多个检查点时，逐个写明通过与否，不要只写一个总结论。

## 状态的含义

| 状态 | 含义 |
|---|---|
| PASS | 所有检查点都符合预期 |
| FAIL | 至少一个检查点不符合预期（写清楚是哪个、实际是什么） |
| PARTIAL | 部分检查点因条件不具备没做，已做的都通过 |
| BLOCKED | 整项无法进行（写明原因） |

## 开始之前

```bash
git clone https://github.com/mbaiagent/nanocode.git && cd nanocode   # 已有就 git pull
export NANOCODE_HOME="$(mktemp -d)"     # 让 nanocode 的个人数据写到临时目录，不影响本机
export REPO="$(pwd)"
```

每次开新终端都要重新执行后两行。所有命令都假设当前目录是仓库根目录 `$REPO`，除非步骤里另有说明。

## 提交方式

```bash
cd "$REPO"
git add verification/results evals/agent_tasks/results
git status --short            # 确认只有这两个目录下的文件有变化
git diff --cached | grep -iE "sk-|bearer|api_key" && echo "发现疑似密钥，先处理掉再提交"
git commit -m "verification: Vxx <一句话结果>"
git pull --rebase && git push
```
