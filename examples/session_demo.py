"""只演示 JSON 存取，消息预设；不请求模型或执行工具。"""

import json
from uuid import uuid4

from personal_agent.session import TextSession


def main() -> None:
    name = f"demo_{uuid4().hex[:8]}"
    session = TextSession(name)
    session.create()
    messages = [
        {"role": "user", "content": "记住我的练习口令：松果-8624。"},
        {"role": "assistant", "content": "已记住练习口令。"},
    ]
    session.save(messages)
    print(f"固定消息已保存到：{session.path}")

    restored = TextSession(name).load()
    print("重新创建存档对象，从磁盘读回的消息：")
    print(json.dumps(restored, ensure_ascii=False, indent=2))
    print(f"与原消息一致：{restored == messages}")
    print(f"真实模型续聊命令：python -m personal_agent.main --resume {name}")


if __name__ == "__main__":
    main()
