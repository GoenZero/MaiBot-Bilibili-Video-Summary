"""用她自己的账号逛 B 站：首页推荐、热门、搜索、评论区，以及点赞、投币、收藏、稍后再看、关注、观看记录。

都是 B 站网页自己用的接口（不是正式开放接口），B 站改了就要跟着改。
每个请求都先过 Account 的守卫：暂停中不发、超配额不发、两次请求之间留间隔；
被拦下（未登录、风控、太频繁）就持久暂停账号操作，不重试、不换路绕。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import asyncio
import html
import json
import logging
import re

import aiohttp

from .account import QR_DONE, Account, AccountPaused, cookies_from_response
from .bilibili import USER_AGENT, BiliClient, BiliError, VideoInfo, _info_from_view, format_seconds, sign_wbi
from .links import VideoRef

_EM = re.compile(r"</?em[^>]*>")


@dataclass
class VideoCard:
    bvid: str
    aid: int
    title: str
    owner: str
    owner_mid: int
    duration: int  # 秒
    views: int

    def line(self, index: int) -> str:
        views = f"{self.views / 10000:.1f}万" if self.views >= 10000 else str(self.views)
        return f"{index}. {self.title}｜{self.owner}｜{format_seconds(self.duration)}｜{views}播放｜{self.bvid}"


@dataclass
class Comment:
    user: str
    text: str
    likes: int
    pictures: List[str] = field(default_factory=list)
    replies: List["Comment"] = field(default_factory=list)


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_duration(value: Any) -> int:
    """接口里的时长：秒数，或者搜索结果里的「4:31」「1:02:03」。"""
    if isinstance(value, (int, float)):
        return int(value)
    parts = [p for p in str(value or "").split(":") if p.strip().isdigit()]
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds


class Browser:
    PASSPORT = "https://passport.bilibili.com"
    WWW = "https://www.bilibili.com"
    IMAGE_HOSTS: tuple = ("hdslb.com",)

    def __init__(self, session: aiohttp.ClientSession, bili: BiliClient, account: Account, logger: Optional[logging.Logger] = None) -> None:
        self._session = session
        self.bili = bili
        self.account = account
        self.log = logger or logging.getLogger(__name__)
        self._feed_index = 0
        self._fav_folder: Optional[int] = None

    @property
    def api(self) -> str:
        return self.bili.API

    # ── 请求 ──────────────────────────────────────

    async def _cookies(self) -> Dict[str, str]:
        await self.bili._ensure_device()
        device = {k: v for k, v in self.bili._cookies.items() if k in ("buvid3", "buvid4", "b_nut")}
        return {**device, **self.account.cookies()}

    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        form: Optional[Dict[str, Any]] = None,
        wbi: bool = False,
        referer: str = "https://www.bilibili.com/",
        base: str = "",
    ) -> Dict[str, Any]:
        wait = self.account.before_request()
        if wait > 0:
            await asyncio.sleep(wait)
        cookies = await self._cookies()
        if wbi:
            params = sign_wbi(params or {}, await self.bili._wbi_key())
        if self.account.paused:  # 排队、准备期间别的请求刚触发了暂停
            raise AccountPaused(f"B 站那边暂停了：{self.account.paused}。等主人处理后再试")
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": referer,
            "Origin": "https://www.bilibili.com",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
        }
        url = (base or self.api) + path
        try:
            async with self._session.request(
                method, url, params=params, data=form, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as response:
                reason = self.account.pause_reason_for(http_status=response.status)
                if reason:
                    self.account.pause(reason)
                    raise AccountPaused(f"B 站那边暂停了：{reason}。等主人处理后再试")
                if response.status >= 400:
                    raise BiliError(f"B 站返回 HTTP {response.status}", f"{path} HTTP {response.status}")
                payload = await response.json(content_type=None)
        except asyncio.TimeoutError as exc:
            raise BiliError("连接 B 站超时", f"{path} 超时") from exc
        except aiohttp.ClientError as exc:
            raise BiliError(f"连接 B 站失败（{type(exc).__name__}）", f"{path} {type(exc).__name__}") from exc
        except ValueError as exc:
            raise BiliError("B 站返回的内容看不懂", f"{path} 不是 JSON") from exc
        except RuntimeError as exc:  # 等待期间插件重新加载、会话已关
            raise BiliError("插件刚重新加载，再试一次", f"{path} {exc}") from exc
        if not isinstance(payload, dict):
            raise BiliError("B 站返回的内容看不懂", f"{path} 不是 JSON 对象")
        code = _int(payload.get("code"))
        if code != 0:
            reason = self.account.pause_reason_for(code=code)
            if reason:
                self.account.pause(reason)
                raise AccountPaused(f"B 站那边暂停了：{reason}。等主人处理后再试")
            raise BiliError(f"B 站返回错误 {code}：{str(payload.get('message', ''))[:60]}", f"{path} code={code}")
        data = payload.get("data")
        return data if isinstance(data, dict) else {"_value": data}

    async def _post(self, path: str, form: Dict[str, Any], *, referer: str) -> Dict[str, Any]:
        csrf = self.account.csrf
        if not csrf:
            raise BiliError("还没登录她的 B 站账号，做不了这个")
        return await self._call("POST", path, form={**form, "csrf": csrf}, referer=referer)

    # ── 账号 ──────────────────────────────────────

    async def me(self) -> Dict[str, Any]:
        """登录状态、昵称、硬币数。登录信息失效（-101）或被拦时照样暂停。"""
        if not self.account.logged_in:
            return {"login": False, "uname": "", "mid": "", "coins": None}
        data = await self._call("GET", "/x/web-interface/nav")
        return {"login": bool(data.get("isLogin")), "uname": str(data.get("uname") or ""), "mid": str(data.get("mid") or ""), "coins": data.get("money")}

    async def view(self, ref: VideoRef) -> VideoInfo:
        """视频信息（带着她的账号，所以也过守卫）。"""
        data = await self._call("GET", "/x/web-interface/view", params={"bvid": ref.bvid} if ref.bvid else {"aid": ref.aid})
        return _info_from_view(data, ref)

    async def qr_start(self) -> Dict[str, str]:
        async with self._session.get(
            f"{self.PASSPORT}/x/passport-login/web/qrcode/generate",
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            payload = await response.json(content_type=None)
        data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else {}
        if not data.get("url") or not data.get("qrcode_key"):
            raise BiliError("拿不到登录二维码")
        return {"url": str(data["url"]), "key": str(data["qrcode_key"])}

    async def qr_poll(self, key: str) -> int:
        """查一次扫码状态；登录成功时把凭证存好。返回状态码。"""
        async with self._session.get(
            f"{self.PASSPORT}/x/passport-login/web/qrcode/poll",
            params={"qrcode_key": key},
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            payload = await response.json(content_type=None)
            cookies = cookies_from_response(response)
        data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else {}
        code = _int(data.get("code"))
        if code == QR_DONE:
            # 登录成功的跳转链接里也带着这几个 Cookie，响应头里没有时用它补上
            query = parse_qs(urlparse(str(data.get("url") or "")).query)
            for name in ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid"):
                if name not in cookies and query.get(name):
                    cookies[name] = query[name][0]
            if not cookies.get("SESSDATA"):
                raise BiliError("登录成功了但没拿到登录信息")
            self.account.save_credential(cookies, str(data.get("refresh_token") or ""), mid=cookies.get("DedeUserID", ""))
        return code

    # ── 找视频 ────────────────────────────────────

    async def feed(self, count: int = 12) -> List[VideoCard]:
        self._feed_index += 1
        data = await self._call(
            "GET",
            "/x/web-interface/wbi/index/top/feed/rcmd",
            params={"fresh_type": 4, "ps": count, "fresh_idx": self._feed_index, "fresh_idx_1h": self._feed_index, "brush": self._feed_index, "feed_version": "V8"},
            wbi=True,
        )
        cards = []
        for item in data.get("item") or []:
            if not isinstance(item, dict) or item.get("goto") != "av" or not item.get("bvid"):
                continue  # 广告、直播、番剧卡片
            owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
            stat = item.get("stat") if isinstance(item.get("stat"), dict) else {}
            cards.append(VideoCard(str(item["bvid"]), _int(item.get("id")), str(item.get("title") or ""), str(owner.get("name") or ""), _int(owner.get("mid")), _int(item.get("duration")), _int(stat.get("view"))))
        return cards

    async def popular(self, count: int = 12) -> List[VideoCard]:
        data = await self._call("GET", "/x/web-interface/popular", params={"ps": count, "pn": 1})
        cards = []
        for item in data.get("list") or []:
            if not isinstance(item, dict) or not item.get("bvid"):
                continue
            owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
            stat = item.get("stat") if isinstance(item.get("stat"), dict) else {}
            cards.append(VideoCard(str(item["bvid"]), _int(item.get("aid")), str(item.get("title") or ""), str(owner.get("name") or ""), _int(owner.get("mid")), _int(item.get("duration")), _int(stat.get("view"))))
        return cards

    async def search(self, keyword: str, page: int = 1) -> List[VideoCard]:
        data = await self._call(
            "GET", "/x/web-interface/wbi/search/type", params={"search_type": "video", "keyword": keyword, "page": max(1, page)}, wbi=True
        )
        cards = []
        for item in data.get("result") or []:
            if not isinstance(item, dict) or not item.get("bvid"):
                continue
            title = html.unescape(_EM.sub("", str(item.get("title") or "")))
            cards.append(VideoCard(str(item["bvid"]), _int(item.get("aid")), title, str(item.get("author") or ""), _int(item.get("mid")), parse_duration(item.get("duration")), _int(item.get("play"))))
        return cards

    # ── 评论区 ────────────────────────────────────

    async def comments(self, aid: int, bvid: str, *, newest: bool = False, count: int = 12) -> List[Comment]:
        data = await self._call(
            "GET",
            "/x/v2/reply/wbi/main",
            params={"type": 1, "oid": aid, "mode": 2 if newest else 3, "pagination_str": json.dumps({"offset": ""}), "plat": 1},
            wbi=True,
            referer=f"https://www.bilibili.com/video/{bvid}",
        )
        raw = [r for r in (data.get("top_replies") or []) + (data.get("replies") or []) if isinstance(r, dict)]
        seen, comments = set(), []
        for reply in raw:
            if reply.get("rpid") in seen:
                continue
            seen.add(reply.get("rpid"))
            comment = _comment(reply)
            if comment is None:
                continue
            comment.replies = [c for c in (_comment(r) for r in (reply.get("replies") or [])[:2] if isinstance(r, dict)) if c]
            comments.append(comment)
            if len(comments) >= count:
                break
        return comments

    async def download_image(self, url: str, max_bytes: int = 5 * 1024 * 1024) -> bytes:
        """只下载 B 站图床上的图（评论区的图都在那里）。"""
        url = "https:" + url if url.startswith("//") else url
        host = str(urlparse(url).hostname or "")
        if not any(host == allowed or host.endswith("." + allowed) for allowed in self.IMAGE_HOSTS):
            raise BiliError("不是 B 站的图片")
        if url.startswith("http://") and host.endswith("hdslb.com"):
            url = "https://" + url[len("http://") :]
        try:
            async with self._session.get(url, headers={"User-Agent": USER_AGENT, "Referer": self.WWW + "/"}, timeout=aiohttp.ClientTimeout(total=20)) as response:
                if response.status >= 400:
                    raise BiliError(f"图片下载失败：HTTP {response.status}")
                body = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise BiliError("图片太大")
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
            raise BiliError(f"图片下载失败（{type(exc).__name__}）") from exc
        return bytes(body)

    # ── 账号操作 ──────────────────────────────────

    async def like(self, aid: int, bvid: str) -> None:
        await self._post("/x/web-interface/archive/like", {"aid": aid, "like": 1}, referer=f"{self.WWW}/video/{bvid}")

    async def coin(self, aid: int, bvid: str) -> None:
        await self._post("/x/web-interface/coin/add", {"aid": aid, "multiply": 1, "select_like": 0, "cross_domain": "true"}, referer=f"{self.WWW}/video/{bvid}")

    async def later(self, aid: int, bvid: str) -> None:
        await self._post("/x/v2/history/toview/add", {"aid": aid}, referer=f"{self.WWW}/video/{bvid}")

    async def follow(self, mid: int, bvid: str) -> None:
        await self._post("/x/relation/modify", {"fid": mid, "act": 1, "re_src": 11}, referer=f"{self.WWW}/video/{bvid}")

    async def report_history(self, aid: int, cid: int, progress: int, bvid: str) -> None:
        await self._post("/x/v2/history/report", {"aid": aid, "cid": cid, "progress": progress}, referer=f"{self.WWW}/video/{bvid}")

    async def favorite(self, aid: int, bvid: str) -> None:
        if self._fav_folder is None:
            mid = self.account.credential().get("mid") or self.account.cookies().get("DedeUserID", "")
            data = await self._call("GET", "/x/v3/fav/folder/created/list-all", params={"up_mid": mid, "type": 2})
            folders = [f for f in data.get("list") or [] if isinstance(f, dict) and f.get("id")]
            if not folders:
                raise BiliError("找不到她的收藏夹")
            self._fav_folder = _int(folders[0]["id"])  # 第一个是默认收藏夹
        await self._post(
            "/x/v3/fav/resource/deal", {"rid": aid, "type": 2, "add_media_ids": self._fav_folder, "del_media_ids": ""}, referer=f"{self.WWW}/video/{bvid}"
        )


def _comment(reply: Dict[str, Any]) -> Optional[Comment]:
    content = reply.get("content") if isinstance(reply.get("content"), dict) else {}
    member = reply.get("member") if isinstance(reply.get("member"), dict) else {}
    text = " ".join(str(content.get("message") or "").split())
    pictures = [str(p.get("img_src")) for p in content.get("pictures") or [] if isinstance(p, dict) and p.get("img_src")]
    if not text and not pictures:
        return None
    return Comment(str(member.get("uname") or "某人"), text[:150], _int(reply.get("like")), pictures[:3])
