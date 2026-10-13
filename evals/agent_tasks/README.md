# Agent 任务评测

用 10 个小型编程任务衡量 nanocode + 某个模型的实际完成能力。改了提示词、工具说明、换了模型之后，都应该回来跑一遍，用数字判断是变好还是变差。

## 运行

```bash
# 先验证评测框架本身（不调模型，用参考答案，应该 10/10）
python3 evals/agent_tasks/run.py --reference

# 用真实模型跑（API Key 放在对应环境变量里，例如 DEEPSEEK_API_KEY）
python3 evals/agent_tasks/run.py --provider deepseek --model deepseek-chat --repeat 3
python3 evals/agent_tasks/run.py --provider openrouter --model openrouter/free --tasks T01,T04
```

结果写到 `results/` 下：`.json` 是每次运行的完整数据，`.md` 是汇总表。

**安全**：默认用 bypass 模式，Agent 会在临时目录里直接执行模型生成的命令。任务本身无害，但模型行为无法保证，最好在容器或虚拟机里跑。

## 任务

| 编号 | 考察点 |
|---|---|
| T01 修复多个 bug | 读测试找原因，改完验证 |
| T02 跨文件重命名 | 搜索所有调用点，不遗漏 |
| T03 按规格实现 | 读懂文档字符串、处理边界 |
| T04 分页边界错误 | 定位 off-by-one |
| T05 .env 解析器 | 多条规则同时满足 |
| T06 只读问答 | 在干扰项中找到正确答案，且**不许修改文件** |
| T07 重构 | 行为不变 + 结构要求（AST 检查） |
| T08 命令行参数 | 用子进程测试真实命令行行为 |
| T09 健壮性 | 缺失值、类型转换、非法输入 |
| T10 写测试 | 用 4 个"故意写错的实现"检验测试质量（变异测试） |

## 判定为什么可信

- 判定用的测试放在 `hidden/`，运行判定时才复制进工作目录。Agent 改了自己能看到的测试也没用。
- `tests/test_agent_eval.py` 会检查每个任务：**参考答案必须通过，原样不动必须失败**。判定脚本写错了，单元测试就会报出来。
- T06 会比对所有文件的哈希，改了任何文件都判失败。
- T10 用原版实现运行 Agent 写的测试，防止它改被测代码来迁就测试。

## 用公开基准做外部参照（Aider Polyglot）

自带的 10 个任务偏简单，模型第一次就做对的话，看不出"自动验证 / 重试"这类机制有没有用。可以再用 Aider Polyglot（Exercism 习题）做外部参照。适配脚本在 `evals/polyglot/`，只新增评测代码，不改 agent 本身：

```bash
git clone --depth 1 https://github.com/Aider-AI/polyglot-benchmark /tmp/polyglot-benchmark
python3 evals/polyglot/convert.py --src /tmp/polyglot-benchmark --language python --limit 10 --seed 0
python3 evals/agent_tasks/run.py --tasks-dir evals/polyglot/tasks --reference      # 先自检
python3 evals/agent_tasks/run.py --tasks-dir evals/polyglot/tasks --provider deepseek --model deepseek-chat --repeat 3
```

- 转换时会逐题自检：**参考答案必须通过、桩代码原样必须失败**，不过的题会被跳过并说明原因。
- 每个任务里，桩代码和测试都放在工作目录（Agent 可以自己跑测试），判定时再用 `hidden/` 里的原版测试覆盖回去，所以改测试、删测试都没用。
- `--limit N --seed S` 是固定种子的随机抽样：改动前后一定要用**同一组参数**，比较的才是同一批题。`--exercises a,b` 可以直接指定题目，`--list` 列出全部。
- 目前只支持 `python`（34 题）。其他语言需要对应的工具链和测试命令，加到 `convert.py` 的 `LANGUAGES` 里。
- 生成的任务在 `evals/polyglot/tasks/`，已加入 `.gitignore`，不随仓库分发。题目内容版权归 [Exercism](https://exercism.org)。
- **局限**：这是公开题库，模型可能见过答案，分数适合做改动前后的对比，不适合当绝对能力的结论。

## 局限

10 个任务都很小、都是 Python、都在单个目录里，只能代表"小范围修改"这一类工作。模型有随机性，单次结果波动大，比较时请用 `--repeat 3` 或更多。
