# 验证结果汇总

每完成一项就更新对应的一行。"要点"一栏写一句话结论，例如"8/10 通过，T05、T10 失败"。

| 项 | 内容 | 状态 | 要点 | 结果文件 |
|---|---|---|---|---|
| V01 | 环境、单元测试、评测框架自检 | PASS | 75 个单测 OK，评测自检 10/10 | results/V01.md |
| V02 | Mock 模型端到端（命令行） | PASS | 5 检查点全过；trace 37 事件无错误 | results/V02.md |
| V03 | Web 界面（浏览器实操） | ✅ PASS | 8/8，见 V03.md | 2026-10-13 |
| V04 | Web 安全（跨站请求、DNS 重绑定、令牌） | ✅ PASS | 8/8，见 V04.md | 2026-10-13 |
| V05 | 工作区信任 | PASS | 未信任忽略/--trust生效/记住信任/改配置失效，4项全过 | results/V05.md |
| V06 | 路径权限（`~`、软链接、`..`） | PASS | 8 项行为与预期逐项一致 | results/V06.md |
| V07 | 真实模型：OpenAI 兼容协议，基础对话 | PASS | 退出码0、一句自我介绍、token精确统计、无错误 | results/V07.md |
| V08 | 真实模型：修 bug 完整流程（工具调用、权限、hooks、MCP） | PARTIAL | bug修对4/4通过；allow规则被`cd&&`前缀绕过致bash全拒（候选issue） | results/V08.md |
| V09 | 真实模型：Anthropic 协议（可选） | PARTIAL | 直连 OpenRouter 崩溃（#18 未修）；经本地翻译桥走 Gemini 基础对话+工具调用全通 | results/V09.md |
| V10 | 真实模型：search_docs 与子 Agent | PARTIAL | 检索/索引/子Agent/file数12=12全对；出处引了转引非一手 | results/V10.md |
| V11 | RAG 向量 / 混合检索评测 | PASS | 补做（2026-10-15，OpenRouter qwen3-embedding-8b）：vector R@1 77% / MRR 0.84，bm25 R@1 36%；hybrid R@3 95%；22 题样本小 | results/V11.md |
| V12 | Agent 任务评测（13 个任务） | PASS | 第三轮 DeepSeek V4 Flash、无 shim、`--repeat 3`：37/39；T10 1/3（漏 mutant_4，已分析为模型能力限制）。前两轮 11/13 | results/V12.md |
| V13 | 真实模型：验证闭环（`--verify`、终审、子任务验收、经验候选） | PASS | 5/5 检查点真实模型通过（verify 打回修好、终审 diff、子任务验收+越界拦截、经验候选） | results/V13.md |

## 2026-10-13 19:00：#16/#17/#18/#20 修复验证并关闭
- 拉到新基线 `0422d2a`（含 fix/anthropic-done-sentinel、fix/openai-extra-content、fix/bash-compound-rules、feat/web-fetch）
- #16：19 个单测全过 + 4 行为场景符合设计（工作区内 cd→allow、工作区外→ask、搭便车→ask、deny 段→deny）
- #17：gemini-3.1-flash-lite 直连 Google OpenAI 端点两轮工具调用成功，无 400（token 6709/78）
- #18：OpenRouter Anthropic 端点基础调用成功，无 TypeError
- #20：同一次调用正常结束，流末尾 `[DONE]` 被跳过，无 JSONDecodeError
- 四个 issue 均已评论证据并关闭

## 2026-10-13 23:40：V12 重跑
- 评测框架自检 `--reference` 13/13；`deepseek/deepseek-v4-flash-0731`（OpenRouter 付费）`--repeat 3` 完整跑完，37/39，T10 1/3；T10 复跑 `--repeat 5 --keep` 0/5，每次都漏 `mutant_4`（`x >= hi + 1`，只有 `hi < x < hi + 1` 的浮点数才能区分）
- 发现并修复评测脚本问题：API 429 被当成模型失败（Gemini 额度用完时 0/13），`fix/eval-infra-errors` 已合入
