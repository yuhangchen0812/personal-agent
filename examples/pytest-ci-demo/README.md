# pytest / JUnit 输入

此自编工程用于实际产生包含失败、错误和跳过项的 JUnit XML。初始失败是样例的一部分，不代表主项目测试失败。

运行入口与结果解释见[演示与测试](../../docs/TESTING.md)，真实任务见[使用指南](../../docs/USAGE.md)。

完整离线演示：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m examples.ci_walkthrough
```

运行产生的报告保存在独立的新目录。不要改写历史回执或把已观察样本称为新的盲测。
