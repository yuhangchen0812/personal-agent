# 演示与测试

## 安装开发依赖

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

测试使用固定响应、SDK 消息对象或本地模拟 HTTP；不需要真实模型密钥。CI 任务中执行 pytest 的子进程使用自编工程副本，故意保留初始失败和回归失败。

## 三个演示入口

| 入口 | 验证内容 |
| --- | --- |
| `.\demo.cmd` | 文件读写、Plan 与审批、HTML 和报告 |
| `python -m examples.agent_loop_walkthrough` | 成功/拒绝/上限三条消息流，执行入口和处理函数分别计数 |
| `python -m examples.ci_walkthrough` | CI 诊断、差异审阅、公开/独立复验、拒绝回归、采纳后回滚 |

使用项目虚拟环境中的 Python 执行命令。每次演示创建新的 `.personal` 目录，不覆盖旧结果。

## 本地测试

运行完整的离线默认验收：

```powershell
.\verify.cmd
```

当前共 28 组，包含新增的 core_unit 核心回归组，以及工具、协议、恢复和应用集成组。总控保留逐组 JSON、日志及汇总，检查源码在运行前后是否一致；默认不请求模型、不要求私人历史文件。也可指定新报告目录：

```powershell
.\verify.cmd --output-dir .personal/verification-example
```

按文件检查核心循环与路径规则：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -p test_agent.py
.\.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -p test_tools.py
.\.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -p test_agent_config.py
```

全量 unittest discovery：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -p "test_*.py"
```

全量 unittest 和默认分组中存在共用流程，计数不能相加。tests 中部分用例复用 codex 验证模块，因此不要删除 codex Python 文件。

## GitHub Actions

[Windows CI](../.github/workflows/windows-ci.yml) 使用同一个总控运行 28 个默认组，保留逐组 JSON、日志、汇总和源码指纹，不配置模型密钥、不运行 live_smoke。容器配置有单独的[验收工作流](../.github/workflows/container-acceptance.yml)，镜像中 examples 或 playground/README.md 变化也会触发检查。

有完整本机历史报告时可显式使用 `.\verify.cmd --history`。此模式仍会核对历史字段和文件；缺失或失败会导致总结果失败，不会重新调用历史模型，也不会将缺失文件当作通过。

汇总保留旧版 m3 兼容字段；本地总控不据此判断跨环境全面完成。实际部署结果以独立工作流和对应提交的报告为准。

原始公开提交的 Windows 与 Linux 容器均已远端运行通过；后续提交应检查各自的实际工作流状态。验证范围和链接见 [VALIDATION.md](VALIDATION.md)。

## 怎样解释结果

- 固定响应验证的是执行链路，不是实时模型质量。
- completed 表示循环结束；evaluated 表示测量完成；published 表示本地报告生成。
- 自编 holdout 分组不是第三方盲测；已观察的样本不能继续作为未见证据。
- 公开测试通过后仍可能新增回归；没有独立测试时，不能据公开结果宣称完整修复。
- Windows Job 提供进程资源限制，不构成文件系统或网络安全沙箱。

公开版实际核验记录见 [VALIDATION.md](VALIDATION.md)。
