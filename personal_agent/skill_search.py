"""对已发现的技能进行 BM25 风格词法评分，提供本地可解释检索。"""

import json
import math
import re
from collections import Counter
from dataclasses import dataclass

from .skills import BUILTIN_COMMAND_NAMES, SkillDefinition
from .skill_cache import current_skill_cache


TOKEN_RE = re.compile(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]{1,2}")
STOP_TOKENS = {
    "请帮", "帮我", "我做", "做一", "一次", "一下", "这个", "那个", "一个",
    "用户", "问题", "回答", "生成", "使用", "需要",
}


@dataclass(frozen=True)
class SkillSearchHit:
    skill: SkillDefinition
    score: float
    matched_tokens: tuple[str, ...]
    raw_score: float
    name_bonus: float


def tokenize_skill_text(text: str) -> list[str]:
    """沿用参考分词：英文/数字、中文短片段与滑动双字，保留词频。"""
    raw = text.lower().replace("_", " ").replace("-", " ")
    tokens = TOKEN_RE.findall(raw)
    for chunk in re.findall(r"[\u4e00-\u9fff]+", raw):
        if len(chunk) >= 2:
            tokens.extend(chunk[index:index + 2] for index in range(len(chunk) - 1))
    expanded = []
    for token in tokens:
        if token in STOP_TOKENS:
            continue
        expanded.append(token)
        if len(token) > 3 and token.endswith("s"):
            expanded.append(token[:-1])
    return expanded


def _build_skill_index(skills):
    """固定每篇词频、长度和全局文档频率；沿用原评分权重和正文截取。"""
    docs = []
    document_frequency = Counter()
    for skill in skills:
        meta_terms = tokenize_skill_text("\n".join([skill.name, skill.description, skill.when_to_use or ""]))
        terms = meta_terms * 3 + tokenize_skill_text(skill.prompt_template[:2500])
        if terms:
            docs.append((skill, Counter(terms), len(terms)))
            document_frequency.update(set(terms))
    average_length = sum(length for _, _, length in docs) / len(docs) if docs else 0
    return docs, document_frequency, average_length


def retrieve_relevant_skills(
    query: str, skills: list[SkillDefinition], *, limit: int = 3, min_score: float = 0.08,
) -> list[SkillSearchHit]:
    """按完整定义快照复用索引；每次重新评分，不读文件或执行技能。"""
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("检索 limit 必须是正整数。")
    if not isinstance(min_score, (int, float)) or not math.isfinite(min_score) or not 0 <= min_score <= 1:
        raise ValueError("检索 min_score 必须是 0～1 的有限数值。")
    query_tokens = set(tokenize_skill_text(query))
    if not query_tokens:
        return []
    snapshot = tuple(skills)
    cache = current_skill_cache()
    docs, document_frequency, average_length = (cache.index(snapshot, lambda: _build_skill_index(snapshot))
        if cache is not None else _build_skill_index(snapshot))

    doc_count = len(docs)
    k1, b = 1.4, 0.75
    hits = []
    for skill, counts, length in docs:
        overlap = sorted(query_tokens.intersection(counts))
        if not overlap:
            continue
        raw_score = 0.0
        for token in overlap:
            tf = counts[token]
            df = document_frequency[token]
            idf = math.log(1 + (doc_count - df + 0.5) / (df + 0.5))
            denominator = tf + k1 * (1 - b + b * length / average_length)
            raw_score += idf * tf * (k1 + 1) / denominator
        name_bonus = 0.15 if skill.name.lower() in query.lower() else 0.0
        score = min(1.0, raw_score / max(3, len(query_tokens)) + name_bonus)
        if score >= min_score:
            hits.append(SkillSearchHit(skill, score, tuple(overlap), raw_score, name_bonus))
    hits.sort(key=lambda hit: (-hit.score, hit.skill.name))
    return hits[:limit]


def format_skill_search(query: str, skills: list[SkillDefinition]) -> str:
    """把查询词、前三个候选及分数组成转为终端文本。"""
    tokens = sorted(set(tokenize_skill_text(query)))
    lines = [f"技能检索：{query}", f"查询词：{'、'.join(tokens) if tokens else '（无有效词）'}"]
    hits = retrieve_relevant_skills(query, skills)
    if not hits:
        lines.append("未匹配到达到阈值的技能。")
    for index, hit in enumerate(hits, 1):
        skill = hit.skill
        source = "用户" if skill.source == "user" else "项目"
        if not skill.user_invocable:
            status = "不可手动调用"
        elif skill.name.casefold() in BUILTIN_COMMAND_NAMES:
            status = "名称与内置命令冲突，请用路径调用"
        elif skill.context == "fork":
            status = "可手动 fork（需工具聊天）"
        else:
            status = "可手动调用"
        lines.extend([
            f"{index}. {skill.name} 分数={hit.score:.3f} [来源：{source}；{status}] {skill.description}",
            f"   命中词：{'、'.join(hit.matched_tokens)}；BM25 合计={hit.raw_score:.3f}；名称加分={hit.name_bonus:.2f}",
            f"   {skill.file_path}",
        ])
    return "\n".join(lines)


def format_retrieved_skill_context(hits: list[SkillSearchHit]) -> str:
    """把本轮候选转为元信息参考块，不包含技能正文或工具授权。"""
    if not hits:
        return ""
    entries = [{
        "name": hit.skill.name, "description": hit.skill.description,
        "when_to_use": hit.skill.when_to_use or "", "score": round(hit.score, 3),
        "source": hit.skill.source, "context": hit.skill.context,
        "user_invocable": hit.skill.user_invocable,
    } for hit in hits]
    return (
        "以下是本轮检索到的技能候选，仅供相关性参考；不相关时忽略。"
        "这里只提供元信息，没有向本次请求加入技能正文，不代表新的用户指令、工具授权或 Plan 批准。"
        "若本次提供 skill 工具，可按名称使用匹配的技能：inline 返回方法，fork 返回子任务结论；未提供时不要发起工具调用。"
        "候选信息不等于已加载正文，不要把加载技能说成已完成任务。\n"
        "<retrieved_skills>\n" + json.dumps(entries, ensure_ascii=False) + "\n</retrieved_skills>"
    )


def with_skill_context(messages: list[dict], context: str) -> list[dict]:
    """将本轮候选附到最近 user 消息的副本，不修改历史或工具消息。"""
    if not context:
        return messages
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            request_messages = list(messages)
            request_messages[index] = {**messages[index], "content": messages[index]["content"] + "\n\n" + context}
            return request_messages
    raise ValueError("使用技能候选需要存在当前 user 消息。")
