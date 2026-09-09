"""长期记忆：故障案例库（agent_cases 表）

发布失败/告警处置 → 沉淀为案例；再次遇到相似场景时通过 search_cases 召回
（字符二元组 Jaccard 相似度，零向量依赖），作为 Agent/Advisor 决策的参考经验。

检索策略：
    1. service/env/tags 精确/模糊命中直接加分
    2. symptom+root_cause+action 与查询串做字符二元组相似度打分
    3. 按 (得分, 时间) 取 TopN
"""
import re
import time

from webapp import db as dbm

_BIGRAM_RE = re.compile(r"[\u4e00-\u9fff\w]")


def _bigrams(text: str) -> set:
    """中文友好的字符二元组：'数据库连接超时' → {'数据','据库','库连',...}"""
    chars = _BIGRAM_RE.findall((text or "").lower())
    return {chars[i] + chars[i + 1] for i in range(len(chars) - 1)} if len(chars) > 1 else set()


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def create_case(db_path: str, title: str, service: str = "", env: str = "",
                version: str = "", symptom: str = "", root_cause: str = "",
                action: str = "", result: str = "", lesson: str = "",
                tags: list = None, source: str = "manual",
                advice_id: int = 0, user_id: int = 0,
                username: str = "") -> int:
    now = int(time.time())
    return dbm.create_record(db_path, "agent_cases", dict(
        title=title, service=service or "", env=env or "", version=version or "",
        symptom=symptom or "", root_cause=root_cause or "", action=action or "",
        result=result or "", lesson=lesson or "",
        tags=list(tags) if tags else [], source=source or "manual",
        advice_id=advice_id or 0, created_by=user_id or 0,
        created_by_name=username or "", created_at=now, updated_at=now))


def get_case(db_path: str, case_id: int):
    return dbm.get_record(db_path, "agent_cases", case_id)


def list_cases(db_path: str, service: str = "", limit: int = 50) -> list:
    if service:
        rows = dbm.list_records(db_path, "agent_cases", "service=?",
                                (service,), "id DESC")
    else:
        rows = dbm.list_records(db_path, "agent_cases", order="id DESC")
    return rows[:max(1, min(limit, 200))]


def update_case(db_path: str, case_id: int, fields: dict) -> bool:
    fields["updated_at"] = int(time.time())
    return dbm.update_record(db_path, "agent_cases", case_id, fields)


def delete_case(db_path: str, case_id: int) -> bool:
    return dbm.delete_record(db_path, "agent_cases", case_id)


def search_cases(db_path: str, keyword: str = "", service: str = "",
                 limit: int = 5) -> list:
    """相似案例召回：返回按相关度降序的案例列表（含 score 字段）"""
    candidates = list_cases(db_path, service=service, limit=200)
    if not keyword:
        return [dict(c, score=1.0) for c in candidates[:limit]]
    qgrams = _bigrams(keyword)
    scored = []
    for c in candidates:
        score = 0.0
        if qgrams:
            hay = " ".join([
                c.get("title", ""), c.get("symptom", ""),
                c.get("root_cause", ""), c.get("action", ""),
                c.get("lesson", "")])
            score = _jaccard(qgrams, _bigrams(hay)) * 0.7
        # 结构化字段命中补分（服务名 / 标签模糊命中）
        tag_hit = any(keyword in t for t in (c.get("tags") or []))
        if keyword in (c.get("service") or ""):
            tag_hit = True
        if tag_hit:
            score += 0.4
        if c.get("service") and service and c["service"] == service:
            score += 0.3
        if score > 0:
            scored.append((score, c))
    scored.sort(key=lambda x: (-x[0], -(x[1].get("created_at") or 0)))
    return [dict(c, score=round(s, 3)) for s, c in scored[:max(1, min(limit, 20))]]


def summarize_case(c: dict) -> str:
    """给 LLM 看的紧凑案例摘要（单行，避免上下文膨胀）"""
    tags = ",".join(c.get("tags") or []) or "-"
    return ("[{id}] {title}（service={service} env={env} version={version} "
            "tags={tags} result={result}）\n"
            "  现象: {symptom}\n  根因: {root_cause}\n  处置: {action}\n"
            "  教训: {lesson}").format(
        id=c["id"], title=c.get("title", ""), service=c.get("service", "") or "-",
        env=c.get("env", "") or "-", version=c.get("version", "") or "-",
        tags=tags, result=c.get("result", "") or "-",
        symptom=c.get("symptom", "") or "-",
        root_cause=c.get("root_cause", "") or "-",
        action=c.get("action", "") or "-",
        lesson=c.get("lesson", "") or "-")
