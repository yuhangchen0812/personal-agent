---
name: fork-code-review
description: 在独立子任务中读取代码并解释入口、调用关系与问题依据。
when-to-use: 需要隔离调查过程，只把代码检查结论带回当前对话时
allowed-tools: ["read_file", "grep_search", "list_files"]
user-invocable: true
context: fork
---

# 独立代码调查

任务参数：$ARGUMENTS

1. 先定位并读取任务涉及的代码，说明入口、输入、处理过程和输出。
2. 如需了解调用关系，再搜索直接调用方或被调用函数，控制调查范围。
3. 返回带文件位置的结论、已验证证据和仍不确定的部分。
4. 根据实际工具结果作答，方法文字本身不代表任务已经完成。

技能所在目录：${CLAUDE_SKILL_DIR}
