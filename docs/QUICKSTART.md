# 快速开始

## 准备环境

主要运行环境为 Windows、Python 3.12；内置命令工具使用 PowerShell。先用 `python --version` 确认版本，在项目根目录创建环境：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

以下命令直接使用虚拟环境中的 Python，无需激活环境。离线文件与 Plan 示例无需模型账号：

```powershell
.\demo.cmd doctor
.\demo.cmd
```

`doctor` 检查依赖并只报告凭据存在性。演示实际读写隔离目录中的文件，输出 JSON 报告与 `index.html` 的路径；模型响应和演示审批为固定脚本。

## 配置模型服务

PowerShell 中创建配置，已有文件会保留：

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

CMD 对应命令：

```cmd
if not exist .env copy .env.example .env
```

按 [.env.example](../.env.example) 在本地填写凭据，或使用已有环境变量。CLI 覆盖本次运行配置，环境变量优先于 `.env`。

| 配置 | 选择方式 | 凭据 |
| --- | --- | --- |
| OpenAI-compatible | 默认 `openai`，可用 `--model` 和 `--api-base` 覆盖 | `DASHSCOPE_API_KEY`，自定义兼容服务也使用此变量 |
| 原生 Anthropic Messages | `--protocol anthropic --anthropic-provider native` | `ANTHROPIC_API_KEY`、`ANTHROPIC_MODEL` |
| 千问 Messages 网关 | `--protocol anthropic --anthropic-provider qwen` | `DASHSCOPE_API_KEY`，模型按对应配置选择 |

默认配置见 [config.py](../personal_agent/config.py)。使用服务实际提供的模型名；端点填 API 根地址，不附具体的 chat/completions 路径。协议兼容和原生签名的验证范围见[能力边界](LIMITATIONS.md)。

## 新建并恢复工具会话

```powershell
.\run.cmd --session first-task --with-tools --stream
```

输入“读取 personal_agent/agent.py，解释 run_agent_loop 的参数和返回值”。模型可以请求文件工具；写入和 Shell 按当前权限模式检查。输入 `exit` 退出，之后恢复：

```powershell
.\run.cmd --resume first-task --stream
```

新建会话名需要尚未使用；已有会话使用 `--resume`。恢复读取原有文本/工具类型，不重放历史操作。临时工具对话直接使用 `.\run.cmd`；单独的 `.\run.cmd --stream` 是不提供工具的临时文本对话。

## 执行单次任务

```powershell
.\run.cmd --prompt "读取 README.md 并概括项目结构" --output-format json --dont-ask
```

最终 JSON 写入 stdout，进度写入 stderr。单次任务不读取终端审批，`completed` 表示循环结束；实际工具结果见 `tool_trace`。完整参数以 `.\run.cmd --help` 为准。

## 运行源码中的完整 CI 示例

该入口需要完整源码仓库及开发依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -X utf8 -B -m tests.support.ci_walkthrough
```

案例和结果解释见[CI 流程演示](DEMO.md)。仅使用运行包时，可运行 `python run_demo.py` 查看基础示例；完整回归测试和 CI 复现需使用源码仓库。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 启动器找不到 `.venv` | 在当前项目根目录创建环境并安装依赖 |
| 会话名已存在 | 使用 `--resume 名称` 或选择新的会话名 |
| 流式聊天无法调用工具 | 使用 `--session 名称 --with-tools --stream` 新建工具会话 |
| 缺少 pytest | 源码仓库安装 `requirements-dev.txt` |
| 模型配置或凭据错误 | 核对所选协议、模型、端点和本地变量 |
| 输出目录已存在 | 指定新的输出目录；旧报告会保留 |

返回[项目首页](../README.md)，或继续阅读[使用指南](USAGE.md)。
