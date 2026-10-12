# 验证结果汇总

每完成一项就更新对应的一行。"要点"一栏写一句话结论，例如"8/10 通过，T05、T10 失败"。

| 项 | 内容 | 状态 | 要点 | 结果文件 |
|---|---|---|---|---|
| V01 | 环境、单元测试、评测框架自检 | PASS | 75 个单测 OK，评测自检 10/10 | results/V01.md |
| V02 | Mock 模型端到端（命令行） | PASS | 5 检查点全过；trace 37 事件无错误 | results/V02.md |
| V03 | Web 界面（浏览器实操） | ✅ PASS | 8/8，见 V03.md | 2026-10-13 |
| V04 | Web 安全（跨站请求、DNS 重绑定、令牌） | 待验证 | | |
| V05 | 工作区信任 | PASS | 未信任忽略/--trust生效/记住信任/改配置失效，4项全过 | results/V05.md |
| V06 | 路径权限（`~`、软链接、`..`） | PASS | 8 项行为与预期逐项一致 | results/V06.md |
| V07 | 真实模型：OpenAI 兼容协议，基础对话 | 待验证 | | |
| V08 | 真实模型：修 bug 完整流程（工具调用、权限、hooks、MCP） | 待验证 | | |
| V09 | 真实模型：Anthropic 协议（可选） | BLOCKED | 无 ANTHROPIC_API_KEY | results/V09.md |
| V10 | 真实模型：search_docs 与子 Agent | 待验证 | | |
| V11 | RAG 向量 / 混合检索评测 | PARTIAL | 基线完成（bm25 R@3 91%）；无 embedding Key | results/V11.md |
| V12 | Agent 任务评测（10 个任务） | 待验证 | | |
