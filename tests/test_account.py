"""她自己的 B 站账号：扫码登录、逛、看评论区、操作、配额与风控暂停、表情包收藏。"""

from __future__ import annotations

from typing import Any, Dict, List

import asyncio
import base64
import copy
import io

from aiohttp import web
from aiohttp.test_utils import TestServer
from PIL import Image

import bili_video_under_test as plugin_module
from bili_video_under_test.core import qwen as qwen_module
from bili_video_under_test.core.account import Account, AccountPaused, Limits, QuotaExceeded
from bili_video_under_test.core.bilibili import BiliClient
from bili_video_under_test.core.browse import Browser, parse_duration

from fake_servers import BVID, FakeWorld
from test_plugin import CONFIG, FakeHost, make_plugin

ACCOUNT_CONFIG = {**copy.deepcopy(CONFIG), "bilibili": {"cookie": "", "quality": "360P"}}


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (800, 600), (200, 30, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


class AccountWorld(FakeWorld):
    def __init__(self) -> None:
        super().__init__()
        self.qr_polls = 0
        self.qr_codes = [86101, 86090, 0]
        self.posts: List[Dict[str, Any]] = []
        self.reply_status = 200
        self.reply_code = 0
        self.logged_in = False

    def app(self) -> web.Application:
        app = super().app()
        app.router.add_get("/x/passport-login/web/qrcode/generate", self.qr_generate)
        app.router.add_get("/x/passport-login/web/qrcode/poll", self.qr_poll)
        app.router.add_get("/x/web-interface/wbi/index/top/feed/rcmd", self.feed)
        app.router.add_get("/x/web-interface/popular", self.popular)
        app.router.add_get("/x/web-interface/wbi/search/type", self.search)
        app.router.add_get("/x/v2/reply/wbi/main", self.replies)
        app.router.add_get("/x/v3/fav/folder/created/list-all", self.folders)
        app.router.add_get("/bfs/new_dyn/pic.png", self.picture)
        for path in ("/x/web-interface/archive/like", "/x/web-interface/coin/add", "/x/v3/fav/resource/deal", "/x/v2/history/toview/add", "/x/relation/modify", "/x/v2/history/report"):
            app.router.add_post(path, self.post)
        return app

    async def nav(self, request: web.Request) -> web.Response:
        response = await super().nav(request)
        if "SESSDATA=sess" in request.headers.get("Cookie", ""):
            return web.json_response({"code": 0, "data": {"isLogin": True, "uname": "大肥鱼本鱼", "mid": 42, "money": 7, "wbi_img": {"img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png", "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png"}}})
        return response

    async def qr_generate(self, request: web.Request) -> web.Response:
        return web.json_response({"code": 0, "data": {"url": "https://account.bilibili.com/h5/account-h5/auth/scan-web?qrcode_key=k1", "qrcode_key": "k1"}})

    async def qr_poll(self, request: web.Request) -> web.Response:
        assert request.query["qrcode_key"] == "k1"
        code = self.qr_codes[min(self.qr_polls, len(self.qr_codes) - 1)]
        self.qr_polls += 1
        response = web.json_response({"code": 0, "data": {"code": code, "refresh_token": "rt" if code == 0 else "", "url": ""}})
        if code == 0:
            response.set_cookie("SESSDATA", "sess")
            response.set_cookie("bili_jct", "csrf-token")
            response.set_cookie("DedeUserID", "42")
        return response

    async def feed(self, request: web.Request) -> web.Response:
        self.check_signature(request)
        return web.json_response({"code": 0, "data": {"item": [
            {"goto": "av", "bvid": BVID, "id": 170001, "title": "猫跳上桌子", "owner": {"name": "猫猫UP", "mid": 9}, "duration": 95, "stat": {"view": 123456}},
            {"goto": "ad", "bvid": "BV1ad", "title": "广告"},
            {"goto": "av", "bvid": "BV1long", "id": 2, "title": "三小时直播回放", "owner": {"name": "某人", "mid": 1}, "duration": 3 * 3600, "stat": {"view": 5}},
            {"goto": "av", "bvid": "BV1blk", "id": 3, "title": "屏蔽词视频", "owner": {"name": "某人", "mid": 1}, "duration": 60, "stat": {"view": 5}},
        ]}})

    async def popular(self, request: web.Request) -> web.Response:
        return web.json_response({"code": 0, "data": {"list": [{"bvid": BVID, "aid": 170001, "title": "热门猫", "owner": {"name": "猫猫UP", "mid": 9}, "duration": 95, "stat": {"view": 9}}]}})

    async def search(self, request: web.Request) -> web.Response:
        self.check_signature(request)
        assert request.query["keyword"] == "鲸鱼"
        return web.json_response({"code": 0, "data": {"result": [{"bvid": BVID, "aid": 170001, "title": '<em class="keyword">鲸鱼</em>顶海带', "author": "海洋台", "mid": 5, "duration": "4:31", "play": 20000}]}})

    async def replies(self, request: web.Request) -> web.Response:
        self.check_signature(request)
        if self.reply_status != 200:
            return web.Response(status=self.reply_status)
        if self.reply_code:
            return web.json_response({"code": self.reply_code, "message": "风控"})
        picture = f"{self.base}/bfs/new_dyn/pic.png"
        return web.json_response({"code": 0, "data": {"replies": [
            {"rpid": 1, "member": {"uname": "网友甲"}, "like": 520, "content": {"message": "这猫绝了", "pictures": [{"img_src": picture}]},
             "replies": [{"rpid": 11, "member": {"uname": "网友乙"}, "like": 3, "content": {"message": "笑死，猫：我没摔"}}]},
            {"rpid": 2, "member": {"uname": "网友丙"}, "like": 0, "content": {"message": "前方高能"}},
        ]}})

    async def folders(self, request: web.Request) -> web.Response:
        return web.json_response({"code": 0, "data": {"list": [{"id": 777, "title": "默认收藏夹"}]}})

    async def picture(self, request: web.Request) -> web.Response:
        return web.Response(body=_png(), content_type="image/png")

    async def post(self, request: web.Request) -> web.Response:
        form = dict(await request.post())
        self.posts.append({"path": request.path, **form})
        return web.json_response({"code": 0, "data": {}})


def with_account_world(monkeypatch, test):
    async def main():
        world = AccountWorld()
        server = TestServer(world.app())
        await server.start_server()
        world.base = str(server.make_url("")).rstrip("/")
        monkeypatch.setattr(BiliClient, "API", world.base)
        monkeypatch.setattr(BiliClient, "COMMENT", world.base)
        monkeypatch.setattr(BiliClient, "WWW", world.base)
        monkeypatch.setattr(Browser, "PASSPORT", world.base)
        monkeypatch.setattr(Browser, "IMAGE_HOSTS", ("hdslb.com", "127.0.0.1"))
        monkeypatch.setattr(plugin_module, "QR_POLL_SECONDS", 0.01)
        monkeypatch.setitem(qwen_module.REGIONS, "beijing", world.base)
        try:
            await test(world)
        finally:
            await server.close()

    asyncio.run(main())


def fast_limits(**kw) -> Limits:
    return Limits(**{"request_interval": 0, "action_interval": 0, **kw})


async def login(plugin, host, world) -> None:
    await plugin.handle_login(stream_id="s1")
    await asyncio.wait_for(plugin._login_task, 5)
    plugin._account.limits = fast_limits()


# ── 纯逻辑 ────────────────────────────────────


def test_account_guard_quota_dedupe_and_pause(tmp_path):
    account = Account(tmp_path, fast_limits(per_day={"like": 2, "coin": 0, "favorite": 1, "later": 1, "follow": 1, "history": 1}))
    account.check_action("like", "BV1")
    account.record_action("like", "BV1")
    try:
        account.check_action("like", "BV1")
        raise AssertionError("重复点赞应该被拦")
    except QuotaExceeded as exc:
        assert "已经点赞过了" in str(exc)
    account.record_action("like", "BV2")
    try:
        account.check_action("like", "BV3")
        raise AssertionError("超配额应该被拦")
    except QuotaExceeded as exc:
        assert "次数用完了" in str(exc)
    assert account.pause_reason_for(http_status=412).startswith("被 B 站风控拦下")
    account.pause(account.pause_reason_for(code=-352))
    again = Account(tmp_path)  # 重启后仍然暂停
    try:
        again.before_request()
        raise AssertionError("暂停中不该发请求")
    except AccountPaused as exc:
        assert "风控" in str(exc)
    assert again.resume() and not again.paused


def test_parse_duration():
    assert parse_duration("4:31") == 271 and parse_duration("1:02:03") == 3723 and parse_duration(95) == 95


# ── 接进插件 ──────────────────────────────────


def test_qr_login_then_browse_comments_actions_emoji(tmp_path, monkeypatch):
    host = FakeHost()
    config = copy.deepcopy(ACCOUNT_CONFIG)
    config["browse"] = {"blocked_keywords": ["屏蔽词"], "max_minutes": 40, "comment_images": 2}
    plugin = make_plugin(tmp_path, host, config)

    async def test(world):
        await plugin.on_load()
        # 没登录：首页推荐是大众推荐；操作做不了
        plugin._account.limits = fast_limits()
        anon = await plugin.tool_browse(source="推荐")
        assert anon["success"] and "还没登录" in anon["content"]
        assert (await plugin.tool_action(video=BVID, action="点赞"))["success"] is False

        await login(plugin, host, world)
        sent = [p["args"] for name, p in host.calls if name == "cap.call" and p["capability"] in ("send.image", "send.text")]
        assert sent[0]["image_base64"] and sent[0]["storage_message"] is False  # 二维码不进聊天记录
        assert any("扫到了" in a.get("text", "") for a in sent) and any("登录好了：大肥鱼本鱼" in a.get("text", "") for a in sent)
        assert plugin._account.logged_in and plugin._account.credential()["uname"] == "大肥鱼本鱼"
        assert "SESSDATA=sess" in plugin._watcher.bili._headers()["Cookie"]  # 看视频也用她的账号

        feed = await plugin.tool_browse(source="推荐")
        assert feed["success"] and "你的首页推荐" in feed["content"] and "猫跳上桌子" in feed["content"]
        assert "广告" not in feed["content"] and "三小时" not in feed["content"] and "屏蔽词" not in feed["content"]
        found = await plugin.tool_browse(source="搜索", keyword="鲸鱼")
        assert "鲸鱼顶海带｜海洋台｜04:31｜2.0万播放" in found["content"]
        assert (await plugin.tool_browse(source="搜索"))["success"] is False
        assert "热门猫" in (await plugin.tool_browse(source="热门"))["content"]

        comments = await plugin.tool_comments(video=BVID)
        assert "网友甲（👍520）：这猫绝了 [图1]" in comments["content"] and "↳ 网友乙（👍3）：笑死，猫：我没摔" in comments["content"]
        image = comments["content_items"][0]
        assert image["type"] == "image" and image["name"] == "图1"
        with Image.open(io.BytesIO(base64.b64decode(image["data"]))) as thumb:
            assert max(thumb.size) <= 512

        liked = await plugin.tool_action(video=BVID, action="点赞")
        assert liked["success"] and "已经点赞" in liked["content"]
        assert (await plugin.tool_action(video=BVID, action="点赞"))["success"] is False  # 不重复点
        assert (await plugin.tool_action(video=BVID, action="收藏"))["success"]
        assert (await plugin.tool_action(video=BVID, action="关注UP主"))["success"]
        assert (await plugin.tool_action(video=BVID, action="转发"))["success"] is False
        posted = {p["path"]: p for p in world.posts}
        assert posted["/x/web-interface/archive/like"]["csrf"] == "csrf-token"
        assert posted["/x/v3/fav/resource/deal"]["add_media_ids"] == "777"

        saved = await plugin.tool_save_emoji(image="图1")
        assert saved["success"]
        register = next(p for name, p in host.calls if name == "cap.call" and p["capability"] == "emoji.register")
        assert base64.b64decode(register["args"]["emoji_base64"]) == _png()
        assert (await plugin.tool_save_emoji(image="图9"))["success"] is False

        watched = await plugin.tool_watch(url=BVID)
        assert watched["success"]
        await asyncio.sleep(0.2)
        assert any(p["path"] == "/x/v2/history/report" for p in world.posts)  # 看完留观看记录

        status = await plugin.status_text()
        assert "她的账号：✅ 大肥鱼本鱼（硬币 7）" in status and "点赞 1" in status
        await plugin.on_unload()

    with_account_world(monkeypatch, test)


def test_risk_control_pauses_until_owner_resumes(tmp_path, monkeypatch):
    host = FakeHost()
    plugin = make_plugin(tmp_path, host, copy.deepcopy(ACCOUNT_CONFIG))

    async def test(world):
        await plugin.on_load()
        await login(plugin, host, world)
        world.reply_status = 412
        blocked = await plugin.tool_comments(video=BVID)
        assert blocked["success"] is False and "暂停" in blocked["error"]
        world.reply_status = 200
        still = await plugin.tool_browse(source="热门")  # 别的请求也不发了
        assert still["success"] is False and "暂停" in still["error"]
        assert "⛔ 账号操作已暂停" in await plugin.status_text()
        await plugin.handle_resume(stream_id="s1")
        assert (await plugin.tool_browse(source="热门"))["success"]
        world.reply_code = -352
        assert "风控" in (await plugin.tool_comments(video=BVID))["error"]
        await plugin.on_unload()

    with_account_world(monkeypatch, test)



def test_credential_private_and_pause_hits_queued_requests_and_actions(tmp_path, monkeypatch):
    import os
    import stat

    host = FakeHost()
    plugin = make_plugin(tmp_path, host, copy.deepcopy(ACCOUNT_CONFIG))

    async def test(world):
        await plugin.on_load()
        await login(plugin, host, world)
        cred = tmp_path / "data" / "account" / "credential.json"
        assert stat.S_IMODE(os.stat(cred).st_mode) == 0o600  # 登录信息只有插件能读（更新资料后也是）
        # 并发投币：上限 1 次，只会真的投一次
        plugin._account.limits = fast_limits(per_day={"like": 30, "coin": 1, "favorite": 15, "later": 15, "follow": 5, "history": 40})
        results = await asyncio.gather(*(plugin.tool_action(video=BVID, action="投币") for _ in range(3)))
        assert sum(r["success"] for r in results) == 1
        assert sum(p["path"] == "/x/web-interface/coin/add" for p in world.posts) == 1
        # 排队中的请求：前一个被拦下暂停后，后一个不再发出
        plugin._account.limits.request_interval = 0.3
        world.reply_status = 412
        browser = plugin._browser
        await browser.bili._wbi_key()  # 签名参数先拿好，第一个请求一到就发出去
        first, second = await asyncio.gather(
            browser._call("GET", "/x/v2/reply/wbi/main", params={"oid": 1}, wbi=True), browser.popular(), return_exceptions=True
        )
        assert isinstance(first, AccountPaused) and isinstance(second, AccountPaused)
        assert not any(p.get("path") == "/x/web-interface/popular" for p in world.posts)
        assert "SESSDATA" not in plugin._watcher.bili._headers()["Cookie"]  # 看视频那边也撤掉了她的登录信息
        await plugin.handle_resume(stream_id="s1")
        assert "SESSDATA=sess" in plugin._watcher.bili._headers()["Cookie"]
        await plugin.on_unload()

    with_account_world(monkeypatch, test)


def test_image_numbers_do_not_collide_between_comment_calls(tmp_path, monkeypatch):
    host = FakeHost()
    plugin = make_plugin(tmp_path, host, copy.deepcopy(ACCOUNT_CONFIG))

    async def test(world):
        await plugin.on_load()
        await login(plugin, host, world)
        first = await plugin.tool_comments(video=BVID)
        second = await plugin.tool_comments(video=BVID)
        assert "[图1]" in first["content"] and "[图2]" in second["content"]
        assert set(plugin._images) == {"图1", "图2"}
        await plugin.on_unload()

    with_account_world(monkeypatch, test)


def test_watch_path_with_her_login_is_guarded(tmp_path, monkeypatch):
    host = FakeHost()
    plugin = make_plugin(tmp_path, host, copy.deepcopy(ACCOUNT_CONFIG))

    async def test(world):
        await plugin.on_load()
        await login(plugin, host, world)
        before = plugin._account.today()["requests"]
        assert (await plugin.tool_watch(url=BVID))["success"]
        assert plugin._account.today()["requests"] > before  # 看视频带她的登录信息时也计数
        world.block_view = 2  # 视频信息接口被风控：她的账号暂停
        plugin._cache._data.clear() if hasattr(plugin._cache, "_data") else None
        await plugin._watcher.bili.view(await plugin._watcher.bili.resolve("BV1another00"))
        assert "风控" in plugin._account.paused
        # 暂停后重建：看视频完全不带登录信息，也不退回配置里的旧 Cookie
        plugin.set_plugin_config({**copy.deepcopy(ACCOUNT_CONFIG), "bilibili": {"cookie": "SESSDATA=old", "quality": "360P"}})
        await plugin.on_config_update("self", {}, "")
        assert "SESSDATA" not in plugin._watcher.bili._headers()["Cookie"]
        await plugin.on_unload()

    with_account_world(monkeypatch, test)


def test_forbidden_features_are_not_retried_until_owner_resumes(tmp_path, monkeypatch):
    host = FakeHost()
    plugin = make_plugin(tmp_path, host, copy.deepcopy(ACCOUNT_CONFIG))

    async def test(world):
        await plugin.on_load()
        await login(plugin, host, world)
        world.reply_code = -403
        first = await plugin.tool_comments(video=BVID)
        assert first["success"] is False and "不让你的账号看评论区" in first["error"]
        world.reply_code = 0
        again = await plugin.tool_comments(video=BVID)  # 不再去碰 B 站
        assert again["success"] is False and "先别用了" in again["error"]
        assert "🚫 B 站不让她的账号看评论区" in await plugin.status_text()
        assert (await plugin.tool_browse(source="热门"))["success"]  # 别的功能照常
        await plugin.handle_resume(stream_id="s1")
        assert (await plugin.tool_comments(video=BVID))["success"]
        await plugin.on_unload()

    with_account_world(monkeypatch, test)
