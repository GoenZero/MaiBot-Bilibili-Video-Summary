"""B 站网页接口：视频信息、播放地址、字幕、弹幕、登录状态。

这些都是 B 站网页自己用的接口，不是正式开放接口，B 站改了就要跟着改。
WBI 签名的做法参考了 maibot-qwen-video（MIT 许可，© 2026 MaiBot Community）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urljoin, urlparse

import asyncio
import base64
import hashlib
import html
import json
import logging
import random
import re
import string
import time
import uuid

import aiohttp

from .links import VideoRef, is_short_url, parse_video_url, ref_from_text

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0 Safari/537.36"
# B 站网页端 getMixinKey() 用的置换表
_MIXIN_TABLE = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)
_WBI_KEY_TTL = 6 * 3600
_ERRORS = {
    -400: "请求有误",
    -403: "没有权限看这个视频",
    -404: "视频不存在或已被删除",
    -352: "被 B 站风控拦下了，稍后再试",
    -412: "请求太频繁，被 B 站拦下了",
    62002: "视频不可见（可能是私密或审核中）",
    62004: "视频审核中",
    62012: "视频仅 UP 主自己可见",
}


class BiliError(Exception):
    """给人和 AI 看的错误说明；detail 只写进日志（哪个接口、什么状态），帮助排查。
    blocked 表示是被风控拦下的（可以换条路再试）。"""

    def __init__(self, message: str, detail: str = "", *, blocked: bool = False, status: int = 0) -> None:
        super().__init__(message)
        self.detail = detail
        self.blocked = blocked
        self.status = status


# 被 B 站风控（HTTP 412）拦下后，这个接口地址暂停一段时间，不再接连请求，免得越拦越久；网页那条路不受影响
BLOCK_COOLDOWN = 60.0
# 换成「不带登录信息」再试时去掉的 Cookie（读视频信息、播放地址都不需要登录）
_LOGIN_COOKIES = {"SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid"}
_INITIAL_STATE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;\s*\(function", re.S)
_PLAYINFO = re.compile(r"window\.__playinfo__\s*=\s*(\{.*?\})\s*</script>", re.S)


@dataclass
class VideoInfo:
    bvid: str
    aid: int
    cid: int
    page: int
    title: str
    part_title: str
    owner: str
    duration: int  # 这一 P 的秒数
    description: str = ""
    category: str = ""
    page_count: int = 1
    owner_mid: int = 0

    @property
    def url(self) -> str:
        return f"https://www.bilibili.com/video/{self.bvid}" + (f"?p={self.page}" if self.page > 1 else "")


@dataclass(frozen=True)
class Chapter:
    """UP 主在进度条上标的一段。"""

    start: int  # 秒
    title: str


def format_chapters(chapters: List["Chapter"], limit: int = 15) -> str:
    """「00:00 开场 / 03:12 开始做菜 / …」"""
    shown = [f"{format_seconds(c.start)} {c.title}" for c in chapters[:limit]]
    if len(chapters) > limit:
        shown.append(f"……共 {len(chapters)} 段")
    return " / ".join(shown)


@dataclass
class MediaPlan:
    """要交给视频模型的文件：一个带声音的 mp4，或者分开的画面和声音。"""

    video_url: str
    audio_url: str = ""
    size_hint: int = 0  # 画面文件的字节数，B 站给的或按码率估的，可能为 0
    audio_size_hint: int = 0


def parse_cookie(raw: str) -> Dict[str, str]:
    """WebUI 里粘贴的整段 Cookie（a=b; c=d），或者只有 SESSDATA 的值。"""
    raw = str(raw or "").strip()
    if not raw:
        return {}
    if "=" not in raw:
        return {"SESSDATA": raw}
    cookies: Dict[str, str] = {}
    for part in raw.split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key.strip():
            cookies[key.strip()] = value.strip()
    return cookies


def mixin_key(img_key: str, sub_key: str) -> str:
    raw = img_key + sub_key
    return "".join(raw[i] for i in _MIXIN_TABLE if i < len(raw))[:32]


def sign_wbi(params: Dict[str, Any], key: str, *, timestamp: Optional[int] = None) -> Dict[str, str]:
    signed = dict(params)
    signed["wts"] = int(time.time()) if timestamp is None else timestamp
    normalized = {k: "".join(ch for ch in str(v) if ch not in "!'()*") for k, v in sorted(signed.items())}
    normalized["w_rid"] = hashlib.md5((urlencode(normalized) + key).encode()).hexdigest()
    return normalized


def _fingerprint_params() -> Dict[str, str]:
    """网页播放器带的一组浏览器特征参数；没有时 playurl 更容易被风控。"""

    def noise(low: int, high: int) -> str:
        raw = "".join(random.choices(string.ascii_letters + string.digits, k=random.randint(low, high)))
        return base64.b64encode(raw.encode()).decode().rstrip("=")

    width, height = random.choice(((1920, 1080), (1536, 864), (1366, 768), (2560, 1440)))
    offset, scroll = random.randrange(114), random.randint(0, 100)
    shift = random.randrange(514)
    inter = {
        "ds": [],
        "wh": [2 * width + 2 * height + 3 * offset, 4 * width - height + offset, offset],
        "of": [3 * scroll + shift, 4 * scroll + 2 * shift, shift],
    }
    return {
        "dm_img_list": "[]",
        "dm_img_str": noise(16, 64),
        "dm_cover_img_str": noise(32, 128),
        "dm_img_inter": json.dumps(inter, separators=(",", ":")),
    }


_TIME_COLON = re.compile(r"^\s*(?:(\d{1,2}):)?(\d{1,3}):(\d{1,2})\s*$")
_TIME_CN = re.compile(r"^\s*(?:(\d+)\s*(?:小时|时))?\s*(?:(\d+)\s*分(?:钟)?)?\s*(?:(\d+)\s*秒)?\s*$")


def parse_time(text: str) -> Optional[int]:
    """「06:30」「1:02:30」「390」「6分30秒」→ 秒数；认不出返回 None。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    match = _TIME_COLON.match(raw)
    if match:
        hours, minutes, seconds = (int(x) if x else 0 for x in match.groups())
        return hours * 3600 + minutes * 60 + seconds
    match = _TIME_CN.match(raw)
    if match and any(match.groups()):
        hours, minutes, seconds = (int(x) if x else 0 for x in match.groups())
        return hours * 3600 + minutes * 60 + seconds
    return None


