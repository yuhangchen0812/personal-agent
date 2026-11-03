# 测试

测试以 unittest discovery 运行；部分集成测试复用 codex 中的验证函数，并通过 pytest 子进程执行自编工程。先安装 requirements-dev.txt。

入口、CI 和结果解释见 [TESTING.md](../docs/TESTING.md)。固定响应不请求模型服务；涉及真实模型的可选命令必须显式选择。
