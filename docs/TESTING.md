# 测试与验证

使用 Python 3.12，在源码仓库根目录安装开发依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

## 目录职责

- `tests/test_*.py`：运行时、权限、恢复、协议和 CI 工作流的正式回归测试。
- `tests/support/`：测试共用的固定响应、隔离工作区与集成检查。
- `tests/fixtures/`：自编工程、预期失败、回归数据和评估清单；不属于应用源码。
- `scripts/verify.py`：分组验证、超时与取消、日志、汇总及源码指纹。

测试默认使用固定响应或本地模拟 HTTP，不需要模型密钥。涉及 pytest 的子进程在临时工程副本中执行。测试数据中故意保留的失败，用于检验诊断、回归拒绝和回滚；不要单独扫描整个 fixtures 目录并期待全部通过。

## 执行检查

Windows 一键运行分组验证：

```powershell
.\verify.cmd
```

等价的 Python 入口，可指定尚不存在的报告目录：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts/verify.py --output-dir .personal/verification-example
```

默认报告保存在 `.personal/verification/`，不会写入测试源码目录。每组都有 JSON 和日志；汇总检查工作区源码在执行前后是否一致。总控只依据当前运行结果，不依赖个人历史报告。

完整回归测试：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -p "test_*.py"
```

单独检查核心循环：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m unittest tests.test_agent
```

分组验证和全量 unittest 会复用流程，测试计数不能相加。`--live` 仅在显式指定时请求配置的模型服务，会产生实际用量；默认验证不使用它。

## 流程复现

- `.\demo.cmd`：运行包也能使用的文件、Plan 与审批示例。
- `python -m tests.support.agent_loop_walkthrough`：源码仓库中的成功、拒绝和次数上限轨迹。
- `python -m tests.support.ci_walkthrough`：源码仓库中的诊断、候选复验、回归拒绝、采纳及回滚。详见 [CI 流程复现](DEMO.md)。

上述固定响应检查证明执行链路，不代表实时模型质量。

## GitHub Actions

[Windows CI](../.github/workflows/windows-ci.yml) 使用同一分组总控，保留 JSON、日志、汇总和源码指纹，不配置模型密钥。完整 unittest discovery 另列一步，覆盖全部正式测试。

[容器检查](../.github/workflows/container-acceptance.yml) 在独立 Linux 环境构建并运行镜像，检查进程、权限、持久化和停止信号。镜像所需源码、示例及配置变化会触发该工作流。修改后的远端验证结果应以对应提交的 Actions 为准，本地模拟引擎通过不能代替真实容器运行。

运行包不包含 `tests/`、`scripts/`、CI 配置、开发依赖和本地报告。完整开发检查需使用源码仓库。
