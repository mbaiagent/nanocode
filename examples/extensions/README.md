# 扩展示例：MCP 服务器 + 技能 + 自定义命令

在这个目录里启动 nanocode 就能看到效果（项目配置里的 MCP 服务器会执行命令，第一次会询问是否信任）：

```bash
cd examples/extensions
python3 -m nanocode --provider openrouter --model <模型>   # 或 --provider mock 只看扩展列表
> /mcp       # 四个 MCP 服务器的连接状态和工具
> /skills    # 三个技能、一个自定义命令
```

Web 界面（`python3 -m nanocode --web`）右侧的"扩展"面板显示同样的内容。

| 类型 | 名字 | 说明 | 依赖 |
| --- | --- | --- | --- |
| MCP | demo | 仓库自带的最小 MCP 服务器（word_count / save_note） | python3 |
| MCP | fs | 官方文件系统服务器，只能访问当前目录 | Node.js（npx） |
| MCP | memory | 官方知识图谱记忆（默认存在 npx 缓存里；要换位置就在 env 里设 MEMORY_FILE_PATH 为绝对路径） | Node.js（npx） |
| MCP | fetch | 官方网页抓取，转成 Markdown | uv（uvx） |
| 技能 | commit-message | 按 Conventional Commits 写提交说明 | — |
| 技能 | unittest-writer | 用标准库 unittest 补测试 | — |
| 技能 | web-research | 抓网页 → 提炼要点 → 记进 memory | fetch + memory |
| 命令 | /count | 用 demo 服务器统计字数 | demo |

缺了 Node.js 或 uv 也没关系：对应的服务器在 `/mcp` 里显示 ✗ 和失败原因，其余照常可用。
