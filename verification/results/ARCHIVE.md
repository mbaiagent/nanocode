# 测试与验证归档索引

这里汇总 2026-10-13 至 2026-10-15 期间所有测试、评测、压测、验证的过程和结果：做了什么、用什么命令、基于哪个提交、结果是什么、原始输出在哪里。
原始输出放在 `raw/<日期>/`；每个评测的完整结果在 `evals/agent_tasks/results/`；各项验证的详细记录在 `verification/results/Vxx.md`。

## 一、单元测试与平台测试

| 日期 | 命令 | 基于提交 | 结果 | 原始输出 |
|---|---|---|---|---|
| 2026-10-15 | `python3 -m unittest discover -s tests` | 9948b6e（main） | 371 个测试，OK | 只记录结论，未另存 |
| 2026-10-15 | `python3 -m unittest discover -s server/tests` | 9948b6e（main） | 72 个测试，OK，9 个按条件跳过（需要 `NCS_TEST_*` 环境变量） | 只记录结论，未另存 |
| 2026-10-15 | `python3 -m unittest discover -s tests -p test_docs_consistency.py` | 修改 `docs/10-rag.md` 之后 | OK（文档数字与代码一致） | 只记录结论 |

注：最近几次合并（load-test、workflow-guide）都是在合并前后各跑一次两套测试，结论都是 OK。更早的分支只记录了合并前的结果。

## 二、Agent 任务评测（`evals/agent_tasks`）

| 日期 | 模型 | 命令 | 结果 | 完整结果 |
|---|---|---|---|---|
| 2026-10-13 | DeepSeek V4 Flash（OpenRouter） | `run.py --provider openrouter --model deepseek/deepseek-v4-flash-0731 --repeat 3` | 37/39（95%）；T10 漏掉 `mutant_4` | `evals/agent_tasks/results/20261013-2322-…json`，`verification/results/V12.md` |
| 2026-10-13 | 参考答案自检 | `run.py --reference` | 13/13 | `evals/agent_tasks/results/20261013-2051-reference--.json` |
| 2026-10-15 | gpt-oss-20b（OpenRouter，最便宜的付费 tools 模型之一） | `run.py --provider openrouter --model openai/gpt-oss-20b --repeat 3` | **进行中**，完成后更新本表 | 完成后写入 `evals/agent_tasks/results/` |

## 三、检索评测（RAG，`examples/rag_eval`）

| 日期 | 运行 | 语料 | 结果 | 原始输出 | 状态 |
|---|---|---|---|---|---|
| 2026-10-13 | 基线（11 篇、102 块） | 旧版 docs | bm25 R@3 91%，grep-关键词 73% | `verification/results/V11.md` 的第一部分 | 有效，但语料已变 |
| 2026-10-15 | 向量（语料混用，grep 用 32 篇、BM25 和向量用 17 篇） | 不一致 | 数字作废 | `raw/2026-10-15/v11-superseded-32files.txt` | **作废**（评测缺陷，已修正） |
| 2026-10-15 | 基线（修正后，17 篇、221 块） | docs 顶层 | bm25 R@1 36%，R@3 91%，MRR 0.61；grep-关键词 R@3 32% | `raw/2026-10-15/v11-baseline.txt` | 有效 |
| 2026-10-15 | 向量 + 混合（修正后） | docs 顶层 | vector R@1 77%，MRR 0.84；hybrid R@3 95% | `raw/2026-10-15/v11-vector.txt` | 有效，正式结果 |

## 四、压测（`nanocode_server loadtest`）

| 日期 | 命令 | 基于提交 | 结果 | 原始输出 |
|---|---|---|---|---|
| 2026-10-15 | `loadtest --users 10 25 50 100`（修复监听队列之后的最终版本） | 2d778ac（合并为 d195440） | 10/25/50/100 全部成功；建会话 p95 2.7 / 6.3 / 12.5 / 27.3 秒；沙箱内存 207 / 514 / 1026 / 2048 MB | `raw/2026-10-15/loadtest-final.txt`，分析见 `perf-loadtest.md` |
| 2026-10-15 | 修复前的同一命令（队列长度 5） | 修复前 | 25 个用户有 1 个连接被重置，50 个有 17 个失败 | 未另存，记录在 `perf-loadtest.md` |
| 2026-10-15 | 瓶颈诊断（插桩计时、`docker run` 对比、`vmstat`） | 2d778ac | 建会话时间主要在容器启动到就绪；Docker 守护进程约每秒 10 个容器；整机 CPU 平均约 50% | 未另存，结论记录在 `perf-loadtest.md` |

压测过程中出现的 16 个"不明进程"后来确认是压测留下的沙箱容器，已清理。

## 五、多用户演示（`nanocode_server demo`）

| 日期 | 命令 | 结果 | 原始输出 |
|---|---|---|---|
| 平台开发阶段（提交 f066701 之前，具体日期未记录） | `python3 -m nanocode_server demo --users 10` | 10 个用户 51/51 项隔离检查通过 | 未另存 |

## 六、真实环境验证（`verification/`）

| 项 | 状态 | 记录 |
|---|---|---|
| V01–V13 | V01–V07、V12、V13 为 PASS；V08–V11 为 PARTIAL（因缺 Key 或外部条件限制，已写明）；V11 于 2026-10-15 补做为 PASS | `verification/results/SUMMARY.md` 与各 `Vxx.md` |

## 七、模型与价格调研（2026-10-15）

| 内容 | 命令或来源 | 归档文件 |
|---|---|---|
| OpenRouter 模型列表（389 个支持 tools 且有价格的模型）与最便宜的候选 | `GET https://openrouter.ai/api/v1/models`，筛选 | `raw/2026-10-15/openrouter-cheapest-tools-models.txt` |
| OpenRouter embedding 模型列表（33 个） | `GET https://openrouter.ai/api/v1/embeddings/models` | `raw/2026-10-15/openrouter-embedding-models.txt` |
| qwen3-embedding-8b 连通性测试（3 句，相似度 0.734 / 0.528） | 直接调用 `nanocode.rag.make_embedder` | 未另存，结论见 V11 |

## 八、已作废或需注意的记录

- **V11 第一版向量结果（语料不一致）**：作废，原因和修正见 `V11.md`。
- **RAG 语料描述**：docs 下的"32 篇"包括 `docs/platform/`，但检索索引只含顶层 17 篇。以 `docs/10-rag.md` 的说明为准。
- **`verification/TASKS.md` 中"OpenRouter 不一定提供 embedding 接口"**：已过时。
- **压测中的沙箱容器**：已清理，不计入任何结果。

## 九、如何复现

```bash
python3 -m unittest discover -s tests
python3 -m unittest discover -s server/tests
python3 evals/agent_tasks/run.py --reference
python3 examples/rag_eval/run_eval.py                                         # 基线
NANOCODE_API_KEY="$OPENROUTER_API_KEY" python3 examples/rag_eval/run_eval.py \
    --embed-provider "" --embed-base-url https://openrouter.ai/api/v1 \
    --embed-model qwen/qwen3-embedding-8b --verbose                           # 向量与混合
python3 -m nanocode_server loadtest --users 10 25 50 100
```

API Key 只从环境变量读取，不写进命令或文件。
