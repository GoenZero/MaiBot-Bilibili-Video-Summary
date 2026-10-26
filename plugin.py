"""MaiBot B 站插件：让 AI 能「看」B 站视频，也能用她自己的账号逛 B 站。

短视频交给阿里百炼的千问 Omni 或 Google Gemini 看画面和声音；长视频读字幕；都附上标题、简介和热门弹幕。
扫码登录她自己的账号后，她可以逛首页推荐、热门、搜索，看评论区（带图），点赞、投币、收藏、稍后再看、关注 UP 主，
看完留下观看记录，让推荐慢慢变成她的口味；评论区里喜欢的图可以收藏进 MaiBot 的表情包。
看不看、逛什么、做不做，全由她用工具自己决定；插件只提供眼睛和手，不替她调模型、不发评论。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional

import asyncio
import base64
import contextlib
import io
import shutil
import time

import aiohttp
from maibot_sdk import Command, HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, ToolParameterInfo, ToolParamType

from .config import PLUGIN_VERSION, BilibiliVideoConfig
from .core.account import ACTION_NAMES, FEATURES, QR_DONE, QR_EXPIRED, QR_SCANNED, Account, AccountPaused, Limits, QuotaExceeded, qr_png_base64
from .core.bilibili import BiliClient, BiliError, parse_time
from .core.browse import Browser, Comment, VideoCard
from .core.cache import ResultCache
from .core.links import find_links
from .core.gemini import GeminiClient, GeminiSettings
from .core.qwen import QwenClient, QwenSettings
from .core.watcher import WatchSettings, Watcher

WATCH_TOOL = "bilibili_watch"
WATCH_DESCRIPTION = (
    "看一个 B 站视频，拿到视频的内容描述（短视频会看画面和声音，长视频读字幕），以及标题、UP 主、简介和热门弹幕。"
    "对方发来 B 站链接（包括 b23.tv 短链接或 BV 号）、想让你看看，或者你好奇视频内容时用。"
    "B 站链接请用这个工具，不必再用网页抓取工具去抓 B 站页面。"
    "只想看其中一段时，填 start 和 end（例如 06:30 和 10:50），会给你这段时间的字幕原文；这只能读字幕，看不到这段的画面。"
    "看完像自己看过一样自然地聊，不要逐条复述描述，也不要提到工具。没看成就如实告诉对方原因，不要让对方替你去看。"
)
BROWSE_TOOL = "bilibili_browse"
COMMENTS_TOOL = "bilibili_comments"
ACTION_TOOL = "bilibili_action"
EMOJI_TOOL = "bilibili_save_emoji"
BROWSE_DESCRIPTION = (
    "用你自己的 B 站账号找视频：source 填「推荐」（你的首页推荐，会越来越像你的口味）、「热门」或「搜索」（填 keyword）。"
    "返回标题、UP 主、时长、播放量和 BV 号；想看哪个就用 bilibili_watch。空闲时间想逛就逛，不想逛就算了。"
)
COMMENTS_DESCRIPTION = (
    "看一个 B 站视频的评论区（热门或最新），每条带点赞数和两条回复，评论里的图会一起给你看（标成「图1」「图2」）。"
    "看看大家怎么聊、有什么梗；看到有意思的说法可以用 learn_expression 学下来，没见过的梗弄懂了可以用 learn_jargon 记下，"
    "喜欢的图可以用 bilibili_save_emoji 收藏成表情包。"
)
ACTION_DESCRIPTION = (
    "对一个视频做点什么（用你自己的账号）：action 填「点赞」「投币」「收藏」「稍后再看」或「关注UP主」。"
    "真喜欢再做，你点过、收藏过、关注过的会让推荐越来越像你的口味。每天有次数上限，做过的不会重复做。"
)
EMOJI_DESCRIPTION = "把刚才看评论区时看到的一张图收藏成你的表情包（image 填「图1」这样的编号）。只收藏你真觉得有意思、以后想拿来用的。"
ACTION_KEYS = {"点赞": "like", "投币": "coin", "收藏": "favorite", "稍后再看": "later", "关注UP主": "follow", "关注up主": "follow", "关注": "follow"}
# 一条消息里最多预取几个链接
PREFETCH_PER_MESSAGE = 2
# 扫码登录：二维码有效期内每隔几秒查一次
QR_POLL_SECONDS = 2.5
QR_TIMEOUT = 180
# 评论图交给她看时缩到多大；一次评论区最多编号几张图、最近一共记住几张（要大于前者）
IMAGE_MAX_SIDE = 512
IMAGE_PER_CALL = 20
IMAGE_KEEP = 60


def _collect_text(value: Any, out: List[str], depth: int = 0) -> None:
    """从消息字典里收集所有文字（链接可能在任何一段里）。"""
    if depth > 6 or len(out) > 50:
        return
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for key in ("data", "text", "processed_plain_text", "raw_message", "url"):
            if key in value:
                _collect_text(value[key], out, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _collect_text(item, out, depth + 1)


class BilibiliVideoPlugin(MaiBotPlugin):
    config_model: ClassVar[type] = BilibiliVideoConfig

    def __init__(self) -> None:
        super().__init__()
        self._session: Optional[aiohttp.ClientSession] = None
        self._watcher: Optional[Watcher] = None
        self._cache: Optional[ResultCache] = None
        self._last_error = ""
        self._account: Optional[Account] = None
        self._browser: Optional[Browser] = None
        self._images: Dict[str, str] = {}  # 「图N」→ 最近看评论区时的图片地址（编号一直往上加，同时看几个评论区也不会串）
        self._image_seq = 0
        self._action_lock = asyncio.Lock()  # 账号操作一个一个来：配额、去重、间隔才不会被同时绕过
        self._login_task: Optional[asyncio.Task[None]] = None
        self._background: set[asyncio.Task[Any]] = set()

    # ── 生命周期 ──────────────────────────────────

    async def on_load(self) -> None:
        data_dir = Path(self.ctx.paths.data_dir)
        shutil.rmtree(data_dir / "tmp", ignore_errors=True)  # 上次没清掉的临时文件
        self._cache = ResultCache(data_dir / "cache.json")
        self._account = Account(data_dir / "account", self._limits())
        self._account.on_pause = self._drop_login_from_watcher
        await self._build()
        self.ctx.logger.info("B站视频插件已加载（v%s）", PLUGIN_VERSION)

    async def on_unload(self) -> None:
        task, self._login_task = self._login_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._close()

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        del scope, config_data, version
        if self._account is not None:
            self._account.limits = self._limits()
        await self._close()
        await self._build()
        self.ctx.logger.info("B站视频插件配置已生效")

    async def _build(self) -> None:
        cfg = self.config
        # trust_env：沿用 MaiBot 容器里设置的 HTTP(S)_PROXY
        # 不用会话自带的 Cookie 罐：B 站的 Cookie 只按插件里填的发，「不带登录信息再试」时才能真的不带
        self._session = aiohttp.ClientSession(
            trust_env=True,
            cookie_jar=aiohttp.DummyCookieJar(),
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=60),
        )
        # 扫过码就用她自己账号的登录信息（看 AI 字幕也用它），没扫过才用配置里粘贴的 Cookie
        account = self._account
        if account is not None and account.logged_in:
            # 她的账号暂停中就完全不带登录信息（也不退回配置里的旧 Cookie）
            cookie = "" if account.paused else "; ".join(f"{k}={v}" for k, v in account.cookies().items())
            bili = BiliClient(self._session, cookie, logger=self.ctx.logger, guard=account)
        else:
            bili = BiliClient(self._session, cfg.bilibili.cookie, logger=self.ctx.logger)
        if self._account is not None:
            self._browser = Browser(self._session, bili, self._account, logger=self.ctx.logger)
        viewer: Optional[Any] = None
        if cfg.qwen.provider == "Gemini":
            if cfg.gemini.api_key.strip():
                viewer = GeminiClient(
                    self._session,
                    GeminiSettings(
                        api_key=cfg.gemini.api_key.strip(),
                        model=cfg.gemini.model.strip() or "gemini-3.5-flash",
                        fps=cfg.qwen.fps,
                        resolution=cfg.gemini.resolution,
                        upload_timeout=float(cfg.watch.upload_timeout),
                        model_timeout=float(cfg.watch.model_timeout),
                    ),
                    logger=self.ctx.logger,
                )
        elif cfg.qwen.api_key.strip():
            viewer = QwenClient(
                self._session,
                QwenSettings(
                    api_key=cfg.qwen.api_key.strip(),
                    region=cfg.qwen.region,
                    model=cfg.qwen.model.strip() or "qwen3.5-omni-plus",
                    fps=cfg.qwen.fps,
                    upload_timeout=float(cfg.watch.upload_timeout),
                    model_timeout=float(cfg.watch.model_timeout),
                ),
                logger=self.ctx.logger,
            )
        settings = WatchSettings(
            short_seconds=cfg.watch.short_minutes * 60,
            quality=32 if cfg.bilibili.quality == "480P" else 16,
            max_mb=cfg.watch.max_mb,
            download_timeout=float(cfg.watch.download_timeout),
            max_chars=cfg.watch.max_chars,
            danmaku=cfg.watch.danmaku,
        )
        assert self._cache is not None
        self._watcher = Watcher(
            bili,
            viewer,
            self._cache,
            settings,
            Path(self.ctx.paths.data_dir) / "tmp",
            summarize=self._summarize,
            logger=self.ctx.logger,
        )

    def _drop_login_from_watcher(self) -> None:
        """账号被暂停时，看视频那边也别再带她的登录信息（主人 /bili_resume 后重建时恢复）。"""
        watcher = self._watcher
        if watcher is not None:
            for name in ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid"):
                watcher.bili._cookies.pop(name, None)

    def _limits(self) -> Limits:
        cfg = self.config.account
        limits = Limits(requests_per_day=cfg.requests_per_day)
        limits.per_day.update(like=cfg.likes_per_day, coin=cfg.coins_per_day, favorite=cfg.favorites_per_day, follow=cfg.follows_per_day)
        return limits

    async def _close(self) -> None:
        self._browser = None
        for task in list(self._background):
            task.cancel()
        watcher, self._watcher = self._watcher, None
        if watcher is not None:
            watcher.close()
        session, self._session = self._session, None
        if session is not None:
            await session.close()

    async def _summarize(self, subtitles: str, limit: int, chapters: str) -> str:
        if chapters:
            how = (
                f"UP 主把视频分成了这些段：{chapters}\n"
                "请按这些分段逐段概括，每段一行，行首写这一段的开始时间和段名，例如「03:12 开始做菜：……」。"
            )
        else:
            how = "视频没有分段。请按时间顺序把内容分成几段概括，每段一行，行首写这一段的开始时间，例如「03:12 ……」。"
        prompt = (
            f"下面是一个 B 站视频的字幕，每行开头是时间。{how}"
            f"保留关键细节（人名、数字、结论），总共 {limit} 字以内。只根据字幕内容，不要编造，不要加开场白。\n\n"
            "字幕：\n" + subtitles[:30000]
        )
        result = await self.ctx.llm.generate(prompt, task_name=self.config.watch.summary_task.strip() or "utils")
        if not result.get("success", True) and not result.get("response"):
            raise RuntimeError(str(result.get("error") or "模型没有返回"))
        return str(result.get("response") or "")

    # ── 收到链接先看起来 ──────────────────────────

    @HookHandler(
        "chat.receive.before_process",
        name="bilibili_prefetch",
        description="收到 B 站链接时先在后台开始看（只看，不改消息）",
        mode=HookMode.OBSERVE,
        timeout_ms=2000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def on_inbound(self, **kwargs: Any) -> None:
        watcher = self._watcher
        if watcher is None or not self.config.plugin.enabled or not self.config.watch.prefetch:
            return None
        texts: List[str] = []
        _collect_text(kwargs.get("message"), texts)
        links = find_links("\n".join(texts))[:PREFETCH_PER_MESSAGE]
        for link in links:
            watcher.prefetch(link)
        if links:
            self.ctx.logger.info("B站视频：收到链接，先在后台看（%d 个）", len(links))
        return None

    # ── 给 AI 的工具 ──────────────────────────────

    @Tool(
        WATCH_TOOL,
        description=WATCH_DESCRIPTION,
        parameters=[
            ToolParameterInfo(name="url", param_type=ToolParamType.STRING, description="B 站视频链接、b23.tv 短链接或 BV 号"),
            ToolParameterInfo(
                name="start", param_type=ToolParamType.STRING, description="只看一段时：开始时间，例如 06:30；看整个视频时不填", required=False
            ),
            ToolParameterInfo(
                name="end", param_type=ToolParamType.STRING, description="只看一段时：结束时间，例如 10:50；不填就看到开始时间之后 5 分钟", required=False
            ),
        ],
        timeout_ms=1_200_000,
    )
    async def tool_watch(self, url: str = "", start: str = "", end: str = "", **kwargs: Any) -> Dict[str, Any]:
        del kwargs
        watcher = self._watcher
        if watcher is None or not self.config.plugin.enabled:
            return {"success": False, "error": "看视频的功能现在关着"}
        links = find_links(str(url or "")) or [str(url or "").strip()]
        segment: Optional[tuple[int, int]] = None
        if str(start or "").strip() or str(end or "").strip():
            begin = parse_time(start) if str(start or "").strip() else 0
            finish = parse_time(end) if str(end or "").strip() else (begin + 300 if begin is not None else None)
            if begin is None or finish is None:
                return {"success": False, "error": "start / end 的时间格式认不出，请写成 06:30 这样"}
            segment = (begin, finish)
        try:
            content = await (watcher.segment(links[0], *segment) if segment else watcher.watch(links[0]))
            if not segment:
                self._spawn(self._report_history(links[0]))
        except BiliError as exc:
            self._last_error = str(exc)
            self.ctx.logger.warning("B站视频：没看成（%s）：%s %s", links[0], exc, exc.detail)
            return {"success": False, "error": f"这个视频没看成：{exc}"}
        except Exception as exc:  # noqa: BLE001 - 任何意外都如实告诉 AI，而不是让工具调用报错
            self.ctx.logger.exception("B站视频：看视频时出错")
            self._last_error = type(exc).__name__
            return {"success": False, "error": "这个视频没看成：插件出了点问题"}
        return {"success": True, "content": content}

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _report_history(self, link: str) -> None:
        """看完一个视频，给她的账号留下观看记录（让推荐慢慢变成她的口味）。失败只记日志。"""
        browser, watcher = self._browser, self._watcher
        if browser is None or watcher is None or not self.config.account.history or not browser.account.logged_in:
            return
        try:
            info = await browser.view(await watcher.bili.resolve(link))
            async with self._action_lock:
                await asyncio.sleep(browser.account.check_action("history", info.bvid))
                await browser.report_history(info.aid, info.cid, max(1, info.duration), info.bvid)
                browser.account.record_action("history", info.bvid)
        except (QuotaExceeded, AccountPaused) as exc:
            self.ctx.logger.info("B站：没留观看记录（%s）", exc)
        except BiliError as exc:
            self.ctx.logger.warning("B站：留观看记录失败：%s %s", exc, exc.detail)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            self.ctx.logger.exception("B站：留观看记录时出错")

    # ── 用她自己的账号逛 ──────────────────────────

    def _usable(self) -> Optional[str]:
        if not self.config.plugin.enabled or self._browser is None:
            return "B 站的功能现在关着"
        return None

    def _keep(self, card: VideoCard, *, limit_length: bool) -> bool:
        cfg = self.config.browse
        if card.owner in cfg.blocked_ups:
            return False
        if limit_length and (card.duration > cfg.max_minutes * 60 or any(word and word in card.title for word in cfg.blocked_keywords)):
            return False
        return True

    @Tool(
        BROWSE_TOOL,
        description=BROWSE_DESCRIPTION,
        parameters=[
            ToolParameterInfo(name="source", param_type=ToolParamType.STRING, description="推荐、热门 或 搜索"),
            ToolParameterInfo(name="keyword", param_type=ToolParamType.STRING, description="搜索时填关键词", required=False),
            ToolParameterInfo(name="page", param_type=ToolParamType.INTEGER, description="搜索结果第几页，默认 1", required=False),
        ],
        chat_scope="private",
    )
    async def tool_browse(self, source: str = "推荐", keyword: str = "", page: Any = 1, **kwargs: Any) -> Dict[str, Any]:
        del kwargs
        if error := self._usable():
            return {"success": False, "error": error}
        assert self._browser is not None
        count = self.config.browse.list_count
        where = str(source or "推荐").strip()
        try:
            if where == "搜索":
                if not str(keyword or "").strip():
                    return {"success": False, "error": "搜索要填 keyword"}
                try:
                    page_number = int(page or 1)
                except (TypeError, ValueError):
                    page_number = 1
                cards = [c for c in await self._browser.search(str(keyword).strip(), page_number) if self._keep(c, limit_length=False)]
                title = f"搜「{str(keyword).strip()}」"
            elif where == "热门":
                cards = [c for c in await self._browser.popular(count + 8) if self._keep(c, limit_length=True)]
                title = "热门"
            else:
                cards = [c for c in await self._browser.feed(count + 4) if self._keep(c, limit_length=True)]
                title = "你的首页推荐" if self._browser.account.logged_in else "首页推荐（还没登录，是大众推荐）"
        except (AccountPaused, QuotaExceeded) as exc:
            return {"success": False, "error": str(exc)}
        except BiliError as exc:
            self.ctx.logger.warning("B站：%s 失败：%s %s", where, exc, exc.detail)
            return {"success": False, "error": f"没刷出来：{exc}"}
        cards = cards[:count]
        if not cards:
            return {"success": True, "content": f"{title}：这次什么都没刷到。"}
        lines = [c.line(i) for i, c in enumerate(cards, start=1)]
        return {"success": True, "content": f"{title}：\n" + "\n".join(lines)}

    async def _video(self, video: str) -> Any:
        """视频信息：短链接只跟随跳转（不带登录信息），查信息走她账号的守卫。"""
        assert self._watcher is not None and self._browser is not None
        links = find_links(str(video or "")) or [str(video or "").strip()]
        return await self._browser.view(await self._watcher.bili.resolve(links[0]))

    @Tool(
        COMMENTS_TOOL,
        description=COMMENTS_DESCRIPTION,
        parameters=[
            ToolParameterInfo(name="video", param_type=ToolParamType.STRING, description="BV 号或视频链接"),
            ToolParameterInfo(name="sort", param_type=ToolParamType.STRING, description="热门（默认）或 最新", required=False),
        ],
        chat_scope="private",
    )
    async def tool_comments(self, video: str = "", sort: str = "热门", **kwargs: Any) -> Dict[str, Any]:
        del kwargs
        if error := self._usable():
            return {"success": False, "error": error}
        assert self._browser is not None
        if reason := self._browser.account.blocked("comments"):
            return {"success": False, "error": f"B 站现在不让你的账号看评论区（{reason}），先别用了"}
        try:
            info = await self._video(video)
            comments = await self._browser.comments(info.aid, info.bvid, newest=str(sort or "").strip() == "最新", count=self.config.browse.comment_count)
        except (AccountPaused, QuotaExceeded) as exc:
            return {"success": False, "error": str(exc)}
        except BiliError as exc:
            self.ctx.logger.warning("B站：看评论区失败：%s %s", exc, exc.detail)
            if exc.code == -403:
                self._block("comments", str(exc))
                return {"success": False, "error": f"B 站不让你的账号看评论区（{exc}），先别用了"}
            return {"success": False, "error": f"评论区没打开：{exc}"}
        if not comments:
            return {"success": True, "content": f"《{info.title}》的评论区还没人说话。"}
        images: Dict[str, str] = {}
        lines = [f"《{info.title}》的评论区（{'最新' if str(sort or '').strip() == '最新' else '热门'}）："]
        for i, comment in enumerate(comments, start=1):
            lines.append(f"{i}. {self._comment_line(comment, images)}")
            for reply in comment.replies:
                lines.append(f"   ↳ {self._comment_line(reply, images)}")
        self._images.update(images)
        for old in list(self._images)[:-IMAGE_KEEP]:  # 只记最近这些张，单次评论区编号的图不会被挤掉
            del self._images[old]
        items = await self._image_items(images)
        if images:
            lines.append("（图在下面；想收藏成表情包就用 bilibili_save_emoji，填图的编号）")
        return {"success": True, "content": "\n".join(lines), "content_items": items}

    def _comment_line(self, comment: Comment, images: Dict[str, str]) -> str:
        marks = []
        for url in comment.pictures:
            if len(images) >= IMAGE_PER_CALL:
                marks.append("[图]")
                continue
            self._image_seq += 1
            name = f"图{self._image_seq}"
            images[name] = url
            marks.append(f"[{name}]")
        likes = f"（👍{comment.likes}）" if comment.likes else ""
        return f"{comment.user}{likes}：{comment.text}{' ' + ' '.join(marks) if marks else ''}"

    async def _image_items(self, images: Dict[str, str]) -> List[Dict[str, Any]]:
        """把这次评论区的图缩小后交给她看（最多 comment_images 张）。"""
        assert self._browser is not None
        items: List[Dict[str, Any]] = []
        for name, url in list(images.items())[: self.config.browse.comment_images]:
            try:
                raw = await self._browser.download_image(url)
                items.append({"type": "image", "data": _thumbnail(raw), "mime_type": "image/jpeg", "name": name})
            except Exception as exc:  # noqa: BLE001 - 一张图看不了不影响别的
                self.ctx.logger.info("B站：评论图 %s 没下载成（%s）", name, exc)
        return items

    @Tool(
        ACTION_TOOL,
        description=ACTION_DESCRIPTION,
        parameters=[
            ToolParameterInfo(name="video", param_type=ToolParamType.STRING, description="BV 号或视频链接"),
            ToolParameterInfo(name="action", param_type=ToolParamType.STRING, description="点赞、投币、收藏、稍后再看 或 关注UP主"),
        ],
        chat_scope="private",
    )
    async def tool_action(self, video: str = "", action: str = "", **kwargs: Any) -> Dict[str, Any]:
        del kwargs
        if error := self._usable():
            return {"success": False, "error": error}
        assert self._browser is not None
        key = ACTION_KEYS.get(str(action or "").strip().replace(" ", ""))
        if key is None:
            return {"success": False, "error": "action 只能是 点赞、投币、收藏、稍后再看、关注UP主"}
        if not getattr(self.config.account, key):
            return {"success": False, "error": f"{ACTION_NAMES[key]}没开放"}
        if not self._browser.account.logged_in:
            return {"success": False, "error": "还没登录你自己的 B 站账号"}
        if reason := self._browser.account.blocked("actions"):
            return {"success": False, "error": f"B 站现在不让你的账号点赞、投币、收藏这些（{reason}），先别用了"}
        try:
            info = await self._video(video)
            target = str(info.bvid) if key != "follow" else ""
            if key == "follow":
                target = str(info.owner_mid or await self._owner_mid(info.bvid))
            async with self._action_lock:
                await self._act(key, info, target)
        except (AccountPaused, QuotaExceeded) as exc:
            return {"success": False, "error": str(exc)}
        except BiliError as exc:
            self.ctx.logger.warning("B站：%s失败：%s %s", ACTION_NAMES[key], exc, exc.detail)
            if exc.code == -403:
                self._block("actions", str(exc))
                return {"success": False, "error": f"B 站不让你的账号做这些操作（{exc}），先别用了"}
            return {"success": False, "error": f"{ACTION_NAMES[key]}没成功：{exc}"}
        self.ctx.logger.info("B站：她%s了 %s", ACTION_NAMES[key], info.bvid)
        what = f"UP 主「{info.owner}」" if key == "follow" else f"《{info.title}》"
        return {"success": True, "content": f"已经{ACTION_NAMES[key]}：{what}。"}

    async def _act(self, key: str, info: Any, target: str) -> None:
        """在操作锁里：查配额和去重、等间隔、做、记下。"""
        assert self._browser is not None
        account = self._browser.account
        await asyncio.sleep(account.check_action(key, target))
        if account.paused:
            raise AccountPaused(f"B 站那边暂停了：{account.paused}。等主人处理后再试")
        if key == "like":
            await self._browser.like(info.aid, info.bvid)
        elif key == "coin":
            await self._browser.coin(info.aid, info.bvid)
        elif key == "favorite":
            await self._browser.favorite(info.aid, info.bvid)
        elif key == "later":
            await self._browser.later(info.aid, info.bvid)
        else:
            await self._browser.follow(int(target), info.bvid)
        account.record_action(key, target)

    def _block(self, feature: str, reason: str) -> None:
        assert self._browser is not None
        self._browser.account.block(feature, reason)
        self.ctx.logger.warning("B站：B 站不让她的账号%s（%s），先停用，主人 /bili_resume 后再试", FEATURES[feature], reason)

    async def _owner_mid(self, bvid: str) -> int:
        assert self._browser is not None
        data = await self._browser._call("GET", "/x/web-interface/view", params={"bvid": bvid})
        owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
        mid = int(owner.get("mid") or 0)
        if not mid:
            raise BiliError("找不到这个视频的 UP 主")
        return mid

    @Tool(
        EMOJI_TOOL,
        description=EMOJI_DESCRIPTION,
        parameters=[ToolParameterInfo(name="image", param_type=ToolParamType.STRING, description="图的编号，例如 图1")],
        chat_scope="private",
    )
    async def tool_save_emoji(self, image: str = "", **kwargs: Any) -> Dict[str, Any]:
        del kwargs
        if error := self._usable():
            return {"success": False, "error": error}
        assert self._browser is not None
        name = str(image or "").strip().replace("[", "").replace("]", "")
        if name.isdigit():
            name = f"图{name}"
        url = self._images.get(name)
        if not url:
            return {"success": False, "error": "没有这张图（只能收藏刚才看评论区时看到的图）"}
        try:
            raw = await self._browser.download_image(url)
            result = await self.ctx.emoji.register_emoji(base64.b64encode(raw).decode())
        except BiliError as exc:
            return {"success": False, "error": f"图没下载下来：{exc}"}
        if isinstance(result, dict) and result.get("success") is False:
            return {"success": False, "error": str(result.get("message") or result.get("error") or "没收藏成")}
        self.ctx.logger.info("B站：她把评论区的%s收藏成了表情包", name)
        description = result.get("description") if isinstance(result, dict) else ""
        return {"success": True, "content": "收藏好了" + (f"（MaiBot 给它的描述：{description}）" if description else "") + "，以后聊天可以用。"}

    # ── 扫码登录 ──────────────────────────────────

    @Command("bili_login", description="扫码登录她自己的 B 站账号", pattern=r"^/bili_login$")
    async def handle_login(self, stream_id: str = "", **kwargs: Any) -> tuple:
        del kwargs
        browser = self._browser
        if browser is None:
            await self.ctx.send.text("B 站的功能现在关着。", stream_id, storage_message=False)
            return True, "未启用", True
        if self._login_task is not None and not self._login_task.done():
            await self.ctx.send.text("上一个登录二维码还在等扫码。", stream_id, storage_message=False)
            return True, "登录进行中", True
        try:
            qr = await browser.qr_start()
            picture = qr_png_base64(qr["url"])
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("B站：生成登录二维码失败：%s", exc)
            await self.ctx.send.text(f"登录二维码没生成出来：{exc}", stream_id, storage_message=False)
            return True, "二维码失败", True
        # 二维码和提示都不存进聊天记录：不然会被当成聊天内容、被识图写进记忆
        await self.ctx.send.image(picture, stream_id, storage_message=False)
        await self.ctx.send.text("用 B 站 App 扫这个码，登录的是她自己的账号（3 分钟内有效）。", stream_id, storage_message=False)
        self._login_task = asyncio.create_task(self._wait_login(qr["key"], stream_id))
        return True, "已发送登录二维码", True

    async def _wait_login(self, key: str, stream_id: str) -> None:
        deadline = time.monotonic() + QR_TIMEOUT
        told_scanned = False
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(QR_POLL_SECONDS)
                browser = self._browser  # 配置更新会重建会话，每次都取当前的
                if browser is None:
                    continue
                try:
                    code = await browser.qr_poll(key)
                except (aiohttp.ClientError, RuntimeError, asyncio.TimeoutError):
                    continue  # 会话刚被重建，下一轮用新的再查
                if code == QR_DONE:
                    await self._close()
                    await self._build()
                    me = await self._browser.me() if self._browser is not None else {}
                    if self._browser is not None and me.get("mid"):
                        self._browser.account.update_profile(me["mid"], me["uname"])
                    self.ctx.logger.info("B站：她的账号登录好了（%s）", me.get("uname"))
                    await self.ctx.send.text(f"登录好了：{me.get('uname') or '她的账号'}。", stream_id, storage_message=False)
                    return
                if code == QR_SCANNED and not told_scanned:
                    told_scanned = True
                    await self.ctx.send.text("扫到了，在手机上点一下确认登录。", stream_id, storage_message=False)
                if code == QR_EXPIRED:
                    break
            await self.ctx.send.text("二维码过期了，要登录就再发一次 /bili_login。", stream_id, storage_message=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("B站：等扫码时出错：%s", exc)
            await self.ctx.send.text(f"登录没完成：{exc}", stream_id, storage_message=False)

    @Command("bili_resume", description="B 站被暂停后，确认没问题了恢复", pattern=r"^/bili_resume$")
    async def handle_resume(self, stream_id: str = "", **kwargs: Any) -> tuple:
        del kwargs
        account = self._account
        if account is not None and not account.paused and any(account.blocked(f) for f in FEATURES):
            account.resume()
            text = "好，让她再试试评论区和点赞这些。"
            self.ctx.logger.info("B站：主人让她再试被 B 站拒绝过的功能")
        elif account is None or not account.paused:
            text = "B 站没有暂停。"
        else:
            reason = account.paused
            account.resume()
            await self._close()
            await self._build()
            text = f"已恢复（之前暂停的原因：{reason}）。"
            self.ctx.logger.info("B站：主人恢复了她的账号操作（之前：%s）", reason)
        await self.ctx.send.text(text, stream_id, storage_message=False)
        return True, "已处理", True

    # ── 状态命令 ──────────────────────────────────

    @Command("bili_status", description="查看 B 站插件的状态", pattern=r"^/bili_status$")
    async def handle_status(self, stream_id: str = "", **kwargs: Any) -> tuple:
        del kwargs
        text = await self.status_text()
        # 不存进聊天记录：否则 MaiBot 重启后会把状态当成 AI 说过的话
        await self.ctx.send.text(text, stream_id, storage_message=False)
        return True, "状态已显示", True

    async def status_text(self) -> str:
        cfg = self.config
        lines = [f"📺 B站 v{PLUGIN_VERSION}", "状态：" + ("已启用" if cfg.plugin.enabled else "已关闭")]
        watcher = self._watcher
        account = self._account
        if account is not None and account.logged_in and self._browser is not None:
            try:
                me = await self._browser.me()
                lines.append(f"她的账号：✅ {me['uname']}（硬币 {me.get('coins')}）" if me["login"] else "她的账号：❌ 登录失效，请发 /bili_login 重新扫码")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"她的账号：查不到（{exc}）")
            day = account.today()
            done = "，".join(f"{ACTION_NAMES[k]} {v}" for k, v in (day.get("actions") or {}).items() if v)
            lines.append(f"今天：请求 {day.get('requests', 0)} / {account.limits.requests_per_day}" + (f"；{done}" if done else ""))
        elif account is not None:
            lines.append("她的账号：未登录（发 /bili_login 扫码）")
        if account is not None and account.paused:
            lines.append(f"⛔ 账号操作已暂停：{account.paused}（确认没问题后发 /bili_resume）")
        if account is not None:
            for feature, name in FEATURES.items():
                if reason := account.blocked(feature):
                    lines.append(f"🚫 B 站不让她的账号{name}：{reason}（发 /bili_resume 让她再试）")
        if watcher is not None and watcher.bili.has_login_cookie and not (account is not None and account.logged_in):
            try:
                logged_in, name = await watcher.bili.login_status()
                lines.append(f"B 站登录：✅ {name}" if logged_in else "B 站登录：❌ Cookie 已失效，请重新粘贴")
            except BiliError as exc:
                lines.append(f"B 站登录：查不到（{exc}）")
        elif not (account is not None and account.logged_in):
            lines.append("B 站登录：未填写（读不到 AI 字幕）")
        if cfg.qwen.provider == "Gemini":
            model_line = f"Gemini {cfg.gemini.model}（画面细节：{cfg.gemini.resolution}）" if cfg.gemini.api_key.strip() else "未填写 Gemini API Key（只读字幕和简介）"
        else:
            region = "北京" if cfg.qwen.region == "beijing" else "新加坡"
            model_line = f"百炼 {cfg.qwen.model}（{region}）" if cfg.qwen.api_key.strip() else "未填写百炼 API Key（只读字幕和简介）"
        lines.append(f"看画面的模型：{model_line}")
        lines.append(f"看画面的时长上限：{cfg.watch.short_minutes} 分钟")
        if watcher is not None and watcher.last_download:
            lines.append(f"最近一次下载视频：{watcher.last_download}")
        if watcher is not None and watcher.viewer is not None and watcher.viewer.last_upload:
            lines.append(f"最近一次把视频发给{watcher.viewer.name}：{watcher.viewer.last_upload}")
        lines.append(f"已缓存：{len(self._cache) if self._cache else 0} 条")
        if watcher is not None and watcher.bili.blocked_seconds:
            lines.append(f"⚠️ 刚被 B 站风控拦过，{watcher.bili.blocked_seconds} 秒内暂停请求")
        if self._last_error:
            lines.append(f"最近一次失败：{self._last_error}")
        return "\n".join(lines)


def _thumbnail(raw: bytes) -> str:
    """缩到最长边 IMAGE_MAX_SIDE 的 JPEG（base64），动图取第一帧。"""
    from PIL import Image

    with Image.open(io.BytesIO(raw)) as image:
        image.seek(0)
        frame = image.convert("RGB")
        frame.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
        buffer = io.BytesIO()
        frame.save(buffer, format="JPEG", quality=85)
    return base64.b64encode(buffer.getvalue()).decode()


def create_plugin() -> BilibiliVideoPlugin:
    """MaiBot 插件工厂函数。"""
    return BilibiliVideoPlugin()
