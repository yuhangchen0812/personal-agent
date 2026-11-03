"""启动参数定义与校验；解析阶段不启动运行时或请求模型。"""

import argparse
from pathlib import Path
import sys

from .permissions import PERMISSION_MODES
from .usage import parse_token_price


def build_parser() -> argparse.ArgumentParser:
    """创建交互模式和单次任务共用的参数解析器。"""
    parser = argparse.ArgumentParser(description="Personal Agent：具有工具权限与终端确认的命令行助手。")
    parser.add_argument("--protocol", choices=("openai", "anthropic"), help="本次模型协议；不按模型名猜测协议")
    parser.add_argument("--anthropic-provider", choices=("native", "qwen"), help="Messages 服务配置；qwen 显式使用千问官方端点及 DASHSCOPE_API_KEY")
    parser.add_argument("--model", metavar="NAME", help="本次模型名，覆盖对应协议的环境配置")
    parser.add_argument("--api-base", metavar="URL", help="本次 HTTP(S) API 根地址，覆盖环境配置；不接受内嵌凭据或查询串")
    parser.add_argument("--thinking", nargs="?", const="adaptive", choices=("disabled", "adaptive", "enabled"), help="思考模式；Qwen 配置支持显式 enabled/disabled，Anthropic 裸参数表示 adaptive")
    parser.add_argument("--thinking-budget", type=int, metavar="N", help="enabled 思考预算；项目要求小于输出上限，Qwen Chat 1～32768，Messages 至少 1024；千问网关正文和思考分别计限")
    parser.add_argument("--max-output-tokens", type=int, metavar="N", help="本次模型输出上限，适用于两种协议")
    task_input = parser.add_mutually_exclusive_group()
    task_input.add_argument("--prompt", metavar="TEXT", help="执行一个普通任务后退出；不读取终端审批，不解释 REPL 命令")
    task_input.add_argument("--prompt-file", metavar="PATH", help="从 UTF-8 文件读取单次任务；- 表示读取 stdin 至 EOF")
    parser.add_argument("--output-format", choices=("text", "json"), default=None, help="单次任务最终输出格式（默认 text）；进度写入 stderr")
    parser.add_argument("--max-turns", type=int, default=5, metavar="N", help="父循环及每个子任务各自的逻辑模型调用上限，不含 SDK 内部重试，必须为正整数（默认 5）")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--permission-mode", choices=PERMISSION_MODES, default="default", help="权限模式（默认 default）")
    for flag, mode in (("--plan", "plan"), ("--accept-edits", "acceptEdits"), ("--dont-ask", "dontAsk"), ("--yolo", "bypassPermissions")):
        modes.add_argument(flag, dest="permission_mode", action="store_const", const=mode, help=f"--permission-mode {mode} 的快捷入口")
    parser.add_argument("--allow-tool", action="append", help="显式允许的精确工具名（含已配置 MCP 工具），可重复指定")
    parser.add_argument("--deny-tool", action="append", help="显式禁止的精确工具名（含已配置 MCP 工具），可重复指定")
    parser.add_argument("--mcp-config", metavar="PATH", help="显式加载本地 stdio MCP 配置；启动配置中的程序，默认不连接")
    parser.add_argument("--mcp-eager", action="store_true", help="配合 --mcp-config 全量提供外部工具定义；默认先 tool_search 检索激活")
    session_options = parser.add_mutually_exclusive_group()
    session_options.add_argument("--session", metavar="NAME", help="创建具名普通文本会话，并在每轮成功后保存")
    session_options.add_argument("--resume", metavar="NAME", help="恢复本项目指定会话，自动识别文本/工具类型并继续保存")
    parser.add_argument("--with-tools", action="store_true", help="配合 --session 新建含工具历史的会话；--resume 自动识别")
    parser.add_argument("--stream", action="store_true", help="流式显示正文；工具会话中先完整拼接请求再审批执行")
    parser.add_argument("--input-price", type=parse_token_price, metavar="YUAN", help="输入单价，元/百万 token；须与 --output-price 同时设置")
    parser.add_argument("--output-price", type=parse_token_price, metavar="YUAN", help="输出单价，元/百万 token；须与 --input-price 同时设置")
    parser.add_argument("--max-cost", type=parse_token_price, metavar="YUAN", help="本次运行估算费用上限（元），须配置两种单价；0 禁止模型请求，在途请求可能超额")
    parser.add_argument("--auto-compact-chars", type=int, metavar="N", help="历史 JSON 达到 N 字符时自动折叠旧轮次，须为正整数；默认关闭")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """校验启动输入，按需读取 UTF-8 提示文件，返回已准备好的参数。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_turns < 1:
        parser.error("--max-turns 必须是正整数。")
    if args.max_output_tokens is not None and args.max_output_tokens < 1:
        parser.error("--max-output-tokens 必须是正整数。")
    if args.thinking_budget is not None and args.thinking_budget < 1:
        parser.error("--thinking-budget 必须为正整数。")
    if (args.input_price is None) != (args.output_price is None):
        parser.error("--input-price 和 --output-price 必须同时设置。")
    single = args.prompt is not None or args.prompt_file is not None
    if args.output_format is not None and not single:
        parser.error("--output-format 需要 --prompt 或 --prompt-file。")
    prompt = args.prompt
    if args.prompt_file is not None:
        try:
            prompt = sys.stdin.read() if args.prompt_file == "-" else Path(args.prompt_file).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as error:
            parser.error(f"无法读取 UTF-8 提示文件（{type(error).__name__}）。")
    if single and not prompt.strip():
        parser.error("单次任务提示不能为空。")
    args.prompt = prompt
    return args
