"""在固定练习文件中演示唯一匹配编辑和拒绝修改，不请求模型。"""

from personal_agent.tools import edit_file, read_file, write_file


def main() -> None:
    target = "playground/编辑演示/状态.txt"
    initial = "标题：文本编辑演示\n状态：待修改\n说明：这一行保持不变。\n重复项：示例 / 示例\n"
    print("准备练习文件（会重置此演示文件）：")
    print(write_file(target, initial))
    print("\n唯一匹配替换及 diff：")
    print(edit_file(target, "状态：待修改", "状态：已修改"))
    print("\n尝试替换不存在的文本：")
    print(edit_file(target, "不存在的内容", "新内容"))
    print("\n尝试替换多处匹配的文本：")
    print(edit_file(target, "示例", "替换"))
    print("\n读取最终文件：")
    print(read_file(target))


if __name__ == "__main__":
    main()
