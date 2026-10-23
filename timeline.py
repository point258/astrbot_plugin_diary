# -*- coding: utf-8 -*-
"""时间轴：把零散的对话按"日期 + 时段"聚成可读的段落。

规则：
- 00:00-04:59 记为「半夜」，归属前一天（叙事连贯）。
- 05-09 早上 / 09-12 上午 / 12-14 中午 / 14-18 下午 / 18-24 晚上。
- 同一 (日期, 时段) 只写一次标题，连续出现才合并。
"""
import datetime as dt

SEGMENTS = [
    (5, "早上"),
    (9, "上午"),
    (12, "中午"),
    (14, "下午"),
    (18, "晚上"),
    (24, "晚上"),
]


def segment_of(ts: int) -> str:
    h = dt.datetime.fromtimestamp(ts).hour
    if h < 5:
        return "半夜"
    for start, name in SEGMENTS:
        if h < start:
            return name
    return "晚上"


def day_of(ts: int) -> str:
    d = dt.datetime.fromtimestamp(ts)
    if d.hour < 5:          # 半夜算前一天
        d = d - dt.timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def day_label(day: str) -> str:
    try:
        return dt.datetime.strptime(day, "%Y-%m-%d").strftime("%Y.%m.%d")
    except Exception:
        return day


def render(turns, with_names=True):
    """turns: 按 ts 升序的 dict 列表，字段 trigger_text / reply_text / user_name / ts。"""
    lines = []
    last_key = None
    for t in turns:
        d = day_of(int(t["ts"]))
        seg = segment_of(int(t["ts"]))
        key = (d, seg)
        if key != last_key:
            if lines:
                lines.append("")
            lines.append("%s%s" % (day_label(d), seg))
            last_key = key
        name = t.get("user_name") or "某人"
        trig = (t.get("trigger_text") or "").strip()
        reply = (t.get("reply_text") or "").strip()
        if trig:
            lines.append(("  %s：%s" % (name, trig)) if with_names else ("  %s" % trig))
        if reply:
            lines.append("  她：%s" % reply)
    return "\n".join(lines).strip()


def participants(turns):
    """{user_id: 显示名}，只算跟她说上话的人。"""
    out = {}
    for t in turns:
        uid = t.get("user_id")
        if not uid or str(uid) in ("None", "0", "null"):
            continue
        if (t.get("trigger_text") or "").strip():
            out[str(uid)] = t.get("user_name") or str(uid)[:6]
    return out


def stats(turns):
    users = participants(turns)
    return {
        "segments": len(turns),
        "users": len(users),
        "at_count": sum(1 for t in turns if t.get("is_at")),
        "chars": sum(len(t.get("reply_text") or "") for t in turns),
    }
