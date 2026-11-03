"""在固定练习文件中演示创建、覆盖和读取，不请求模型。"""

from personal_agent.tools import read_file, write_file


def main() -> None:
    target = "playground/写入演示/问候.txt"
    print("写入第一版：")
    print(write_file(target, "第一版：你好！\n这是写入演示。\n"))
    print("\n整体覆盖为第二版：")
    print(write_file(target, "第二版：你好，Personal Agent！\n这是一份由 write_file 写入的练习文件。\n"))
    print("\n读取实际文件：")
    print(read_file(target))
    print("\n尝试写到 playground 之外：")
    print(write_file("personal_agent/不应创建.txt", "这次写入应该被拒绝。\n"))


if __name__ == "__main__":
    main()
