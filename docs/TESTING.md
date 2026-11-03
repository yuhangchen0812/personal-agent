# 测试与验证

使用 Python 3.12，在源码仓库根目录安装开发依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

## 检查层次

| 检查 | 目的 | 默认是否请求模型服务 |
| --- | --- | --- |
| 完整 unittest 回归 | 核心行为、关键错误和集成路径，当前 213 项 | 否 |
| 分组离线验证 | 当前 28 组，保留逐组 JSON、日志、汇总及源码指纹 | 否 |
| 新环境安装验收 | 在新的 venv 中安装依赖，检查入口、会话和停止信号 | 否；需要联网下载 Python 依赖 |
| 独立容器工作流 | 真实构建与运行、权限、卷、进程恢复和停止信号 | 否；需要可用 Docker Engine |
| `--live` 冒烟 | 调用配置的真实模型服务并执行指定工具链 | 是，仅显式启用 |

分组与全量回归复用部分流程，计数不能相加。固定响应或模拟 HTTP 通过不代表模型任务质量，具体结果应绑定实际运行的代码与环境。

## 本地入口

完整回归：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -p "test_*.py"
```

分组验证：

```powershell
.\verify.cmd
```

等价 Python 入口，可指定尚不存在的报告目录：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B scripts/verify.py --output-dir .personal/verification-example
```

默认报告保存在 `.personal/verification/`。总控根据当前子进程退出码与结果判断，通过前后源码指纹检查输入是否改变；不依赖个人历史报告。

独立安装验收会新建运行包、venv 并从 PyPI 下载依赖：

```powershell
.\demo.cmd release-check --output-dir .personal/clean-install
```

需要新的输出目录。该检查使用同机 Python 基础解释器和独立源码、依赖及工作目录，不等于新的操作系统环境。

## 测试目录

- `tests/test_*.py`：正式回归用例。
- `tests/support/`：共用固定响应、隔离工作区及集成检查。
- `tests/fixtures/`：只读自编工程、预期失败、回归数据和评估清单。

涉及 pytest 的进程在临时工程副本中执行。fixtures 的初始失败和回归失败是验证输入，不是应用自身的失败用例；不要对整个 fixtures 目录单独执行全量 pytest。

运行输出放在被忽略的 `.personal/` 或系统临时目录。运行包不包含这些正式测试、仓库验证脚本、CI 配置或开发依赖。

## 完整流程与 CI

- `.\demo.cmd`：文件、Plan 和审批的基础示例，运行包也可使用。
- `python -m tests.support.agent_loop_walkthrough`：成功、拒绝和次数上限的消息轨迹。
- `python -m tests.support.ci_walkthrough`：诊断、差异复验、回归拒绝、采纳和回滚，详见[演示说明](DEMO.md)。

[Windows CI](../.github/workflows/windows-ci.yml) 执行分组和完整回归并保留结果，不配置模型密钥。[容器工作流](../.github/workflows/container-acceptance.yml) 单独构建和运行 Linux 文件型镜像。推送后的状态以对应提交的 Actions 为准，本地模拟引擎检查与真实容器检查分别记录。
