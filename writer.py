# -*- coding: utf-8 -*-
"""写日记：两次模型调用，一次判断心情，一次写正文。"""
import json
import re

MOOD_SYSTEM = """你只做一件事：读完某人一天在群里的片段，判断她这一天整体的感受。
不要写日记，不要复述事件，不要评价文笔。只输出 JSON。"""

MOOD_PROMPT = """下面是她这一天在群里的片段（只包含她说过的话，和她回应的那些话）：

{timeline}

统计：共 {segments} 段对话，涉及 {users} 个人，其中被 @ {at_count} 次。

判断她这一天整体的感受。只输出 JSON，不要代码块：
{{"mood":"happy|calm|tired|annoyed|excited|down","intensity":0.0,"reason":"不超过20字","tone":"一句话，定日记基调"}}
信息不足就给 calm，强度 0.3。"""

DIARY_PROMPT = """下面是这一天的片段（只包含她说过的话，和她回应的那些话）：

{timeline}

【整体感受】{mood}（强度 {intensity}）：{tone}
【落笔时的心情】{current_mood}

【你对他们已有的印象】
{impressions}

用第一人称写这一天的日记，{words} 字左右。

写法要求，每一条都要守：
1. 不要逐条复述当天发生了什么。只挑 3 到 5 件印象最深的写，其余直接略过。
2. 每写一件事，后面必须跟一句你自己的想法：你在意什么、觉得谁可笑、自己哪里装过头了。
   只有事情没有想法的段落，删掉。
3. 允许跳。可以从下午突然想到早上，也可以写着写着扯到别的事上。
4. 不要用"然后""后来""接着""结果"这类词串场，直接说下一件事。
5. 不要按人排队写。同一个人可以在不同段落出现，也可以一次都不提。
6. 结尾停在具体的事上。不总结这一天，不写"今天真是……"这种收尾。
7. 不写真实姓名、住址、电话、病情。提到人就说"那个谁""小X"。
8. 至少留一段完全不写别人，只写你自己：吃了什么、身上什么感觉、脑子里冒出来的怪念头。
9. 对每个人的态度要和上面那份印象对得上：关系近的写得松、会维护；
   关系远的更冷；旧事里欠你饭、惹过你的，可以顺口翻旧账。
   但不要直接报好感度数字，也不要写"印象里"这种说法。
10. 字数不够就写短，别凑。

另外，为下面这些人各写 2 到 4 句：写你对这个人的感觉，不是复述他说了什么。
可以带一件具体的事，可以带情绪，写不出来就跳过。编号必须照抄，不要自己编：
{people}

只输出 JSON，不要代码块：
{{"title":"不超过12字","body":"正文","fragments":[{{"i":1,"text":"..."}}],"mood_tag":"..."}}"""


def build_mood(timeline_text, st):
    return MOOD_PROMPT.format(
        timeline=timeline_text,
        segments=st.get("segments", 0),
        users=st.get("users", 0),
        at_count=st.get("at_count", 0),
    )


def build_diary(timeline_text, mood, current_mood, words, people, impressions=""):
    people_text = "\n".join(
        "%d. %s" % (i, name) for i, (uid, name) in enumerate((people or {}).items(), 1)
    ) or "（今天没有需要单独写的人）"
    return DIARY_PROMPT.format(
        timeline=timeline_text,
        mood=mood.get("mood", "calm"),
        intensity=round(float(mood.get("intensity", 0.3)), 2),
        tone=mood.get("tone", ""),
        current_mood=current_mood or "平静",
        impressions=(impressions or "（对他们没什么特别的印象）"),
        words=words,
        people=people_text,
    )


def normalize_fragments(data, people):
    """把模型给的 fragments 对齐到真实 user_id。

    模型对 user_id 不可靠，所以提示词里改用编号 i。
    这里依次尝试：i 编号 -> user_id -> 名字 -> 数量对得上时按顺序兜底。
    """
    if not isinstance(data, dict):
        return {}
    ordered = list((people or {}).items())          # [(uid, name), ...]
    if not ordered:
        return {}
    by_name = {str(v): k for k, v in ordered}
    by_id = {str(k): k for k, _ in ordered}

    raw = data.get("fragments") or []
    out, leftovers = {}, []
    for f in raw:
        if not isinstance(f, dict):
            continue
        text = (f.get("text") or "").strip()
        if not text:
            continue
        uid = None
        idx = f.get("i", f.get("index"))
        if isinstance(idx, int) and 1 <= idx <= len(ordered):
            uid = ordered[idx - 1][0]
        elif isinstance(idx, str) and idx.isdigit() and 1 <= int(idx) <= len(ordered):
            uid = ordered[int(idx) - 1][0]
        else:
            raw_uid = f.get("user_id")
            if raw_uid is not None and str(raw_uid) in by_id:
                uid = by_id[str(raw_uid)]
            elif isinstance(raw_uid, str) and raw_uid in by_name:
                uid = by_name[raw_uid]
        if uid:
            out[uid] = text
        else:
            leftovers.append(text)

    if leftovers:
        missing = [k for k, _ in ordered if k not in out]
        if len(missing) == len(leftovers):
            for u, t in zip(missing, leftovers):
                out[u] = t
    return out


def parse_json(text: str):
    if not text:
        return None
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except Exception:
        pass
    m = re.search(r"\{.*\}", s, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


async def call(provider, system: str, prompt: str, timeout: int = 180):
    """统一的一次调用，失败返回空串，不抛。"""
    if provider is None:
        return ""
    try:
        resp = await provider.text_chat(
            prompt=prompt, system_prompt=system, contexts=[])
        return (getattr(resp, "completion_text", "") or "").strip()
    except Exception as e:
        raise RuntimeError("模型调用失败: %r" % (e,))
