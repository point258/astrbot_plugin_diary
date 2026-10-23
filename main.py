# -*- coding: utf-8 -*-
"""日记本：只读记录 + 每天一篇第一人称日记。

设计约束：
- 不改 req 的内容，只在自己库上写；只有关键词命中那一轮追加一条临时提示。
- 全插件只有两处模型调用（判断心情、写正文），都在定时任务里，不占回复链路。
"""
import asyncio
import datetime as dt
import os
import secrets
import sqlite3
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.star.star_tools import StarTools

try:
    from astrbot.core.agent.message import TextPart
except Exception:  # 老版本没有这个类型，退化为不注入
    TextPart = None

from . import people as people_mod
from . import timeline, writer
from .panel import DiaryPanel
from .store import DiaryStore

# 命令回执不算她说的话，别写进日记
NOISE_PREFIXES = (
    "好感度系统状态", "写好了：", "密钥：", "今天素材不够", "插件服务未就绪",
    "✅", "❌", "The current conversation context",
)
COMMAND_WORDS = {"查看好感度", "查好感度", "好感度", "diary_now", "diary_key"}

DEFAULT_PERSONA = (
    "你在写自己的日记。第一人称，短句为主，不总结不升华，写具体的事。"
)


@register("astrbot_plugin_diary", "point",
          "偷看孩子日记：只读记录她说的话，按天写一篇第一人称日记", "1.0.1")
