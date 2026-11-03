# 使用指南

完整参数以 `.\run.cmd --help`、`.\task.cmd workflow --help` 和源码帮助为准。

## 权限与 Plan

| 模式 | 行为 |
| --- | --- |
| default | 允许只读工具，写入和 Shell 需要确认 |
| plan | 只读调查；指定计划文件存在受控写入例外 |
| acceptEdits | 自动允许受控文件编辑，Shell 仍需确认 |
| dontAsk | 对需要确认的操作直接拒绝 |
| bypassPermissions | 跳过询问，显式 deny 仍生效 |

```powershell
.\run.cmd --plan
.\run.cmd --dont-ask --deny-tool run_shell
```

规则使用精确工具名。Plan 限制和显式 deny 不会因 allow 被绕过。路径限制属于应用规则，详见 [SECURITY.md](../SECURITY.md)。

## 会话与流式输出

```powershell
.\run.cmd --session demo --with-tools --stream
.\run.cmd --resume demo --stream
```

会话属于当前项目。恢复校验消息与工具 ID，不重新执行历史调用；新操作依照本次启动权限处理。

## Memory、Skills 与子任务

REPL 内使用 `/memory`、`/skills`、`/agents` 查看本地状态。示例方法位于 [examples/skills](../examples/skills/code-review/SKILL.md)，角色模板位于 [examples/agents](../examples/agents/code-reader.md)。

Skill 声明不授予执行权限。候选演化改变的是方法文件及版本记录，不改变模型权重；评估后仍须显式采纳。

## MCP

```powershell
.\run.cmd --mcp-config examples/mcp-config.json --dont-ask
```

仅在显式配置后启动对应 stdio 程序；默认按需检索并加载工具定义。请先审阅配置及所启动的程序。外部服务的参数约束由服务端负责；当前实现不声称支持所有 MCP 扩展。

## CI 工作流

无需密钥的完整演示：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m examples.ci_walkthrough
```

实际模型调查可以从已审阅的自编套件开始：

```powershell
.\task.cmd workflow create --suite examples/repair-eval/suite.json --output-dir .personal/my-ci-task --review-note "已审阅自编工程，允许在副本执行测试；候选另审"
.\task.cmd workflow diagnose --task .personal/my-ci-task
.\task.cmd workflow show --task .personal/my-ci-task
```

create 冻结输入；diagnose 请求配置的真实模型；show 不重放模型调用。后续 verify 必须提供绑定实际差异指纹的 reviews 文件。publish 生成本地报告，不自动采纳代码。参见[演示与测试](TESTING.md)。

## 用量与预算

`/usage`、`/cost` 查看当前运行已报告用量及估算。CLI 的输入/输出单价单位为元/百万 token，须由使用者填写；缺失用量不按 0 计算。在途请求可能超过预算，估算不是账单。
