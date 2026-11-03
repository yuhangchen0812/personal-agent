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

先检查核心循环与路径规则：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -p test_agent.py
.\.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -p test_tools.py
.\.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -p test_agent_config.py
```

其余单元/集成测试使用同样的 unittest discovery 入口。tests 中部分用例复用 codex 验证模块，因此不要删除 codex Python 文件。

## GitHub Actions

[Windows CI](../.github/workflows/windows-ci.yml) 调用既有 27 个默认本地验证组，分别保留 JSON 和日志，不配置模型密钥、不运行 live_smoke。容器配置有单独的[验收工作流](../.github/workflows/container-acceptance.yml)。工作流存在不等于远端已运行通过。

验证器的默认总控和 verify.cmd 还包含开发期历史报告检查；公开仓库不分发本机原始历史记录。公开 CI 直接调用已有 worker 阶段，避免把历史文件存在性当成本次测试通过。默认总控若提示缺历史记录，应使用上述测试入口及公开 CI 的阶段命令，不能伪造历史文件。

## 怎样解释结果

- 固定响应验证的是执行链路，不是实时模型质量。
- completed 表示循环结束；evaluated 表示测量完成；published 表示本地报告生成。
- 自编 holdout 分组不是第三方盲测；已观察的样本不能继续作为未见证据。
- 公开测试通过后仍可能新增回归；没有独立测试时，不能据公开结果宣称完整修复。
- Windows Job 提供进程资源限制，不构成文件系统或网络安全沙箱。

公开版实际核验记录见 [VALIDATION.md](VALIDATION.md)。
