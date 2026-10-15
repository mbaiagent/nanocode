---
name: web-research
description: 查网页资料并把要点记进知识图谱（需要 fetch 和 memory 两个 MCP 服务器）
---
# 查资料并记下来

1. 用 `mcp__fetch__fetch` 抓取用户给的网址（太长就分段，`start_index` 往后翻）。
2. 提炼 3～5 条要点，每条一句话，注明来源网址。
3. 用 `mcp__memory__create_entities` 把主题记成一个实体（entityType 用 "topic"），要点放进 observations；
   已经有这个实体就用 `mcp__memory__add_observations` 追加。
4. 回答时列出要点，并说明已经记录到哪个实体。
