---
name: code-review
description: "检查代码中的主要问题，并解释依据。"
when-to-use: 用户要求检查一段代码或改动时
allowed-tools: ["read_file", "grep_search", "list_files"]
user-invocable: true
context: inline
---

# 代码检查方法

任务参数：$ARGUMENTS

1. 先读取相关代码，说明输入、输出及主要调用关系。
2. 优先检查会导致错误结果、数据丢失或权限绕过的问题。
3. 每个结论注明文件位置、触发条件和建议修复方向。
4. 区分已验证事实与推测；验证保持必要且精简。

技能所在目录占位符：${CLAUDE_SKILL_DIR}
