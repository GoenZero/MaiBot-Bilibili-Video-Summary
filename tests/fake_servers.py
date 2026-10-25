"""本地假的 B 站和百炼服务器（测试不连外网）。"""

from __future__ import annotations

from typing import Any, Dict, List

import asyncio
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
        self.view_points: List[Dict[str, Any]] = [
            {"type": 2, "from": 65, "to": 120, "content": "开始  做饭", "imgUrl": "x"},
            {"type": 2, "from": 0, "to": 65, "content": "开场", "imgUrl": "x"},
        ]
        self.playurl_mode = "durl"  # durl：只有合并的 mp4；dash：只有分开的；both：两种都有
        self.durl_size = 0  # 合并 mp4 报的大小，0 表示和实际下载的一样
        self.oss_delay = 0.0  # 收完文件后多久才回应
        self.bad_signatures = 0
        self.qwen_payloads: List[Dict[str, Any]] = []
        self.qwen_reply = ["画面里一只猫", "跳上了桌子，笑点是它摔了下来。"]
        self.qwen_status = 200
        self.qwen_delay = 0.0  # 模型开始回文字前的停顿（秒）
        self.upload_policy_host = "http://127.0.0.1/evil"
        self.oss_uploads: List[Dict[str, Any]] = []
        self.upload_dir = "dashscope-instant/abc"
        self.qwen_headers: List[Dict[str, str]] = []
        self.video_size = len(VIDEO_BYTES)
        self.media_hits = 0
        self.base = ""
        self.block_view = 0  # 接下来几次 view 请求返回 HTTP 412
        self.block_logged_in = False  # 带 SESSDATA 的请求都被拦
        self.block_playurl = False
        self.block_page = False
        self.page_hits = 0
        self.view_hits = 0
        self.player_hits = 0
        self.cookies_seen: List[str] = []
        # 假 Gemini
        self.gemini_payloads: List[Dict[str, Any]] = []
        self.gemini_headers: List[Dict[str, str]] = []
        self.gemini_status = 200
        self.gemini_delay = 0.0
        self.gemini_reply: Dict[str, Any] = {
            "candidates": [
                {
                    "content": {"parts": [{"text": "先想想", "thought": True}, {"text": "画面里一只猫跳上了桌子，笑点是它摔了下来。"}]},
                    "finishReason": "STOP",
                }
            ]
        }
        self.gemini_uploads: List[Dict[str, Any]] = []
        self.gemini_polls = 0
        self.gemini_deleted: List[str] = []

    def check_signature(self, request: web.Request) -> None:
        params = dict(request.query)
        w_rid = params.pop("w_rid", "")
        query = urlencode(dict(sorted(params.items())))
        if hashlib.md5((query + mixin_key(IMG_KEY, SUB_KEY)).encode()).hexdigest() != w_rid:
            self.bad_signatures += 1

    def app(self) -> web.Application:
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_get("/x/web-interface/nav", self.nav)
        app.router.add_get("/x/frontend/finger/spi", self.spi)
        app.router.add_get("/x/web-interface/view", self.view)
        app.router.add_get("/x/player/wbi/playurl", self.playurl)
        app.router.add_get("/x/player/wbi/v2", self.player_v2)
        app.router.add_get("/sub.json", self.subtitle_json)
        app.router.add_get("/170002.xml", self.danmaku)
        app.router.add_get("/media/v.mp4", self.media)
        app.router.add_get("/media/a.m4a", self.media)
        app.router.add_get("/short/{code}", self.short)
        app.router.add_get("/video/{ident}", self.video_page)
        app.router.add_get("/api/v1/uploads", self.upload_policy)
        app.router.add_post("/compatible-mode/v1/chat/completions", self.chat)
        app.router.add_post("/oss", self.oss_upload)
        app.router.add_post("/v1beta/models/{tail}", self.gemini_generate)
        app.router.add_post("/upload/v1beta/files", self.gemini_upload_start)
        app.router.add_post("/upload-session/1", self.gemini_upload_bytes)
        app.router.add_get("/v1beta/files/{name}", self.gemini_file)
        app.router.add_delete("/v1beta/files/{name}", self.gemini_delete)
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

    async def spi(self, request: web.Request) -> web.Response:
        return web.json_response({"code": 0, "data": {"b_3": "REAL-BUVID3", "b_4": "REAL-BUVID4"}})

    async def view(self, request: web.Request) -> web.Response:
        self.view_hits += 1
        cookie = request.headers.get("Cookie", "")
        self.cookies_seen.append(cookie)
        if self.block_logged_in and "SESSDATA" in cookie:
            return web.Response(status=412, text="blocked")
        if self.block_view > 0:
            self.block_view -= 1
            return web.Response(status=412, text="blocked")
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
                    "owner": {"name": "养猫的人", "mid": 9},
                    "duration": self.duration + 30,
                    "pages": pages,
                },
            }
        )

    async def playurl(self, request: web.Request) -> web.Response:
        self.check_signature(request)
        if self.block_playurl:
            return web.Response(status=412, text="blocked")
        if self.playurl_mode in ("durl", "both") and request.query.get("fnval") == "1":
            size = self.durl_size or self.video_size
            return web.json_response({"code": 0, "data": {"durl": [{"url": f"{self.base}/media/v.mp4", "size": size}]}})
        if self.playurl_mode in ("dash", "both") and request.query.get("fnval") == "4048":
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
        self.player_hits += 1
        self.check_signature(request)
        # 真实的 B 站给的是「//aisubtitle.hdslb.com/...」（插件会补上 https:）；本地假服务器只有 http，所以给完整地址
        tracks = [{"lan": "ai-zh", "subtitle_url": f"{self.base}/sub.json"}] if self.subtitles else []
        data = {"aid": self.subtitle_aid, "cid": 170002, "subtitle": {"subtitles": tracks}, "view_points": self.view_points}
        return web.json_response({"code": 0, "data": data})

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

    async def video_page(self, request: web.Request) -> web.Response:
        """视频网页：里面嵌着视频信息（__INITIAL_STATE__）和播放地址（__playinfo__）。"""
        self.page_hits += 1
        if self.block_page:
            return web.Response(status=412, text="blocked")
        video_data = {
            "bvid": BVID,
            "aid": 170001,
            "title": "猫猫跳桌子",
            "desc": "我家猫今天又整活了",
            "tname": "动物圈",
            "owner": {"name": "养猫的人", "mid": 9},
            "duration": self.duration,
            "pages": [{"cid": 170002, "part": "正片", "duration": self.duration}],
        }
        state = {"videoData": video_data, "p": 1}
        play = {"code": 0, "data": {"durl": [{"url": f"{self.base}/media/v.mp4", "size": self.video_size}]}}
        html = (
            "<html><head><script>window.__playinfo__="
            + json.dumps(play)
            + "</script></head><body><script>window.__INITIAL_STATE__="
            + json.dumps(state, ensure_ascii=False)
            + ";(function(){var s;}());</script></body></html>"
        )
        return web.Response(text=html, content_type="text/html")

    async def short(self, request: web.Request) -> web.Response:
        return web.Response(status=302, headers={"Location": f"https://www.bilibili.com/video/{BVID}?p=2&share_source=copy"})

    async def upload_policy(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "data": {
                    "upload_dir": self.upload_dir,
                    "oss_access_key_id": "id",
                    "signature": "sig",
                    "policy": "p",
                    "x_oss_object_acl": "private",
                    "x_oss_forbid_overwrite": "true",
                    "upload_host": self.upload_policy_host,
                }
            }
        )

    async def oss_upload(self, request: web.Request) -> web.Response:
        """像阿里云 OSS 一样挑剔：要有总长度，文本字段不能带 Content-Type，文件必须放最后。"""

        def error(code: str, message: str) -> web.Response:
            xml = f"<?xml version=\"1.0\"?><Error><Code>{code}</Code><Message>{message}</Message></Error>"
            return web.Response(status=400, text=xml, content_type="application/xml")

        if not request.headers.get("Content-Length"):
            return error("MissingContentLength", "You must provide the Content-Length HTTP header.")
        reader = await request.multipart()
        fields: Dict[str, str] = {}
        file_size = -1
        while (part := await reader.next()) is not None:
            if file_size >= 0:
                return error("InvalidArgument", "file must be the last field")
            if part.name == "file":
                file_size = len(await part.read())
            else:
                if part.headers.get("Content-Type"):
                    return error("InvalidPolicyDocument", "form field must not have Content-Type")
                fields[part.name or ""] = (await part.read()).decode()
        if file_size < 0 or not fields.get("key", "").startswith("dashscope-instant/abc/"):
            return error("InvalidArgument", "missing file or key")
        if self.oss_delay:
            await asyncio.sleep(self.oss_delay)
        self.oss_uploads.append({"fields": fields, "size": file_size})
        return web.Response(status=200)

    async def gemini_generate(self, request: web.Request) -> web.Response:
        self.gemini_headers.append(dict(request.headers))
        self.gemini_payloads.append({"path": request.path, **(await request.json())})
        if self.gemini_status != 200:
            return web.json_response({"error": {"message": "API key not valid"}}, status=self.gemini_status)
        if self.gemini_delay:
            await asyncio.sleep(self.gemini_delay)
        return web.json_response(self.gemini_reply)

    async def gemini_upload_start(self, request: web.Request) -> web.Response:
        h = request.headers
        if h.get("X-Goog-Upload-Protocol") != "resumable" or h.get("X-Goog-Upload-Command") != "start":
            return web.json_response({"error": {"message": "bad start"}}, status=400)
        self.gemini_uploads.append(
            {"length": int(h["X-Goog-Upload-Header-Content-Length"]), "mime": h["X-Goog-Upload-Header-Content-Type"], "meta": await request.json()}
        )
        return web.Response(headers={"X-Goog-Upload-URL": f"{self.base}/upload-session/1"})

    async def gemini_upload_bytes(self, request: web.Request) -> web.Response:
        if request.headers.get("X-Goog-Upload-Command") != "upload, finalize" or request.headers.get("X-Goog-Upload-Offset") != "0":
            return web.json_response({"error": {"message": "bad upload"}}, status=400)
        data = await request.read()
        if len(data) != self.gemini_uploads[-1]["length"]:
            return web.json_response({"error": {"message": "length mismatch"}}, status=400)
        self.gemini_uploads[-1]["size"] = len(data)
        return web.json_response({"file": {"name": "files/abc", "uri": f"{self.base}/v1beta/files/abc", "state": "PROCESSING"}})

    async def gemini_file(self, request: web.Request) -> web.Response:
        self.gemini_polls += 1
        state = "ACTIVE" if self.gemini_polls >= 2 else "PROCESSING"
        return web.json_response({"name": "files/abc", "uri": f"{self.base}/v1beta/files/abc", "state": state})

    async def gemini_delete(self, request: web.Request) -> web.Response:
        self.gemini_deleted.append(request.match_info["name"])
        return web.json_response({})

    async def chat(self, request: web.Request) -> web.StreamResponse:
        self.qwen_headers.append(dict(request.headers))
        self.qwen_payloads.append(await request.json())
        if self.qwen_status != 200:
            return web.json_response({"error": {"message": "Arrearage"}}, status=self.qwen_status)
        if self.qwen_delay:
            await asyncio.sleep(self.qwen_delay)  # 先看完视频，期间一个字节都不发
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
