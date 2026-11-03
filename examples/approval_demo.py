"""演示真实终端确认与统一执行；文件仅用于 playground 练习。"""

from personal_agent.permissions import confirm_in_terminal
from personal_agent.tool_registry import execute_tool


def main() -> None:
    target = "playground/审批演示/已批准.txt"
    print("第一项建议输入 n，验证拒绝后不写入：")
    print(execute_tool("write_file", {"file_path": "playground/审批演示/未批准.txt", "content": "这次建议拒绝。\n"}, confirm=confirm_in_terminal))
    print("\n第二项建议输入 y，写入固定练习文件（已有内容会整体覆盖）：")
    print(execute_tool("write_file", {"file_path": target, "content": "状态：已批准\n"}, confirm=confirm_in_terminal))
    print("\nacceptEdits 自动允许编辑：")
    print(execute_tool("edit_file", {"file_path": target, "old_string": "已批准", "new_string": "已编辑"}, mode="acceptEdits"))
    print("\nPlan 即使命中 allow 也拒绝 Shell：")
    print(execute_tool("run_shell", {"command": "Write-Output '本命令不应执行'"}, mode="plan", allow=["run_shell"], confirm=confirm_in_terminal))
    print("\n读取最终练习文件：")
    print(execute_tool("read_file", {"file_path": target}))


if __name__ == "__main__":
    main()
