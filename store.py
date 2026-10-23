# -*- coding: utf-8 -*-
"""日记插件的存储层。只碰自己的库，不读不写别的插件。"""
import hashlib
import os
import secrets
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    user_id TEXT,
    user_name TEXT,
    trigger_text TEXT,
    reply_text TEXT,
    ts INTEGER NOT NULL,
    is_at INTEGER DEFAULT 0,
    day TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_day ON turns(group_id, day);

CREATE TABLE IF NOT EXISTS diaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    day TEXT NOT NULL,
    title TEXT,
    body TEXT,
    mood TEXT,
    intensity REAL,
    model TEXT,
    created_at INTEGER,
    UNIQUE(group_id, day)
);

CREATE TABLE IF NOT EXISTS fragments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    diary_id INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    text TEXT
);
CREATE INDEX IF NOT EXISTS idx_frag_diary ON fragments(diary_id);

CREATE TABLE IF NOT EXISTS view_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT UNIQUE NOT NULL,
    fragment_id INTEGER NOT NULL,
    user_id TEXT,
    group_id TEXT,
    created_at INTEGER,
    expires_at INTEGER,
    used_at INTEGER
);
"""


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _day(ts: int) -> str:
    import datetime as _dt
    d = _dt.datetime.fromtimestamp(ts)
    if d.hour < 5:
        d -= _dt.timedelta(days=1)
    return d.strftime("%Y-%m-%d")


class DiaryStore:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path, timeout=5, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
        self.conn.commit()

    # ---------- 记录 ----------

    def add_turns(self, rows):
        """rows: (group_id, user_id, user_name, trigger_text, reply_text, ts, is_at, day)"""
        if not rows:
            return 0
        self.conn.executemany(
            "INSERT INTO turns (group_id,user_id,user_name,trigger_text,reply_text,ts,is_at,day)"
            " VALUES (?,?,?,?,?,?,?,?)", rows)
        self.conn.commit()
        return len(rows)

    def turns_for_day(self, group_id: str, day: str):
        cur = self.conn.execute(
            "SELECT * FROM turns WHERE group_id=? AND day=? ORDER BY ts ASC", (group_id, day))
        return [dict(r) for r in cur.fetchall()]

    def days_with_data(self, group_id: str, limit=7):
        cur = self.conn.execute(
            "SELECT day, COUNT(*) c FROM turns WHERE group_id=? GROUP BY day ORDER BY day DESC LIMIT ?",
            (group_id, limit))
        return [(r["day"], r["c"]) for r in cur.fetchall()]

    # ---------- 日记 ----------

    def save_diary(self, group_id, day, title, body, mood, intensity, model, fragments):
        # 同一天重写：先清掉旧日记的片段，避免 INSERT OR REPLACE 后留下孤儿
        old = self.conn.execute(
            "SELECT id FROM diaries WHERE group_id=? AND day=?", (group_id, day)).fetchone()
        if old:
            self.conn.execute("DELETE FROM fragments WHERE diary_id=?", (old["id"],))
            self.conn.execute("DELETE FROM diaries WHERE id=?", (old["id"],))
        cur = self.conn.execute(
            "INSERT OR REPLACE INTO diaries (group_id,day,title,body,mood,intensity,model,created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (group_id, day, title, body, mood, intensity, model, int(time.time())))
        diary_id = cur.lastrowid
        if diary_id == 0:
            row = self.conn.execute(
                "SELECT id FROM diaries WHERE group_id=? AND day=?", (group_id, day)).fetchone()
            diary_id = row["id"] if row else 0
        self.conn.execute("DELETE FROM fragments WHERE diary_id=?", (diary_id,))
        for uid, text in (fragments or {}).items():
            self.conn.execute(
                "INSERT INTO fragments (diary_id,user_id,text) VALUES (?,?,?)",
                (diary_id, str(uid), text))
        self.conn.commit()
        return diary_id

    def latest_diary(self, group_id: str):
        row = self.conn.execute(
            "SELECT * FROM diaries WHERE group_id=? ORDER BY day DESC LIMIT 1", (group_id,)).fetchone()
        return dict(row) if row else None

    def diary_by_day(self, group_id: str, day: str):
        row = self.conn.execute(
            "SELECT * FROM diaries WHERE group_id=? AND day=?", (group_id, day)).fetchone()
        return dict(row) if row else None

    # ---------- 密钥 ----------

    def issue_key(self, fragment_id: int, user_id: str, group_id: str, ttl_days: int = 7) -> str:
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO view_keys (token_hash,fragment_id,user_id,group_id,created_at,expires_at)"
            " VALUES (?,?,?,?,?,?)",
            (sha256(token), fragment_id, str(user_id), str(group_id), now, now + ttl_days * 86400))
        self.conn.commit()
        return token

    def resolve_key(self, token: str):
        """返回 (fragment_text, day, user_id) 或 None。校验过期。"""
        row = self.conn.execute(
            "SELECT k.*, f.text, f.diary_id, d.day, d.title FROM view_keys k"
            " JOIN fragments f ON f.id = k.fragment_id"
            " JOIN diaries d ON d.id = f.diary_id"
            " WHERE k.token_hash=?", (sha256(token),)).fetchone()
        if not row:
            return None
        if row["expires_at"] and row["expires_at"] < time.time():
            return None
        try:
            self.conn.execute("UPDATE view_keys SET used_at=? WHERE id=?", (int(time.time()), row["id"]))
            self.conn.commit()
        except Exception:
            pass
        return {"text": row["text"], "day": row["day"], "title": row["title"], "user_id": row["user_id"]}

    def fragments_of(self, diary_id: int):
        cur = self.conn.execute(
            "SELECT id,user_id,text FROM fragments WHERE diary_id=?", (diary_id,))
        return [dict(r) for r in cur.fetchall()]

    # ---------- 面板用：素材增删改 ----------

    def groups(self):
        return [r["group_id"] for r in self.conn.execute(
            "SELECT DISTINCT group_id FROM turns ORDER BY group_id")]

    def list_turns(self, group_id, day=None, limit=200):
        if day:
            cur = self.conn.execute(
                "SELECT * FROM turns WHERE group_id=? AND day=? ORDER BY ts DESC LIMIT ?",
                (group_id, day, limit))
        else:
            cur = self.conn.execute(
                "SELECT * FROM turns WHERE group_id=? ORDER BY ts DESC LIMIT ?",
                (group_id, limit))
        return [dict(r) for r in cur.fetchall()]

    def update_turn(self, tid, trigger_text=None, reply_text=None, user_name=None):
        sets, args = [], []
        for col, val in (("trigger_text", trigger_text), ("reply_text", reply_text),
                         ("user_name", user_name)):
            if val is not None:
                sets.append("%s=?" % col)
                args.append(val)
        if not sets:
            return 0
        args.append(int(tid))
        n = self.conn.execute("UPDATE turns SET %s WHERE id=?" % ",".join(sets), args).rowcount
        self.conn.commit()
        return n

    def delete_turn(self, tid):
        n = self.conn.execute("DELETE FROM turns WHERE id=?", (int(tid),)).rowcount
        self.conn.commit()
        return n

    def add_turn(self, group_id, user_id, user_name, trigger_text, reply_text):
        import time as _t
        ts = int(_t.time())
        self.conn.execute(
            "INSERT INTO turns (group_id,user_id,user_name,trigger_text,reply_text,ts,is_at,day)"
            " VALUES (?,?,?,?,?,?,0,?)",
            (group_id, str(user_id), user_name, trigger_text, reply_text, ts, _day(ts)))
        self.conn.commit()

    # ---------- 面板用：日记 ----------

    def list_diaries(self, group_id=None, limit=50):
        if group_id:
            cur = self.conn.execute(
                "SELECT id,group_id,day,title,mood,length(body) n,created_at"
                " FROM diaries WHERE group_id=? ORDER BY day DESC LIMIT ?", (group_id, limit))
        else:
            cur = self.conn.execute(
                "SELECT id,group_id,day,title,mood,length(body) n,created_at"
                " FROM diaries ORDER BY day DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]

    def get_diary(self, did):
        row = self.conn.execute("SELECT * FROM diaries WHERE id=?", (int(did),)).fetchone()
        return dict(row) if row else None

    def delete_diary(self, did):
        did = int(did)
        self.conn.execute("DELETE FROM fragments WHERE diary_id=?", (did,))
        self.conn.execute("DELETE FROM view_keys WHERE fragment_id NOT IN (SELECT id FROM fragments)")
        n = self.conn.execute("DELETE FROM diaries WHERE id=?", (did,)).rowcount
        self.conn.commit()
        return n

    def list_keys(self, limit=100):
        cur = self.conn.execute(
            "SELECT k.id,k.user_id,k.group_id,k.created_at,k.expires_at,k.used_at,"
            " k.token_hash,d.day,f.text FROM view_keys k"
            " LEFT JOIN fragments f ON f.id=k.fragment_id"
            " LEFT JOIN diaries d ON d.id=f.diary_id"
            " ORDER BY k.id DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]

    def revoke_key(self, kid):
        n = self.conn.execute("DELETE FROM view_keys WHERE id=?", (int(kid),)).rowcount
        self.conn.commit()
        return n

    def find_fragment(self, group_id, day, user_id):
        row = self.conn.execute(
            "SELECT f.*, d.day FROM fragments f JOIN diaries d ON d.id=f.diary_id"
            " WHERE d.group_id=? AND d.day=? AND f.user_id=?", (group_id, day, str(user_id))).fetchone()
        return dict(row) if row else None

    # ---------- 维护 ----------

    def purge_old(self, keep_days: int = 30):
        cutoff = time.time() - keep_days * 86400
        self.conn.execute("DELETE FROM turns WHERE ts < ?", (int(cutoff),))
        self.conn.execute("DELETE FROM view_keys WHERE expires_at < ?", (int(time.time()),))
        self.conn.commit()
