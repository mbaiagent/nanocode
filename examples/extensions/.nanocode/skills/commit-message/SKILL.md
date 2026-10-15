---
name: commit-message
description: 根据暂存区的改动写一条规范的 git 提交说明（Conventional Commits）
---
# 写提交说明

1. 运行 `git diff --cached --stat` 和 `git diff --cached` 看清改了什么；暂存区为空就先提醒用户 `git add`。
2. 第一行：`<类型>(<范围>): <一句话>`，不超过 72 个字符，不加句号。
   类型只用：feat / fix / docs / refactor / test / chore / perf。
3. 空一行后写正文：为什么改（而不是改了哪些行），影响到的行为，有没有不兼容。
4. 只输出提交说明本身，放在一个代码块里，不要替用户执行 `git commit`。
