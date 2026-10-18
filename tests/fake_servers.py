"""本地假的 B 站和百炼服务器（测试不连外网）。"""

from __future__ import annotations

from typing import Any, Dict, List

import hashlib
import json
from urllib.parse import urlencode

from aiohttp import web
from aiohttp.test_utils import TestServer

from bili_video_under_test.core.bilibili import mixin_key

IMG_KEY = "7cd084941338484aae1ad9425b84077c"
SUB_KEY = "4932caff0ff746eab6f01bf08b70ac45"
BVID = "BV1xx411c7mD"
VIDEO_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"v" * 2000


class FakeWorld:
    """记录收到的请求，按需返回数据。"""

    def __init__(self) -> None:
        self.duration = 95
        self.subtitles: List[Dict[str, Any]] = [{"from": 1.5, "to": 3, "content": "大家好"}, {"from": 65, "to": 67, "content": "今天做饭"}]
        self.subtitle_aid = 170001
        self.playurl_mode = "durl"
        self.bad_signatures = 0
        self.qwen_payloads: List[Dict[str, Any]] = []
        self.qwen_reply = ["画面里一只猫", "跳上了桌子，笑点是它摔了下来。"]
        self.qwen_status = 200
        self.upload_policy_host = "http://127.0.0.1/evil"
        self.video_size = len(VIDEO_BYTES)
        self.media_hits = 0
        self.base = ""

    def check_signature(self, request: web.Request) -> None:
        params = dict(request.query)
        w_rid = params.pop("w_rid", "")
        query = urlencode(dict(sorted(params.items())))
        if hashlib.md5((query + mixin_key(IMG_KEY, SUB_KEY)).encode()).hexdigest() != w_rid:
            self.bad_signatures += 1

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/x/web-interface/nav", self.nav)
        app.router.add_get("/x/web-interface/view", self.view)
        app.router.add_get("/x/player/wbi/playurl", self.playurl)
        app.router.add_get("/x/player/wbi/v2", self.player_v2)
        app.router.add_get("/sub.json", self.subtitle_json)
        app.router.add_get("/170002.xml", self.danmaku)
        app.router.add_get("/media/v.mp4", self.media)
        app.router.add_get("/media/a.m4a", self.media)
        app.router.add_get("/short/{code}", self.short)
        app.router.add_get("/api/v1/uploads", self.upload_policy)
        app.router.add_post("/compatible-mode/v1/chat/completions", self.chat)
        return app

    async def nav(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "code": -101,
                "data": {
                    "isLogin": False,
                    "wbi_img": {
                        "img_url": f"https://i0.hdslb.com/bfs/wbi/{IMG_KEY}.png",
                        "sub_url": f"https://i0.hdslb.com/bfs/wbi/{SUB_KEY}.png",
                    },
                },
            }
        )

    async def view(self, request: web.Request) -> web.Response:
        if request.query.get("bvid") == "BV1missing00":
            return web.json_response({"code": -404, "message": "啥都木有"})
        pages = [
            {"cid": 170002, "part": "正片", "duration": self.duration},
            {"cid": 170003, "part": "花絮", "duration": 30},
        ]
        return web.json_response(
            {
                "code": 0,
                "data": {
                    "bvid": BVID,
                    "aid": 170001,
                    "title": "猫猫跳桌子",
                    "desc": "我家猫今天又整活了",
                    "tname": "动物圈",
                    "owner": {"name": "养猫的人"},
                    "duration": self.duration + 30,
                    "pages": pages,
                },
            }
        )

    async def playurl(self, request: web.Request) -> web.Response:
        self.check_signature(request)
        if self.playurl_mode == "durl" and request.query.get("fnval") == "1":
            return web.json_response({"code": 0, "data": {"durl": [{"url": f"{self.base}/media/v.mp4", "size": self.video_size}]}})
        if self.playurl_mode == "dash" and request.query.get("fnval") == "4048":
            return web.json_response(
                {
                    "code": 0,
                    "data": {
                        "dash": {
                            "video": [
                                {"id": 32, "codecid": 7, "bandwidth": 900, "baseUrl": f"{self.base}/media/v.mp4"},
                                {"id": 16, "codecid": 12, "bandwidth": 300, "baseUrl": "http://wrong/hevc"},
                                {"id": 16, "codecid": 7, "bandwidth": 400, "baseUrl": f"{self.base}/media/v.mp4"},
                            ],
                            "audio": [
                                {"bandwidth": 320000, "baseUrl": "http://wrong/flac"},
                                {"bandwidth": 64000, "baseUrl": f"{self.base}/media/a.m4a"},
                            ],
                        }
                    },
                }
            )
        return web.json_response({"code": 0, "data": {}})

    async def player_v2(self, request: web.Request) -> web.Response:
        self.check_signature(request)
        # 真实的 B 站给的是「//aisubtitle.hdslb.com/...」（插件会补上 https:）；本地假服务器只有 http，所以给完整地址
        tracks = [{"lan": "ai-zh", "subtitle_url": f"{self.base}/sub.json"}] if self.subtitles else []
        return web.json_response({"code": 0, "data": {"aid": self.subtitle_aid, "cid": 170002, "subtitle": {"subtitles": tracks}}})

    async def subtitle_json(self, request: web.Request) -> web.Response:
        return web.json_response({"body": self.subtitles})

    async def danmaku(self, request: web.Request) -> web.Response:
        lines = ["哈哈哈哈"] * 5 + ["猫：我没摔"] * 3 + ["前排"] * 3 + ["a"]
        xml = "<i>" + "".join(f'<d p="1,1,25,1">{line}</d>' for line in lines) + "</i>"
        return web.Response(text=xml, content_type="text/xml")

    async def media(self, request: web.Request) -> web.Response:
        self.media_hits += 1
        if request.headers.get("Referer", "").find("bilibili.com") < 0:
            return web.Response(status=403)
        return web.Response(body=VIDEO_BYTES if self.video_size == len(VIDEO_BYTES) else b"x" * self.video_size)

    async def short(self, request: web.Request) -> web.Response:
        return web.Response(status=302, headers={"Location": f"https://www.bilibili.com/video/{BVID}?p=2&share_source=copy"})

    async def upload_policy(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "data": {
                    "upload_dir": "dashscope-instant/abc",
                    "oss_access_key_id": "id",
                    "signature": "sig",
                    "policy": "p",
                    "x_oss_object_acl": "private",
                    "x_oss_forbid_overwrite": "true",
                    "upload_host": self.upload_policy_host,
                }
            }
        )

    async def chat(self, request: web.Request) -> web.StreamResponse:
        self.qwen_payloads.append(await request.json())
        if self.qwen_status != 200:
            return web.json_response({"error": {"message": "Arrearage"}}, status=self.qwen_status)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for piece in self.qwen_reply:
            event = {"choices": [{"delta": {"content": piece}}]}
            await response.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
        await response.write(b"data: [DONE]\n\n")
        return response


async def start(world: FakeWorld) -> TestServer:
    server = TestServer(world.app())
    await server.start_server()
    world.base = str(server.make_url("")).rstrip("/")
    return server
