# CI 日志分诊任务包

任务包含 task.json、SKILL.md、development.json 和 holdout.json。真实模型可读取输入、写入 triage.json 并读回；固定响应验证只证明执行和验收流程。

运行入口与结果解释见[演示与测试](../../../docs/TESTING.md)，真实任务见[使用指南](../../../docs/USAGE.md)。

完整离线演示：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m examples.ci_walkthrough
```

运行产生的报告保存在独立的新目录。不要改写历史回执或把已观察样本称为新的盲测。
