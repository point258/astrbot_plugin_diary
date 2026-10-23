# -*- coding: utf-8 -*-
"""把"她对这些人的既有印象"拼成一段上下文。

两个来源，都只读：
- self_learning 的 user_affections：好感度
- livingmemory 的 memory_atoms：按人名检索旧事
"""
import json
import os
import sqlite3

LEVEL_WORDS = [
    (80, "非常喜欢"), (60, "比较喜欢"), (40, "一般"),
    (20, "有点好感"), (0, "刚认识"), (-100, "反感"),
]


def level_word(score: int) -> str:
    for floor, word in LEVEL_WORDS:
        if score >= floor:
            return word
    return "刚认识"


def affection_levels(db_path: str, group_id: str, user_ids):
    """{user_id: 好感度}，查不到的不出现。"""
    ids = [str(u) for u in user_ids if u]
    if not ids or not os.path.exists(db_path):
        return {}
    q = "SELECT user_id, affection_level FROM user_affections WHERE group_id=? AND user_id IN (%s)" % (
        ",".join("?" * len(ids)))
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=3)
        rows = con.execute(q, [str(group_id)] + ids).fetchall()
        con.close()
        return {str(r[0]): int(r[1] or 0) for r in rows}
    except Exception:
        return {}


def memories_for(db_path: str, names, limit: int = 2):
    """{名字: [旧事要点]}

    只靠 entities 命中太松（同一条群聊记忆会把在场所有人都算上），
    所以优先取正文真的提到这个名字的，其次才用实体命中；跨人重复的只留一次。
    """
    if not names or not os.path.exists(db_path):
        return {}
    out = {}
    used = set()
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=3)
        for name in names:
            if not name:
                continue
            try:
                rows = con.execute(
                    "SELECT content, entities FROM memory_atoms"
                    " WHERE status='active' AND (content LIKE ? OR entities LIKE ?)"
                    " ORDER BY importance DESC, id DESC LIMIT 12",
                    ("%" + str(name) + "%", "%" + str(name) + "%")).fetchall()
            except Exception:
                rows = []
            hit_in_text, hit_only_entity = [], []
            for content, _ents in rows:
                t = (content or "").strip().replace("\n", " ")
                if not t or t in used:
                    continue
                (hit_in_text if str(name) in t else hit_only_entity).append(t[:60])
            picked = (hit_in_text + hit_only_entity)[:limit]
            if picked:
                out[str(name)] = picked
                used.update(picked)
        con.close()
    except Exception:
        pass
    return out


def render(participants: dict, levels: dict, memos: dict) -> str:
    """participants: {user_id: name}；输出给模型看的印象清单（不带编号，避免和片段编号冲突）。"""
    lines = []
    for uid, name in (participants or {}).items():
        uid = str(uid)
        score = levels.get(uid)
        head = "- %s" % name
        if score is not None:
            head += "（好感 %d，%s）" % (score, level_word(score))
        lines.append(head)
        for m in (memos.get(name) or [])[:2]:
            lines.append("    · " + m)
    return "\n".join(lines)