class DiaryPlugin(Star):
    def __init__(self, context: Context, config):
        super().__init__(context)
        self.config = config
        self.data_dir = str(StarTools.get_data_dir())
        self.store = DiaryStore(os.path.join(self.data_dir, "diary.db"))
        self._pending = {}      # umo -> 触发信息
        self._buf = []          # 待落库的 turn
        self._tasks = []
        self._panel = None

    # ---------------- 生命周期 ----------------

    async def initialize(self):
        if self.config.get("panel_enabled", True):
            try:
                port = int(self.config.get("panel_port", 2566))
                pub = int(self.config.get("public_port", 2588))
                self._panel = DiaryPanel(
                    self.store,
                    host=self.config.get("panel_host", "0.0.0.0"),
                    port=port,
                    public_port=pub,
                    password=self._panel_password(log=False),
                    password_provider=lambda: self._panel_password(log=False),
                    regenerate=self._regen,
                )
                await self._panel.start()
                logger.info("[日记] 群友页 :%d ｜ 管理页 :%d/admin" % (pub, port))
            except Exception as e:
                logger.error("[日记] 面板启动失败: %r" % (e,))
        self._tasks.append(asyncio.create_task(self._flusher()))
        self._tasks.append(asyncio.create_task(self._scheduler()))
        logger.info("[日记] 已就绪，数据目录 %s" % self.data_dir)

    async def terminate(self):
        for t in self._tasks:
            t.cancel()
        await self._flush()
        if self._panel:
            try:
                await self._panel.stop()
                logger.info("[日记] 面板已关闭，端口释放")
            except Exception as e:
                logger.warning("[日记] 关闭面板失败: %r" % (e,))

    def _panel_password(self, log: bool = True) -> str:
        pw = (self.config.get("panel_password") or "").strip()
        if pw:
            return pw
        path = os.path.join(self.data_dir, "panel_password.txt")
        if os.path.exists(path):
            try:
                pw = open(path, encoding="utf-8").read().strip()
            except Exception:
                pw = ""
        if not pw:
            pw = secrets.token_urlsafe(9)
            try:
                open(path, "w", encoding="utf-8").write(pw)
            except Exception:
                pass
        if log:
            logger.info("[日记] 管理面板密码：%s（可在插件设置里改）" % pw)
        return pw

    async def _regen(self, group_id: str, day: str):
        return await self.generate(group_id, day)

    # ---------------- 记录 ----------------

    @filter.on_llm_request()
    async def on_req(self, event: AstrMessageEvent, req):
        """只读：记下这一轮是谁、说了什么；关键词命中时追加一条临时提示。"""
        try:
            if not self.config.get("enabled", True):
                return
            if event.is_private_chat():
                return
            text = (event.get_message_str() or "").strip()
            if not text:
                return
            gid = str(event.get_group_id() or "")
            if not gid:
                return
            uid = str(event.get_sender_id() or "")
            if not uid or uid in ("None", "0"):
                return
            self._pending[event.unified_msg_origin] = {
                "gid": gid,
                "uid": uid,
                "name": event.get_sender_name() or "某人",
                "text": text[:500],
                "at": 1 if getattr(event, "is_at_or_wake_command", False) else 0,
                "ts": int(time.time()),
            }
            self._maybe_hint(event, req, gid, text)
        except Exception as e:
            logger.debug("[日记] on_req 忽略异常: %r" % (e,))

    def _maybe_hint(self, event, req, gid, text):
        if TextPart is None:
            return
        kws = self.config.get("keywords") or []
        if not any(k and k in text for k in kws):
            return
        score = self._affection(gid, str(event.get_sender_id() or ""))
        threshold = int(self.config.get("key_threshold", 90))
        allow = score > threshold
        hint = ("有人问起你的日记本。这次%s。"
                "不要报任何分数，不要提门槛，不要说\"好感度多少\"，也别解释规则，一句话就过。"
                % ("可以给他看" if allow else "别给"))
        try:
            part = TextPart(text=hint)
            mark = getattr(part, "mark_as_temp", None)
            if callable(mark):
                mark()
            req.extra_user_content_parts.append(part)
        except Exception as e:
            logger.debug("[日记] 注入提示失败: %r" % (e,))

    @filter.after_message_sent()
    async def on_sent(self, event: AstrMessageEvent):
        try:
            p = self._pending.pop(event.unified_msg_origin, None)
            if not p:
                return
            reply = self._reply_text(event)
            if not reply or self._noise(reply):
                return
            self._buf.append((
                p["gid"], p["uid"], p["name"], p["text"], reply, p["ts"], p["at"],
                timeline.day_of(p["ts"]),
            ))
            if len(self._buf) >= 50:
                await self._flush()
        except Exception as e:
            logger.debug("[日记] on_sent 忽略异常: %r" % (e,))

    @staticmethod
    def _noise(text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return True
        if t.startswith("/") or t in COMMAND_WORDS:
            return True
        return any(t.startswith(p) for p in NOISE_PREFIXES)

    @staticmethod
    def _reply_text(event) -> str:
        try:
            res = event.get_result()
            if not res:
                return ""
            chain = getattr(res, "chain", None) or []
            txt = "".join(c.text for c in chain if isinstance(c, Plain))
            if txt:
                return txt.strip()
            fn = getattr(res, "get_plain_text", None)
            if callable(fn):
                return (fn() or "").strip()
        except Exception:
            pass
        return ""

    async def _flush(self):
        if not self._buf:
            return
        rows, self._buf = self._buf, []
        try:
            self.store.add_turns(rows)
        except Exception as e:
            logger.error("[日记] 落库失败: %r" % (e,))

    async def _flusher(self):
        while True:
            await asyncio.sleep(5)
            await self._flush()

    # ---------------- 生成 ----------------

    async def _scheduler(self):
        while True:
            await asyncio.sleep(600)
            try:
                await self._maybe_write()
            except Exception as e:
                logger.error("[日记] 定时任务异常: %r" % (e,))

    async def _maybe_write(self):
        if not self.config.get("enabled", True):
            return
        now = dt.datetime.now()
        if now.hour < int(self.config.get("generate_hour", 23)):
            return
        interval = max(1, int(self.config.get("interval_days", 1)))
        for gid in self._groups():
            day = timeline.day_of(int(time.time()))
            last = self.store.latest_diary(gid)
            if last:
                try:
                    gap = (dt.date.fromisoformat(day) - dt.date.fromisoformat(last["day"])).days
                    if gap < interval:
                        continue
                except Exception:
                    pass
            try:
                await self.generate(gid, day)
            except Exception as e:
                logger.error("[日记] 生成失败 %s: %r" % (gid, e))

    def _groups(self):
        try:
            cur = self.store.conn.execute("SELECT DISTINCT group_id FROM turns")
            return [r["group_id"] for r in cur.fetchall()]
        except Exception:
            return []

    async def generate(self, group_id: str, day: str):
        turns = self.store.turns_for_day(group_id, day)
        if len(turns) < int(self.config.get("min_segments", 3)):
            return None
        body_text = timeline.render(turns)
        st = timeline.stats(turns)
        people = timeline.participants(turns)

        mood_prov = self._provider(self.config.get("mood_model_id"))
        diary_prov = self._provider(self.config.get("diary_model_id")) or mood_prov

        mood = {}
        try:
            raw = await writer.call(mood_prov, writer.MOOD_SYSTEM, writer.build_mood(body_text, st))
            mood = writer.parse_json(raw) or {}
        except Exception as e:
            logger.warning("[日记] 心情判断失败: %r" % (e,))
        if not mood:
            mood = {"mood": "calm", "intensity": 0.3, "tone": ""}

        persona = self._persona()
        impressions = self._impressions(group_id, people)
        prompt = writer.build_diary(
            body_text, mood, self._current_mood(group_id),
            int(self.config.get("words", 400)), people, impressions=impressions)
        raw2 = await writer.call(diary_prov, persona, prompt)
        data = writer.parse_json(raw2)
        if not data or not data.get("body"):
            raise RuntimeError("日记输出解析失败")

        frags = writer.normalize_fragments(data, people)
        diary_id = self.store.save_diary(
            group_id, day, (data.get("title") or "")[:40], data["body"].strip(),
            mood.get("mood"), float(mood.get("intensity", 0.3)),
            str(self.config.get("diary_model_id") or ""), frags)
        logger.info("[日记] %s %s 写好了，%d 字，%d 个片段"
                    % (group_id, day, len(data["body"]), len(frags)))
        return diary_id

    # ---------------- 外部数据（只读） ----------------

    def _provider(self, pid):
        """指定了就按 id 取；留空就用 AstrBot 当前的对话模型。"""
        if pid:
            try:
                p = self.context.get_provider_by_id(str(pid))
                if p is not None:
                    return p
            except Exception as e:
                logger.warning("[日记] 取模型失败 %s: %r" % (pid, e))
        try:
            return self.context.get_using_provider()
        except Exception:
            return None

    def _sl_db(self) -> str:
        return os.path.join(os.path.dirname(self.data_dir),
                            "astrbot_plugin_self_learning", "messages.db")

    def _lm_db(self) -> str:
        return os.path.join(os.path.dirname(self.data_dir),
                            "astrbot_plugin_livingmemory", "livingmemory.db")

    def _impressions(self, group_id: str, people: dict) -> str:
        """好感度 + 旧记忆，拼成"她对这些人的既有印象"。全只读。"""
        try:
            levels = people_mod.affection_levels(self._sl_db(), group_id, people.keys())
            memos = people_mod.memories_for(self._lm_db(), list(people.values()))
            return people_mod.render(people, levels, memos)
        except Exception as e:
            logger.debug("[日记] 印象构建失败: %r" % (e,))
            return ""

    def _affection(self, group_id: str, user_id: str) -> int:
        if not user_id:
            return 0
        db = self._sl_db()
        if not os.path.exists(db):
            return 0
        try:
            con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=2)
            row = con.execute(
                "SELECT affection_level FROM user_affections WHERE group_id=? AND user_id=?",
                (str(group_id), str(user_id))).fetchone()
            con.close()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    def _current_mood(self, group_id: str) -> str:
        db = os.path.join(os.path.dirname(self.data_dir),
                          "astrbot_plugin_self_learning", "messages.db")
        if not os.path.exists(db):
            return ""
        try:
            con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=2)
            row = con.execute(
                "SELECT mood_type, mood_intensity FROM bot_mood WHERE group_id=? AND is_active=1"
                " ORDER BY start_time DESC LIMIT 1", (str(group_id),)).fetchone()
            con.close()
            if row:
                return "%s（强度 %.2f）" % (row[0], float(row[1] or 0))
        except Exception:
            pass
        return ""

    def _persona(self) -> str:
        f = (self.config.get("persona_file") or "").strip()
        if f:
            path = os.path.join(os.path.dirname(__file__), f)
            if os.path.exists(path):
                try:
                    return open(path, encoding="utf-8").read().strip()
                except Exception:
                    pass
        return DEFAULT_PERSONA

    # ---------------- 命令 ----------------

    @filter.command("diary_now")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def diary_now(self, event: AstrMessageEvent):
        """立刻写今天的日记（管理员）"""
        gid = str(event.get_group_id() or "")
        day = timeline.day_of(int(time.time()))
        try:
            did = await self.generate(gid, day)
        except Exception as e:
            yield event.plain_result("写失败了：%s" % e)
            return
        if not did:
            yield event.plain_result("今天素材不够，不写。")
            return
        d = self.store.diary_by_day(gid, day)
        yield event.plain_result("写好了：《%s》%d 字" % (d["title"], len(d["body"])))

    @filter.command("diary_key")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def diary_key(self, event: AstrMessageEvent, uid: str = ""):
        """给某人签发最近一篇的查看密钥（管理员）"""
        gid = str(event.get_group_id() or "")
        d = self.store.latest_diary(gid)
        if not d:
            yield event.plain_result("还没有日记。")
            return
        frag = None
        for f in self.store.fragments_of(d["id"]):
            if str(f["user_id"]) == str(uid):
                frag = f
                break
        if not frag:
            yield event.plain_result("这篇里没有关于 %s 的片段。" % uid)
            return
        token = self.store.issue_key(frag["id"], uid, gid)
        port = self.config.get("panel_port", 2588)
        yield event.plain_result("%s\n密钥：%s" % (d["title"], token))
