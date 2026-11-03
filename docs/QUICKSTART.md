# 快速开始

## 环境

- Python 3.12，使用 `python --version` 确认。
- Windows 为主要运行环境；内置 Shell 工具使用 PowerShell。
- 离线演示不需要模型账号；真实模型任务需要自行配置服务凭据。

在仓库根目录执行以下命令，CMD 与 PowerShell 均不需要激活虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\demo.cmd doctor
.\demo.cmd
```

只运行基础 Agent 可安装 `requirements.txt`；CI 演示和开发测试需要 `requirements-dev.txt` 中的 pytest。`doctor` 只报告凭据存在性，不输出密钥。

## 模型配置

PowerShell：`Copy-Item .env.example .env`。CMD：`copy .env.example .env`。

在本地 `.env` 中填写 `DASHSCOPE_API_KEY`，也可以直接设置终端环境变量。不要将真实值写入示例文件。默认模型及服务地址见 [config.py](../personal_agent/config.py)，也可显式覆盖：

```powershell
.\run.cmd --model YOUR_MODEL --stream
.\run.cmd --model YOUR_MODEL --api-base https://your-provider.example/v1
```

自定义 OpenAI-compatible 服务仍使用项目配置的密钥变量；服务须支持实际使用的消息与工具契约。不要把服务根地址与具体的 chat/completions 路径混用。

Anthropic Messages 入口使用 `--protocol anthropic`，原生服务对应 `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL`。千问 Messages 需显式加 `--anthropic-provider qwen`，使用 `DASHSCOPE_API_KEY`。实际协议支持和验证范围见[能力边界](LIMITATIONS.md)。

## 第一次使用

```powershell
.\run.cmd --stream
```

输入“读取 personal_agent/agent.py，解释 run_agent_loop 的参数和返回值”。模型可提出只读工具请求。写入、Shell 等操作依照本次权限模式处理。

单次任务：

```powershell
.\run.cmd --prompt "读取 README.md 并概括项目入口" --output-format json --dont-ask
```

`--prompt` 模式不读取终端审批输入，也不解释 REPL 的斜杠命令；`completed` 表示循环完成，具体任务结果仍需按实际产物判断。

## 常见启动问题

| 现象 | 检查 |
| --- | --- |
| 启动器提示缺少 .venv | 在当前仓库创建环境并安装依赖 |
| 缺少 pytest | 安装 requirements-dev.txt |
| 缺少模型凭据 | 检查本地 .env 或环境变量；离线演示无需凭据 |
| 模型名或协议错误 | 用 --help 核对配置，选择服务实际支持的模型与协议 |
| 演示输出目录已存在 | 换用新目录；程序保留旧报告 |

返回[项目首页](../README.md)。
