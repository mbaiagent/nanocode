# 安全模型与信任边界（单机）

> 本文只讲 nanocode 核心（单机 CLI / `nanocode --web`）的安全边界。多用户平台（nanocode-server：控制面、模型网关、沙箱、出站代理、快照）的安全模型已随平台移到私有仓库。
> 单机的攻击面和修复记录见 [README「安全」](../README.md#安全) 和 [第 04 章](04-permissions-and-hooks.md)。
> 发现问题请提 issue（本仓库公开：不要在 issue 里贴真实 IP、域名、密钥）。

## 一、一句话

**权限系统是启发式闸门，不是沙箱。** 单机模式下 Agent 直接跑在你的电脑上，没有隔离；权限模式、allow/deny 规则、风险分类只是减少误操作和提示词注入的损失。需要真正的隔离，请放进容器 / 虚拟机里跑。

## 二、信任边界

| 边界 | 谁在里面 | 挡什么 | 靠什么 |
|---|---|---|---|
| 单机 Agent ↔ 你的电脑 | 本地 nanocode | 误删、越界写、陌生项目配置执行命令 | 权限模式、真实路径判定、复合命令拆段、项目配置信任（`trust.py`）。**没有隔离** |

## 三、权限判定

- 权限判定失败安全：deny → plan → allow → bypass → 只读 → accept_edits → ask，非交互时 ask 按 deny；路径按真实路径判定（`~`、`..`、软链接），执行前复核（#42 / PR #108）。
- 项目配置里的敏感键（hooks、MCP、`base_url`、`permissions.allow` 等）首次使用要信任确认并记内容指纹。

## 四、已知弱点

| 问题 | 影响 | 现在的缓解 | 跟踪 |
|---|---|---|---|
| 读文件默认放行，`.env`、`*.pem`、`*.key`、`id_rsa` / `id_ed25519`、`.netrc`、`.aws/credentials` 被读时不确认 | 提示词注入可以把这些内容读进上下文 | 读取这些文件默认询问、非交互拒绝，宽泛 allow 规则不放行；bash 常见读法启发式识别（`python -c`、`grep -r` 等看不穿）；模式可由 `permissions.sensitive_read(_exclude)` 和平台策略调整，见 [04 章](04-permissions-and-hooks.md#敏感文件读取136) | [#136](https://github.com/mbaiagent/nanocode/issues/136) |
| 权限分类是字符串启发式：`bash -c "…"`、`xargs`、别名、变量展开看不穿；`risk.py` 只做正则识别 | 不能把 allow/deny 规则当成安全边界 | 容器隔离兜底；`review` 模式高风险永远确认 | 设计如此，见 [第 04 章](04-permissions-and-hooks.md)、[第 11 章](11-verification-loop.md) |
