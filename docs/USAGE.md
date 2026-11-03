# 使用指南

安装与模型配置见[快速开始](QUICKSTART.md)。完整参数以 `.\run.cmd --help`、`.\task.cmd --help` 和 `.\task.cmd workflow --help` 为准。

## 对话类型与恢复

| 启动方式 | 类型 | 工具 |
| --- | --- | --- |
| `.\run.cmd` | 临时工具对话 | 按本次权限执行 |
| `.\run.cmd --stream` | 临时文本流式对话 | 不提供工具 |
| `.\run.cmd --session first-task --with-tools --stream` | 新建具名工具会话 | 按本次权限执行 |
| `.\run.cmd --session text-chat --stream` | 新建具名文本会话 | 不提供工具 |
| `.\run.cmd --resume first-task --stream` | 恢复已保存的会话类型 | 工具会话按本次权限继续 |

会话属于当前项目。恢复校验消息与工具调用 ID，并按绑定引用加载会话记忆；后续操作仍经过本次权限检查。`/clear` 先保存空历史，成功后再清理运行中的历史和记忆选择，保留本次用量、预算及 Plan 状态。

## 权限与 Plan

| 模式 | 行为 |
| --- | --- |
| default | 允许只读工具，写入和 Shell 需要确认 |
| plan | 只读调查；指定计划文件存在受控写入例外 |
| acceptEdits | 自动允许受控文件编辑，Shell 仍需确认 |
| dontAsk | 拒绝需要确认的操作 |
| bypassPermissions | 跳过询问，显式 deny 仍生效 |

```powershell
.\run.cmd --plan
.\run.cmd --dont-ask --deny-tool run_shell
```

工具规则使用精确名称。Plan 限制和显式 deny 优先于 allow；路径与进程边界见 [SECURITY.md](../SECURITY.md)。模型声明或恢复的历史批准不授予新的权限。

## 记忆与上下文

- `/memory`：查看长期记忆；`/memory use 名称` 手动选用，`/memory auto` 每轮重新匹配，`/memory off` 关闭选择。
- `/session-memory`：查看具名会话绑定的结构化记忆；`save` 保留历史，`compact` 折叠已完成旧轮次，`off` 停用参考，`use` 重新启用。
- `/compact`：压缩已完成的旧轮次，保留最近一轮；也可通过 `--auto-compact-chars N` 配置自动触发。

长期记忆和会话记忆注入请求副本，不改变原始任务授权。会话恢复与消息回退不撤销文件或 Shell 副作用。

## Skills 与子任务

REPL 内使用 `/skills`、`/skills search 查询` 和 `/agents` 查看方法与角色。方法模板位于 [examples/skills](../examples/skills/code-review/SKILL.md)，角色模板位于 [examples/agents](../examples/agents/code-reader.md)。

Skill 是本地方法文档，可手动调用、检索或作为独立子任务执行。候选演化改变方法文件和版本记录，不改变模型权重；评估后仍需显式采纳。Skill 和子 Agent 的工具声明受父级权限约束。

## stdio MCP

```powershell
.\run.cmd --mcp-config examples/mcp-config.json --dont-ask
```

显式配置后启动对应本地 stdio 程序。默认先用 `tool_search` 检索，在后续模型请求取得对应 Schema 后再调用；可通过 `--mcp-eager` 改为全量加载。流式 MCP 会话需配合 `--session 名称 --with-tools --stream`。

使用前应审阅配置和被启动的程序。外部服务负责其完整参数约束，当前实现覆盖明确的协议子集，见[能力边界](LIMITATIONS.md)。

## CI 工作流

无需模型密钥的完整复现见[CI 流程演示](DEMO.md)。在源码仓库中，也可使用配置的真实模型调查已审阅的自编套件：

```powershell
.\task.cmd workflow create --suite tests/fixtures/repair-eval/suite.json --output-dir .personal/my-ci-task --review-note "已审阅自编工程，允许在副本执行测试；候选另审"
.\task.cmd workflow diagnose --task .personal/my-ci-task
.\task.cmd workflow show --task .personal/my-ci-task
```

这些命令需要开发依赖。`create` 冻结输入；`diagnose` 请求真实模型；`show` 读取状态。后续 `verify` 需要绑定实际候选差异指纹的 reviews 文件。`publish` 生成本地报告，采纳与回滚使用显式命令。处理自己的项目时，应提供自己的套件和测试配置。

## 用量与费用预算

`/usage`、`/cost` 查看已报告用量和费用估算。`--input-price`、`--output-price` 的单位为元/百万 token，需要同时配置；`--max-cost` 设置本次共享预算。

缺失用量不按零计算。请求前检查预算，已报告用量在失败或取消后保留；在途请求可能超过预算，估算不是服务账单。
