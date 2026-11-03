# 候选修复评估

四个自编项目采用 development/holdout 分组，并单独保存回归测试。先收集诊断，再按差异指纹审阅和复验；分组不等于第三方盲测。

运行入口与结果解释见[演示与测试](../../docs/TESTING.md)，真实任务见[使用指南](../../docs/USAGE.md)。

完整离线演示：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m examples.ci_walkthrough
```

运行产生的报告保存在独立的新目录。不要改写历史回执或把已观察样本称为新的盲测。
