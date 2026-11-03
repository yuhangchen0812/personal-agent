# CI 日志输入

这里是自编 run.json/results.jsonl 输入，供单次或批量 CI 分诊。日志不是外部真实事故，分诊通过不表示原 CI 全绿。

运行入口与结果解释见[演示与测试](../../docs/TESTING.md)，真实任务见[使用指南](../../docs/USAGE.md)。

完整离线演示：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m examples.ci_walkthrough
```

运行产生的报告保存在独立的新目录。不要改写历史回执或把已观察样本称为新的盲测。
