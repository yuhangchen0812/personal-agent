# pytest / JUnit 输入

此自编工程用于实际产生包含失败、错误和跳过项的 JUnit XML。初始失败是样例的一部分，不代表主项目测试失败。

源码仓库的运行入口与结果解释见[测试说明](https://github.com/yuhangchen0812/personal-agent/blob/main/docs/TESTING.md)，真实任务见[使用指南](https://github.com/yuhangchen0812/personal-agent/blob/main/docs/USAGE.md)。

完整离线演示：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m tests.support.ci_walkthrough
```

此复现入口需要完整源码仓库与开发依赖，不随运行包分发。运行产生的报告保存在独立的新目录。
