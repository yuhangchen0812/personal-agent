# 显式测试配置

自编 Python 工程配合固定 pytest 配置，在副本运行原版与候选；不安装工程依赖。候选需独立审阅，不提供操作系统沙箱。

运行入口与结果解释见[演示与测试](../../docs/TESTING.md)，真实任务见[使用指南](../../docs/USAGE.md)。

完整离线演示：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m examples.ci_walkthrough
```

运行产生的报告保存在独立的新目录。不要改写历史回执或把已观察样本称为新的盲测。
