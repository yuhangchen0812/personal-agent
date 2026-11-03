# 正式回归测试

这里保存需要随源码维护的测试，不保存个人开发日志、临时排错脚本或历史验收报告。

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -p "test_*.py"
.\verify.cmd
```

`test_*.py` 为正式用例；`support/` 为共用固定响应和集成检查；`fixtures/` 为只读测试输入。fixtures 中的业务工程可能故意存在缺陷，不要将其当作待修复的应用源码，也不要对它们单独执行全目录 pytest。

测试在临时目录中复制、执行和修改这些输入。新增或调整用例时，保留真实失败退出码，并区分固定模型、本地工具、真实服务及真实容器的证据。

运行输出统一放在被 Git 忽略的 `.personal/` 或系统临时目录；测试与测试输入不进入运行包。

更多入口见[测试说明](../docs/TESTING.md)。