def line_seconds(line: str) -> Optional[int]:
    """字幕行「06:30 内容」开头的时间。"""
    head = line.split(" ", 1)[0]
    return parse_time(head) if ":" in head else None


def format_seconds(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class BiliClient:
    """所有请求都有超时；不读整个大文件进内存。"""

    API = "https://api.bilibili.com"
    COMMENT = "https://comment.bilibili.com"

    WWW = "https://www.bilibili.com"

    def __init__(
        self, session: aiohttp.ClientSession, cookie: str = "", logger: Optional[logging.Logger] = None, guard: Optional[Any] = None
    ) -> None:
        self._session = session
        self.log = logger or logging.getLogger(__name__)
        self._cookies = parse_cookie(cookie)
        # 带着她自己账号的登录信息时，请求也要过账号守卫（暂停、配额、间隔），被拦就暂停账号
        self.guard = guard
        # 没有设备标识（buvid3）的请求更容易被风控；用户粘的 Cookie 里一般有，没有就向 B 站要一个
        self._device_ready = "buvid3" in self._cookies
        self._device_lock = asyncio.Lock()
        self._wbi: Tuple[str, float] = ("", 0.0)
        self._wbi_lock = asyncio.Lock()
        self._blocked_until: Dict[str, float] = {}  # 接口地址 → 暂停到什么时候

    @property
    def has_login_cookie(self) -> bool:
        return bool(self._cookies.get("SESSDATA"))

    def _headers(self, referer: str = "https://www.bilibili.com/", *, anonymous: bool = False, html: bool = False) -> Dict[str, str]:
        cookies = {k: v for k, v in self._cookies.items() if not (anonymous and k in _LOGIN_COOKIES)}
        return {
            "User-Agent": USER_AGENT,
            "Referer": referer,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8" if html else "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
        }

    @property
    def blocked_seconds(self) -> int:
        """还要暂停多少秒（任一接口被风控拦下后）。"""
        now = time.monotonic()
        return max([0] + [int(until - now) for until in self._blocked_until.values()])

    async def _ensure_device(self) -> None:
        if self._device_ready:
            return
        async with self._device_lock:
            if self._device_ready:
                return
            self._device_ready = True
            try:
                async with self._session.get(
                    f"{self.API}/x/frontend/finger/spi", headers={"User-Agent": USER_AGENT}, timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    payload = await response.json(content_type=None)
                data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else {}
                if data.get("b_3"):
                    self._cookies["buvid3"] = str(data["b_3"])
                if data.get("b_4"):
                    self._cookies["buvid4"] = str(data["b_4"])
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                pass
            self._cookies.setdefault("buvid3", f"{uuid.uuid4()}infoc")
            self._cookies.setdefault("b_nut", str(int(time.time())))

    async def _get(self, url: str, params: Optional[Dict[str, Any]], headers: Dict[str, str], timeout: float, *, text: bool) -> Any:
        """一次请求。被风控拦下（412）时抛 blocked 的 BiliError。"""
        where = urlparse(url).path or url
        try:
            async with self._session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
                if response.status == 412:
                    raise BiliError(_ERRORS[-412], f"{where} HTTP 412", blocked=True, status=412)
                if response.status >= 400:
                    raise BiliError(f"B 站返回 HTTP {response.status}", f"{where} HTTP {response.status}", status=response.status)
                if text:
                    return await response.text(errors="replace")
                return await response.json(content_type=None)
        except asyncio.TimeoutError as exc:
            raise BiliError("连接 B 站超时", f"{where} 超时") from exc
        except aiohttp.ClientError as exc:
            raise BiliError(f"连接 B 站失败（{type(exc).__name__}）", f"{where} {type(exc).__name__}") from exc
        except ValueError as exc:
            raise BiliError("B 站返回的内容看不懂", f"{where} 不是 JSON") from exc

    async def _json(self, url: str, params: Optional[Dict[str, Any]] = None, *, referer: str = "https://www.bilibili.com/", timeout: float = 15) -> Dict[str, Any]:
        """请求一个接口。被风控拦下时，先换成不带登录信息再试一次；还被拦就暂停这个接口一会。"""
        where = urlparse(url).path or url
        remaining = int(self._blocked_until.get(where, 0) - time.monotonic())
        if remaining > 0:
            raise BiliError(f"刚被 B 站风控拦过，{remaining} 秒后再试", f"{where} 暂停中", blocked=True)
        await self._ensure_device()
        trusted = url.startswith(self.API) or url.startswith(self.WWW)
        guarded = self.guard is not None and self.has_login_cookie and trusted
        if guarded:
            try:
                wait = self.guard.before_request()
                if wait > 0:
                    await asyncio.sleep(wait)
                if self.guard.paused:
                    raise RuntimeError(self.guard.paused)
            except Exception as exc:  # 暂停中、配额用完：这次不带登录信息
                self.log.info("B站视频：她的账号暂停或用完配额（%s），这次不带登录信息", exc)
                guarded = False
        try:
            anonymous = not trusted or (self.guard is not None and not guarded)
            payload = await self._get(url, params, self._headers(referer, anonymous=anonymous), timeout, text=False)
        except BiliError as exc:
            if guarded and (reason := self.guard.pause_reason_for(http_status=exc.status)):
                self.guard.pause(reason)
            if not (exc.blocked and self.has_login_cookie):
                if exc.blocked:
                    self._blocked_until[where] = time.monotonic() + BLOCK_COOLDOWN
                raise
            try:
                payload = await self._get(url, params, self._headers(referer, anonymous=True), timeout, text=False)
            except BiliError as again:
                if again.blocked:
                    self._blocked_until[where] = time.monotonic() + BLOCK_COOLDOWN
                    again.detail += "（带登录、不带登录都被拦）"
                raise
            self.log.info("B站视频：%s 带登录信息被风控拦下，不带登录信息成功了", where)
        if not isinstance(payload, dict):
            raise BiliError("B 站返回的内容看不懂", f"{where} 不是 JSON 对象")
        code = payload.get("code")
        if guarded and isinstance(code, int) and (reason := self.guard.pause_reason_for(code=code)):
            self.guard.pause(reason)  # 带着她的登录信息却显示没登录、被风控或太频繁
        if payload.get("code") in (-352, -412):
            self._blocked_until[where] = time.monotonic() + BLOCK_COOLDOWN
        return payload

    async def _page(self, url: str) -> str:
        """打开视频网页（接口被风控时的后备，网页和接口不是同一个入口）。"""
        # 网页后备不过账号守卫，用她的账号时不带登录信息
        html = await self._get(url, None, self._headers(url, anonymous=self.guard is not None, html=True), 15, text=True)
        return str(html)[:5_000_000]

    @staticmethod
    def _data(payload: Dict[str, Any], *, allow: Tuple[int, ...] = ()) -> Dict[str, Any]:
        code = payload.get("code", 0)
        if code != 0 and code not in allow:
            message = _ERRORS.get(code, f"B 站返回错误 {code}：{str(payload.get('message', ''))[:60]}")
            raise BiliError(message, f"code={code} message={str(payload.get('message', ''))[:60]}", blocked=code in (-352, -412))
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    # ── 链接 ──────────────────────────────────────

    async def resolve(self, link: str) -> VideoRef:
        """链接或 BV 号 → VideoRef。短链接只跟随跳转头，不去打开最终页面（打开容易被拦）。"""
        ref = ref_from_text(link)
        if ref is not None:
            return ref
        if not is_short_url(link):
            raise BiliError("不是 B 站视频链接")
        url = link.strip()
        for _ in range(5):
            try:
                async with self._session.get(
                    url,
                    headers={"User-Agent": USER_AGENT},
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as response:
                    location = response.headers.get("Location", "")
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise BiliError("短链接打不开") from exc
            if not location:
                break
            url = urljoin(url, location)
            ref = parse_video_url(url)
            if ref is not None:
                return ref
            host = (urlparse(url).hostname or "").lower()
            if not (host.endswith("bilibili.com") or is_short_url(url)):
                break
        raise BiliError("这个短链接指向的不是 B 站视频")

    # ── 视频信息 ──────────────────────────────────

    async def view(self, ref: VideoRef) -> VideoInfo:
        params = {"bvid": ref.bvid} if ref.bvid else {"aid": ref.aid}
        try:
            data = self._data(await self._json(f"{self.API}/x/web-interface/view", params))
        except BiliError as exc:
            if not exc.blocked:
                raise
            data = await self._view_from_page(ref, exc)
        return _info_from_view(data, ref)

    async def _view_from_page(self, ref: VideoRef, cause: BiliError) -> Dict[str, Any]:
        try:
            state = _embedded_json(_INITIAL_STATE, await self._page(self.WWW + ref.url.removeprefix("https://www.bilibili.com")))
        except BiliError as exc:
            cause.detail += f"；网页后备也失败：{exc.detail or exc}"
            raise cause from exc
        data = state.get("videoData") if isinstance(state.get("videoData"), dict) else {}
        if not data.get("pages") and not data.get("cid"):
            cause.detail += "；网页里没有视频信息"
            raise cause
        self.log.info("B站视频：视频信息接口被风控拦下，改从网页读取成功")
        return data


    async def _wbi_key(self) -> str:
        async with self._wbi_lock:
            key, fetched_at = self._wbi
            if key and time.time() - fetched_at < _WBI_KEY_TTL:
                return key
            # 没登录时 nav 返回 -101，但仍然带着签名用的两个 key
            data = self._data(await self._json(f"{self.API}/x/web-interface/nav"), allow=(-101,))
            images = data.get("wbi_img") if isinstance(data.get("wbi_img"), dict) else {}
            img = Path(urlparse(str(images.get("img_url", ""))).path).stem
            sub = Path(urlparse(str(images.get("sub_url", ""))).path).stem
            if not img or not sub:
                raise BiliError("拿不到 B 站的签名参数")
            key = mixin_key(img, sub)
            self._wbi = (key, time.time())
            return key

    async def login_status(self) -> Tuple[bool, str]:
        data = self._data(await self._json(f"{self.API}/x/web-interface/nav"), allow=(-101,))
        return bool(data.get("isLogin")), str(data.get("uname") or "")

    # ── 播放地址 ──────────────────────────────────

    async def media_plan(self, info: VideoInfo, quality: int, *, separate: bool = False) -> MediaPlan:
        """优先要一个带声音的 mp4（不用 ffmpeg 合并），没有再用分开的画面和声音；separate=True 时反过来。
        播放地址接口被风控拦下时，改从视频网页里读（网页里也带着播放地址）。"""
        try:
            base = {"bvid": info.bvid, "cid": info.cid, "qn": quality, "fnver": 0, "fourk": 0, **_fingerprint_params()}
            key = await self._wbi_key()
            for fnval in (4048, 1) if separate else (1, 4048):
                data = self._data(
                    await self._json(f"{self.API}/x/player/wbi/playurl", sign_wbi({**base, "fnval": fnval}, key), referer=info.url)
                )
                plan = _plan_from_play(data, quality, separate=separate, duration=info.duration)
                if plan is not None:
                    return plan
        except BiliError as exc:
            if not exc.blocked:
                raise
            try:
                play = _embedded_json(_PLAYINFO, await self._page(self.WWW + info.url.removeprefix("https://www.bilibili.com")))
            except BiliError as page_exc:
                exc.detail += f"；网页后备也失败：{page_exc.detail or page_exc}"
                raise exc from page_exc
            data = play.get("data") if isinstance(play.get("data"), dict) else {}
            plan = _plan_from_play(data, quality, separate=separate, duration=info.duration)
            if plan is None:
                exc.detail += "；网页里没有可用的播放地址"
                raise exc
            self.log.info("B站视频：播放地址接口被风控拦下，改从网页读取成功")
            return plan
        raise BiliError("B 站没给可以下载的视频地址")

    # ── 字幕和弹幕 ────────────────────────────────

    async def subtitles(self, info: VideoInfo) -> Tuple[str, List[Chapter]]:
        """字幕全文（每行「分:秒 内容」）和 UP 主标的分段，都在播放器信息这一个请求里。
        没有字幕时字幕为空字符串；AI 字幕要登录才拿得到。"""
        key = await self._wbi_key()
        data = self._data(
            await self._json(f"{self.API}/x/player/wbi/v2", sign_wbi({"aid": info.aid, "cid": info.cid}, key), referer=info.url)
        )
        # 偶尔会返回别的视频的信息，对一下编号
        if (data.get("aid") and int(data["aid"]) != info.aid) or (data.get("cid") and int(data["cid"]) != info.cid):
            return "", []
        chapters = _chapters(data.get("view_points"))
        subtitle = data.get("subtitle") if isinstance(data.get("subtitle"), dict) else {}
        tracks = [t for t in subtitle.get("subtitles") or [] if isinstance(t, dict) and t.get("subtitle_url")]
        if not tracks:
            return "", chapters

        def rank(track: Dict[str, Any]) -> int:
            lan = str(track.get("lan", "")).lower()
            if lan.startswith("zh"):
                return 0
            if lan.startswith("ai-zh"):
                return 1
            return 2

        url = str(sorted(tracks, key=rank)[0]["subtitle_url"])
        if url.startswith("//"):
            url = "https:" + url
        body = (await self._json(url, referer=info.url)).get("body")
        lines = [
            f"{format_seconds(float(item.get('from') or 0))} {str(item.get('content') or '').strip()}"
            for item in body or []
            if isinstance(item, dict) and str(item.get("content") or "").strip()
        ]
        return "\n".join(lines), chapters

    async def hot_danmaku(self, cid: int, limit: int = 8) -> List[str]:
        """出现次数最多的几条弹幕。"""
        try:
            async with self._session.get(
                f"{self.COMMENT}/{cid}.xml", headers=self._headers(anonymous=self.guard is not None), timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status >= 400:
                    return []
                text = await response.text(errors="replace")
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return []
        counts: Dict[str, int] = {}
        for raw in re.findall(r"<d [^>]*>(.*?)</d>", text, flags=re.S):
            line = html.unescape(raw).strip()
            if 1 < len(line) <= 40:
                counts[line] = counts.get(line, 0) + 1
        ranked = sorted(counts.items(), key=lambda item: -item[1])  # sorted 是稳定的：同样次数按出现先后
        return [line for line, _ in ranked[:limit]]

    # ── 下载 ──────────────────────────────────────

    async def download(self, url: str, dest: Path, *, referer: str, max_bytes: int) -> int:
        """边下边写文件，超过 max_bytes 立即停下并删掉。返回字节数。超时由调用方控制。"""
        size = 0
        try:
            # 弹幕和视频文件不需要登录：用她的账号时不带登录信息，免得这些请求绕过账号守卫
            async with self._session.get(url, headers=self._headers(referer, anonymous=self.guard is not None)) as response:
                if response.status >= 400:
                    raise BiliError(f"下载视频失败：HTTP {response.status}")
                if response.content_length and response.content_length > max_bytes:
                    raise BiliError(f"视频文件太大（{response.content_length // 1048576}MB）")
                with dest.open("wb") as handle:
                    async for chunk in response.content.iter_chunked(256 * 1024):
                        size += len(chunk)
                        if size > max_bytes:
                            raise BiliError(f"视频文件超过 {max_bytes // 1048576}MB 上限")
                        handle.write(chunk)
        except BiliError:
            dest.unlink(missing_ok=True)
            raise
        except aiohttp.ClientError as exc:
            dest.unlink(missing_ok=True)
            raise BiliError(f"下载视频失败（{type(exc).__name__}）") from exc
        return size


def _chapters(raw: Any) -> List[Chapter]:
    """播放器信息里的 view_points：[{"from": 秒, "to": 秒, "content": 标题}, ...]，按时间排好。"""
    chapters: List[Chapter] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("content") or "").split())[:30]
        try:
            start = int(float(item.get("from") or 0))
        except (TypeError, ValueError):
            continue
        if title:
            chapters.append(Chapter(start, title))
    return sorted(set(chapters), key=lambda c: c.start)


def _first_url(item: Dict[str, Any]) -> str:
    for key in ("url", "baseUrl", "base_url"):
        if item.get(key):
            return str(item[key])
    backups = item.get("backupUrl") or item.get("backup_url") or []
    return str(backups[0]) if isinstance(backups, list) and backups else ""


def _plan_from_play(data: Dict[str, Any], quality: int, *, separate: bool = False, duration: int = 0) -> Optional[MediaPlan]:
    durl = [d for d in data.get("durl") or [] if isinstance(d, dict) and _first_url(d)]
    combined = MediaPlan(video_url=_first_url(durl[0]), size_hint=int(durl[0].get("size") or 0)) if len(durl) == 1 else None
    if combined is not None and not separate:
        return combined
    return _dash_plan(data, quality, duration) or combined


def _dash_plan(data: Dict[str, Any], quality: int, duration: int) -> Optional[MediaPlan]:
    dash = data.get("dash") if isinstance(data.get("dash"), dict) else {}
    videos = [v for v in dash.get("video") or [] if isinstance(v, dict) and _first_url(v)]
    audios = [a for a in dash.get("audio") or [] if isinstance(a, dict) and _first_url(a)]
    if not videos or not audios:
        return None
    # 画面：不超过要求清晰度里最高的，优先 AVC（兼容性最好），同档取码率最低的；声音：取码率最低的
    eligible = [v for v in videos if int(v.get("id") or 0) <= quality] or videos
    best_id = max(int(v.get("id") or 0) for v in eligible)
    same = [v for v in eligible if int(v.get("id") or 0) == best_id]
    video = min(same, key=lambda v: (0 if int(v.get("codecid") or 0) == 7 else 1, int(v.get("bandwidth") or 0)))
    audio = min(audios, key=lambda a: int(a.get("bandwidth") or 0))
    # 分开的文件 B 站不给大小，按码率（每秒比特数）乘时长估
    seconds = int(dash.get("duration") or duration or 0)
    return MediaPlan(
        video_url=_first_url(video),
        audio_url=_first_url(audio),
        size_hint=int(video.get("bandwidth") or 0) * seconds // 8,
        audio_size_hint=int(audio.get("bandwidth") or 0) * seconds // 8,
    )


def _embedded_json(pattern: "re.Pattern[str]", html: str) -> Dict[str, Any]:
    match = pattern.search(html)
    if not match:
        return {}
    try:
        value = json.loads(match.group(1))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _info_from_view(data: Dict[str, Any], ref: VideoRef) -> VideoInfo:
    """视频信息接口（或网页里的 videoData）→ VideoInfo。"""
    pages = [p for p in data.get("pages") or [] if isinstance(p, dict)]
    page_index = min(max(ref.page, 1), max(len(pages), 1)) - 1
    page = pages[page_index] if pages else {}
    owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
    cid = int(page.get("cid") or data.get("cid") or 0)
    if not cid:
        raise BiliError("拿不到视频的分 P 信息")
    return VideoInfo(
        bvid=str(data.get("bvid") or ref.bvid),
        aid=int(data.get("aid") or ref.aid or 0),
        cid=cid,
        page=page_index + 1,
        title=str(data.get("title") or ""),
        part_title=str(page.get("part") or ""),
        owner=str(owner.get("name") or ""),
        duration=int(page.get("duration") or data.get("duration") or 0),
        description=str(data.get("desc") or "").strip(),
        category=str(data.get("tname") or ""),
        page_count=max(len(pages), 1),
        owner_mid=int(owner.get("mid") or 0),
    )
