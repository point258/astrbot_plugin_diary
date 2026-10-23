# -*- coding: utf-8 -*-
"""日记面板：公开的密钥查看页 + 密码保护的管理后台。"""
import html
import secrets
import time

from aiohttp import web

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#faf8f5;color:#2b2b2b;
font-family:-apple-system,"PingFang SC","Microsoft YaHei",system-ui,sans-serif}
a{color:#8a6d3b;text-decoration:none}
a:hover{text-decoration:underline}
.wrap{max-width:960px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:19px;font-weight:600;letter-spacing:.03em;margin:0 0 4px}
.sub{color:#8a8179;font-size:13px;margin:0 0 22px;line-height:1.7}
nav{display:flex;gap:18px;font-size:14px;margin:0 0 26px;padding-bottom:12px;
border-bottom:1px solid #ece5da;flex-wrap:wrap}
nav .sp{flex:1}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:9px 8px;border-bottom:1px solid #efe8de;text-align:left;vertical-align:top}
th{color:#8a8179;font-weight:500;font-size:12px}
input,textarea{width:100%;padding:7px 9px;font-size:13px;border:1px solid #ddd4c8;
border-radius:6px;background:#fff;color:inherit;font-family:inherit}
textarea{resize:vertical;min-height:34px}
button{padding:7px 13px;font-size:13px;border:0;border-radius:6px;background:#2b2b2b;
color:#fff;cursor:pointer}
button.ghost{background:#efe8de;color:#5a5148}
button.danger{background:#b0413e}
.card{background:#fff;border:1px solid #eee7dd;border-radius:12px;padding:22px 24px;
box-shadow:0 6px 24px rgba(0,0,0,.04);margin-bottom:18px}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.muted{color:#9a9189;font-size:12px}
.err{color:#b0413e;font-size:13px;margin-top:10px}
.entry{font-size:16px;line-height:2;white-space:pre-wrap;word-break:break-word}
.frag{background:#fdfbf8;border-left:3px solid #e8dcc9;padding:10px 14px;
margin:8px 0;font-size:13px;line-height:1.8}
.center{min-height:80vh;display:flex;align-items:center;justify-content:center}
.login{width:min(380px,88vw)}
@media (prefers-color-scheme:dark){
body{background:#17161a;color:#e8e3dc}
.card{background:#1f1e23;border-color:#2c2b31;box-shadow:none}
nav{border-color:#2c2b31}
th,td{border-color:#2c2b31}
input,textarea{background:#16151a;border-color:#3a3842;color:#e8e3dc}
button{background:#e8e3dc;color:#17161a}
button.ghost{background:#2c2b31;color:#cfc7bd}
.frag{background:#232227;border-color:#3a3842}
}
"""


def page(title, body, nav=""):
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s</title><style>%s</style></head><body><div class="wrap">%s%s</div></body></html>""" % (
        html.escape(title), CSS, nav, body)


def esc(v):
    return html.escape("" if v is None else str(v))


class DiaryPanel:
    def __init__(self, store, host="0.0.0.0", port=2566, public_port=2588,
                 password="", password_provider=None, regenerate=None):
        self.store = store
        self.host = host
        self.port = port                # 管理面板
        self.public_port = public_port  # 给群友看的
        self.password = password or ""
        self.password_provider = password_provider
        self.regenerate = regenerate
        self._sessions = {}
        self._hits = {}
        self._runners = []
        self._runner = None

    def _public_routes(self):
        return [
            web.get("/", self.public_index),
            web.post("/view", self.public_view),
            web.get("/v/{token}", self.public_link),
        ]

    def _admin_routes(self):
        return [
            web.get("/", self.admin_root),
            web.get("/admin", self.admin_login_page),
            web.post("/admin/login", self.admin_login),
            web.get("/admin/logout", self.admin_logout),
            web.get("/admin/", self.dashboard),
            web.get("/admin/turns", self.turns),
            web.post("/admin/turns/save", self.turns_save),
            web.post("/admin/turns/delete", self.turns_delete),
            web.post("/admin/turns/add", self.turns_add),
            web.get("/admin/diaries", self.diaries),
            web.get("/admin/diary", self.diary),
            web.post("/admin/diary/delete", self.diary_delete),
            web.post("/admin/diary/regen", self.diary_regen),
            web.get("/admin/keys", self.keys),
            web.post("/admin/keys/issue", self.keys_issue),
            web.post("/admin/keys/revoke", self.keys_revoke),
        ]

    async def _serve(self, routes, port):
        app = web.Application()
        app.add_routes(routes)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, self.host, port).start()
        self._runners.append(runner)

    async def start(self):
        if self._runner is not None:
            return
        if self.public_port and self.public_port != self.port:
            await self._serve(self._public_routes(), self.public_port)
            await self._serve(self._admin_routes(), self.port)
        else:
            await self._serve(self._public_routes() + self._admin_routes(), self.port)
        self._runner = True

    async def stop(self):
        for r in self._runners:
            try:
                await r.cleanup()
            except Exception:
                pass
        self._runners = []
        self._runner = None

    async def admin_root(self, request):
        raise web.HTTPFound("/admin")

    # ---------------- 鉴权 ----------------

    def _expected(self) -> str:
        if callable(self.password_provider):
            try:
                v = self.password_provider()
                if v:
                    return str(v)
            except Exception:
                pass
        return self.password or ""

    def _authed(self, request):
        tok = request.cookies.get("d_sid", "")
        exp = self._sessions.get(tok)
        return bool(exp and exp > time.time())

    def _set_session(self, resp):
        tok = secrets.token_urlsafe(24)
        self._sessions[tok] = time.time() + 12 * 3600
        resp.set_cookie("d_sid", tok, max_age=12 * 3600, httponly=True, samesite="Lax")
        return resp

    def _throttle(self, ip):
        now = time.time()
        arr = [t for t in self._hits.get(ip, []) if now - t < 300]
        arr.append(now)
        self._hits[ip] = arr
        return len(arr) <= 10

    def _nav(self):
        return """<nav><strong>日记本</strong>
<a href="/admin/">概览</a><a href="/admin/turns">素材</a>
<a href="/admin/diaries">日记</a><a href="/admin/keys">密钥</a>
<span class="sp"></span><a href="/admin/logout">退出</a></nav>"""

    def _guard(self, request):
        if not self._authed(request):
            raise web.HTTPFound("/admin")

    # ---------------- 公开 ----------------

    async def public_index(self, request):
        body = """<div class="center"><div class="card login"><h1>日记本</h1>
<p class="sub">输入她给你的那串密钥。一串只能看一篇，过期就没了。</p>
<form method="post" action="/view">
<input name="key" placeholder="粘贴密钥" autocomplete="off" autofocus>
<div style="margin-top:14px"><button type="submit">翻开</button></div></form></div></div>"""
        return web.Response(text=page("日记本", body), content_type="text/html")

    async def public_view(self, request):
        data = await request.post()
        return await self._show(str(data.get("key", "")), request)

    async def public_link(self, request):
        return await self._show(request.match_info.get("token", ""), request)

    async def _show(self, token, request):
        token = (token or "").strip()
        if not self._throttle(request.remote or "?"):
            return web.Response(text=page("日记本", '<div class="card"><p class="err">太频繁了，等一会儿。</p></div>'),
                                content_type="text/html", status=429)
        rec = self.store.resolve_key(token) if len(token) >= 16 else None
        if not rec:
            return web.Response(text=page("日记本", """<div class="card">
<p class="err">这串不对，或者已经过期了。</p>
<p style="margin-top:14px"><a href="/">返回</a></p></div>"""),
                                content_type="text/html", status=404)
        body = """<div class="card"><h1>%s</h1><p class="sub">%s</p>
<div class="entry">%s</div><p class="muted" style="margin-top:22px">看完就关上吧</p></div>""" % (
            esc(rec.get("title") or "一页"), esc(rec.get("day")), esc(rec.get("text")))
        return web.Response(text=page("日记本", body), content_type="text/html")

    # ---------------- 登录 ----------------

    async def admin_login_page(self, request):
        if self._authed(request):
            raise web.HTTPFound("/admin/")
        err = '<p class="err">密码不对</p>' if request.query.get("e") else ""
        body = """<div class="center"><div class="card login"><h1>管理后台</h1>
<p class="sub">密码在插件设置里，或数据目录的 panel_password.txt。</p>
<form method="post" action="/admin/login">
<input type="password" name="password" placeholder="密码" autofocus>
<div style="margin-top:14px"><button>进入</button></div></form>%s</div></div>""" % err
        return web.Response(text=page("登录", body), content_type="text/html")

    async def admin_login(self, request):
        if not self._throttle(request.remote or "?"):
            return web.Response(text="too many attempts", status=429)
        data = await request.post()
        if str(data.get("password", "")) == self._expected():
            return self._set_session(web.HTTPFound("/admin/"))
        raise web.HTTPFound("/admin?e=1")

    async def admin_logout(self, request):
        self._sessions.clear()
        resp = web.HTTPFound("/admin")
        resp.del_cookie("d_sid")
        return resp

    # ---------------- 概览 ----------------

    async def dashboard(self, request):
        self._guard(request)
        groups = self.store.groups()
        g = request.query.get("g") or (groups[0] if groups else "")
        ds = self.store.list_diaries(g, 5) if g else []
        rows = "".join(
            '<tr><td><a href="/admin/diary?id=%d">%s</a></td><td>%s</td><td>%s</td><td>%d 字</td></tr>'
            % (d["id"], esc(d["title"]), esc(d["day"]), esc(d["mood"]), d["n"]) for d in ds)
        body = """<div class="card"><h1>概览</h1>
<p class="sub">群 %s ｜ 素材 %d 条 ｜ 日记 %d 篇</p>
<table><tr><th>标题</th><th>日期</th><th>心情</th><th>字数</th></tr>%s</table>
<p style="margin-top:16px"><a href="/admin/turns?g=%s">→ 管理素材</a></p></div>""" % (
            esc(g), len(self.store.list_turns(g, limit=9999)) if g else 0,
            len(self.store.list_diaries(g, 999)) if g else 0, rows, esc(g))
        return web.Response(text=page("概览", body, self._nav()), content_type="text/html")

    # ---------------- 素材 ----------------

    async def turns(self, request):
        self._guard(request)
        groups = self.store.groups()
        g = request.query.get("g") or (groups[0] if groups else "")
        day = request.query.get("day") or ""
        rows_data = self.store.list_turns(g, day or None, 300) if g else []
        trs = []
        for t in rows_data:
            trs.append("""<tr>
<td class="muted">%s</td><td>%s</td>
<td><form method="post" action="/admin/turns/save" style="margin:0">
<input type="hidden" name="id" value="%d"><input type="hidden" name="g" value="%s">
<textarea name="trigger">%s</textarea></form></td>
<td><form method="post" action="/admin/turns/save" style="margin:0">
<input type="hidden" name="id" value="%d"><input type="hidden" name="g" value="%s">
<textarea name="reply">%s</textarea>
<div style="margin-top:6px" class="row"><button>保存</button></form>
<form method="post" action="/admin/turns/delete" style="margin:0">
<input type="hidden" name="id" value="%d"><input type="hidden" name="g" value="%s">
<button class="danger">删除</button></form></div></td></tr>""" % (
                time.strftime("%m-%d %H:%M", time.localtime(t["ts"])), esc(t["user_name"]),
                t["id"], esc(g), esc(t["trigger_text"]),
                t["id"], esc(g), esc(t["reply_text"]),
                t["id"], esc(g)))
        body = """<div class="card"><h1>日记素材</h1>
<p class="sub">这些就是写日记时喂给她的原始片段。可以直接改、删，或手动补一条。</p>
<form method="get" action="/admin/turns" class="row" style="margin-bottom:14px">
<input name="day" value="%s" placeholder="按日期过滤，如 2026-10-22" style="max-width:220px">
<input type="hidden" name="g" value="%s"><button class="ghost">过滤</button></form>
<table><tr><th>时间</th><th>谁</th><th>触发她的话</th><th>她的回复</th></tr>%s</table>
<p class="muted" style="margin-top:12px">共 %d 条（最多显示 300）</p></div>
<div class="card"><h1>手动补一条</h1>
<form method="post" action="/admin/turns/add">
<input type="hidden" name="g" value="%s">
<div class="row"><input name="user_name" placeholder="名字" style="max-width:140px">
<input name="user_id" placeholder="QQ 号" style="max-width:160px"></div>
<div style="margin-top:8px"><textarea name="trigger" placeholder="对方说了什么"></textarea></div>
<div style="margin-top:8px"><textarea name="reply" placeholder="她回了什么"></textarea></div>
<div style="margin-top:10px"><button>添加</button></div></form></div>""" % (
            esc(day), esc(g), "".join(trs), len(rows_data), esc(g))
        return web.Response(text=page("素材", body, self._nav()), content_type="text/html")

    async def turns_save(self, request):
        self._guard(request)
        d = await request.post()
        self.store.update_turn(int(d["id"]), trigger_text=d.get("trigger"),
                               reply_text=d.get("reply"))
        raise web.HTTPFound("/admin/turns?g=%s" % d.get("g", ""))

    async def turns_delete(self, request):
        self._guard(request)
        d = await request.post()
        self.store.delete_turn(int(d["id"]))
        raise web.HTTPFound("/admin/turns?g=%s" % d.get("g", ""))

    async def turns_add(self, request):
        self._guard(request)
        d = await request.post()
        if (d.get("trigger") or "").strip() or (d.get("reply") or "").strip():
            self.store.add_turn(d.get("g", ""), d.get("user_id", ""), d.get("user_name", "某人"),
                                d.get("trigger", ""), d.get("reply", ""))
        raise web.HTTPFound("/admin/turns?g=%s" % d.get("g", ""))

    # ---------------- 日记 ----------------

    async def diaries(self, request):
        self._guard(request)
        ds = self.store.list_diaries(None, 100)
        trs = "".join(
            '<tr><td><a href="/admin/diary?id=%d">%s</a></td><td>%s</td><td>%s</td>'
            '<td>%d 字</td><td>%s</td></tr>'
            % (d["id"], esc(d["title"]), esc(d["day"]), esc(d["mood"]), d["n"], esc(d["group_id"]))
            for d in ds)
        body = """<div class="card"><h1>日记</h1><p class="sub">共 %d 篇</p>
<table><tr><th>标题</th><th>日期</th><th>心情</th><th>字数</th><th>群</th></tr>%s</table></div>""" % (
            len(ds), trs)
        return web.Response(text=page("日记", body, self._nav()), content_type="text/html")

    async def diary(self, request):
        self._guard(request)
        did = int(request.query.get("id") or 0)
        d = self.store.get_diary(did)
        if not d:
            raise web.HTTPFound("/admin/diaries")
        frags = "".join('<div class="frag">%s</div>' % esc(f["text"])
                        for f in self.store.fragments_of(did))
        body = """<div class="card"><h1>%s</h1>
<p class="sub">%s ｜ %s ｜ %d 字 ｜ %s</p>
<div class="entry">%s</div>
<div style="margin-top:22px" class="row">
<form method="post" action="/admin/diary/regen" style="margin:0">
<input type="hidden" name="g" value="%s"><input type="hidden" name="day" value="%s">
<button class="ghost">按当天素材重写</button></form>
<form method="post" action="/admin/diary/delete" style="margin:0">
<input type="hidden" name="id" value="%d"><button class="danger">删除这篇</button></form>
</div></div>
<div class="card"><h1>每人片段</h1><p class="sub">公开页面只能看到各自的那一段。</p>%s</div>""" % (
            esc(d["title"]), esc(d["day"]), esc(d["mood"]), len(d["body"]), esc(d["model"]),
            esc(d["body"]), esc(d["group_id"]), esc(d["day"]), d["id"],
            frags or '<p class="muted">没有片段</p>')
        return web.Response(text=page("日记", body, self._nav()), content_type="text/html")

    async def diary_delete(self, request):
        self._guard(request)
        d = await request.post()
        self.store.delete_diary(int(d["id"]))
        raise web.HTTPFound("/admin/diaries")

    async def diary_regen(self, request):
        self._guard(request)
        d = await request.post()
        if self.regenerate:
            try:
                await self.regenerate(d.get("g", ""), d.get("day", ""))
            except Exception:
                pass
        raise web.HTTPFound("/admin/diaries")

    # ---------------- 密钥 ----------------

    async def keys(self, request):
        self._guard(request)
        rows = self.store.list_keys()
        trs = []
        for k in rows:
            exp = time.strftime("%m-%d %H:%M", time.localtime(k["expires_at"])) if k["expires_at"] else "-"
            trs.append("""<tr><td>%s</td><td>%s</td><td>%s</td><td class="muted">%s</td>
<td><form method="post" action="/admin/keys/revoke" style="margin:0">
<input type="hidden" name="id" value="%d"><button class="ghost">作废</button></form></td></tr>""" % (
                esc(k["user_id"]), esc(k["day"]), exp, esc((k["text"] or "")[:28]), k["id"]))
        body = """<div class="card"><h1>查看密钥</h1>
<p class="sub">每串只能看一篇里的自己那段，过期自动失效。</p>
<table><tr><th>给谁</th><th>哪篇</th><th>到期</th><th>片段开头</th><th></th></tr>%s</table></div>
<div class="card"><h1>手动签发</h1>
<form method="post" action="/admin/keys/issue" class="row">
<input name="g" placeholder="群号" style="max-width:180px">
<input name="day" placeholder="日期 2026-10-22" style="max-width:170px">
<input name="uid" placeholder="QQ 号" style="max-width:170px">
<button>签发</button></form></div>""" % (
            "".join(trs) or '<tr><td colspan="5" class="muted">还没有密钥</td></tr>')
        return web.Response(text=page("密钥", body, self._nav()), content_type="text/html")

    async def keys_issue(self, request):
        self._guard(request)
        d = await request.post()
        frag = self.store.find_fragment(d.get("g", ""), d.get("day", ""), d.get("uid", ""))
        if frag:
            self.store.issue_key(frag["id"], d.get("uid", ""), d.get("g", ""))
        raise web.HTTPFound("/admin/keys")

    async def keys_revoke(self, request):
        self._guard(request)
        d = await request.post()
        self.store.revoke_key(int(d["id"]))
        raise web.HTTPFound("/admin/keys")
