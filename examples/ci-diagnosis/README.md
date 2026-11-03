# 源码证据诊断

演示从失败报告和源码快照形成引用及候选差异。模型假设须经过后续审阅和复验，生成候选不等于修复通过。

运行入口与结果解释见[演示与测试](../../docs/TESTING.md)，真实任务见[使用指南](../../docs/USAGE.md)。

完整离线演示：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m examples.ci_walkthrough
```

运行产生的报告保存在独立的新目录。不要改写历史回执或把已观察样本称为新的盲测。
