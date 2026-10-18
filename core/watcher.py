"""「看」一个 B 站视频：整理成一段交给 AI 的文字。

- 短视频（默认 5 分钟以内）且填了百炼 API Key：下载低清晰度版本，交给千问 Omni 看画面和声音。
- 长视频，或者看画面失败：读字幕；字幕太长时用 MaiBot 里配置的文本模型压缩。
- 都附上标题、UP 主、简介和热门弹幕。

同一个链接同时只处理一次（收到链接时的预取和 AI 调工具会合并成一次）；看过的结果缓存起来。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional

import asyncio
import logging
import shutil
import tempfile

from .bilibili import BiliClient, BiliError, Chapter, VideoInfo, format_chapters, format_seconds, line_seconds
from .links import VideoRef
from .cache import ResultCache
from .qwen import QwenClient, QwenError, build_video_prompt

# (字幕全文, 字数上限, 分段说明) → 摘要；没有分段时分段说明为空字符串
Summarizer = Callable[[str, int, str], Awaitable[str]]


@dataclass
class WatchSettings:
    short_seconds: int = 300
    quality: int = 16  # 16=360P，32=480P
    max_mb: int = 60
    download_timeout: float = 120.0
    max_chars: int = 2000
    danmaku: bool = True


class Watcher:
    def __init__(
        self,
        bili: BiliClient,
        qwen: Optional[QwenClient],
        cache: ResultCache,
        settings: WatchSettings,
        temp_root: Path,
        summarize: Optional[Summarizer] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.bili = bili
        self.qwen = qwen
        self.cache = cache
        self.settings = settings
        self.temp_root = temp_root
        self.summarize = summarize
        self.log = logger or logging.getLogger(__name__)
        self._tasks: Dict[str, "asyncio.Task[str]"] = {}
        # 同一个视频（短链接和完整链接可能指向同一个）同时只处理一次
        self._by_video: Dict[str, "asyncio.Task[str]"] = {}
        self._heavy = asyncio.Semaphore(1)  # 树莓派上同时只下载、上传一个视频

    async def watch(self, link: str) -> str:
        """返回交给 AI 的文字。链接不对、视频不存在时抛 BiliError。"""
        key = link.strip()
        task = self._tasks.get(key)
        if task is None:
            task = asyncio.create_task(self._work(key))
            self._tasks[key] = task
            task.add_done_callback(lambda _t, k=key: self._tasks.pop(k, None))
        # 调用方超时或被取消时，后台照样看完并缓存，下次直接用
        return await asyncio.shield(task)

    def close(self) -> None:
        """插件卸载或改配置时，停掉还在进行的下载和请求。"""
        for task in list(self._tasks.values()) + list(self._by_video.values()):
            task.cancel()

    def prefetch(self, link: str) -> None:
        """收到链接时先在后台看起来；出错只记日志。"""
        task = asyncio.ensure_future(self.watch(link))

        def done(t: "asyncio.Future[str]") -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                self.log.warning("B站视频：后台预看没成功（%s）：%s %s", link, exc, getattr(exc, "detail", ""))

        task.add_done_callback(done)

    async def _work(self, link: str) -> str:
        ref = await self.bili.resolve(link)
        cached = self.cache.get(ref.key)
        if cached is not None:
            return cached
        task = self._by_video.get(ref.key)
        if task is None:
            task = asyncio.create_task(self._watch_ref(ref))
            self._by_video[ref.key] = task
            task.add_done_callback(lambda _t, k=ref.key: self._by_video.pop(k, None))
        return await asyncio.shield(task)

    async def segment(self, link: str, start: int, end: int) -> str:
        """只读某一段时间的字幕（原文，太长才压缩）。读不到时抛 BiliError，说明原因。"""
        ref = await self.bili.resolve(link)
        info = await self.bili.view(ref)
        end = min(end, info.duration) if info.duration else end
        if start >= end:
            raise BiliError(f"时间段不对：视频总长 {format_seconds(info.duration)}")
        subtitles = await self._full_subtitles(ref, info)
        if not subtitles:
            hint = "" if self.bili.has_login_cookie else "（B 站的 AI 字幕需要在插件里填登录 Cookie）"
            raise BiliError(f"这个视频没有可用的字幕，没法单独看某一段{hint}")
        lines = []
        for line in subtitles.splitlines():
            at = line_seconds(line)
            if at is not None and start <= at < end:
                lines.append(line)
        span = f"{format_seconds(start)}–{format_seconds(end)}"
        if not lines:
            raise BiliError(f"{span} 这段没有字幕（视频总长 {format_seconds(info.duration)}）")
        body = "\n".join(lines)
        limit = int(self.settings.max_chars * 0.85)
        note = ""
        if len(body) > limit:
            summary = ""
            if self.summarize is not None:
                try:
                    summary = (await self.summarize(body, limit, "")).strip()
                except Exception as exc:  # noqa: BLE001
                    self.log.warning("B站视频：字幕压缩失败：%s", exc)
            if summary:
                body, note = summary, "（这段字幕太长，下面是按时间压缩后的内容）"
            else:
                body, note = body[:limit], "（这段字幕太长，只保留了开头部分）"
        return f"【B站视频】《{info.title}》{span} 这一段的字幕{note}，行首是视频里的时间：\n{body}"

    async def _full_subtitles(self, ref: VideoRef, info: VideoInfo) -> str:
        """完整字幕，读过就缓存，追问别的段落时不用再向 B 站要。"""
        key = f"{ref.key}:subtitles"
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        subtitles, _ = await self.bili.subtitles(info)
        if subtitles:
            self.cache.put(key, subtitles)
        return subtitles

    async def _watch_ref(self, ref: VideoRef) -> str:
        info = await self.bili.view(ref)
        notes: List[str] = []
        danmaku = await self.bili.hot_danmaku(info.cid) if self.settings.danmaku else []

        video_text: Optional[str] = None
        if self.qwen is not None and info.duration <= self.settings.short_seconds:
            try:
                video_text = await self._watch_video(info)
            except (BiliError, QwenError) as exc:
                self.log.warning("B站视频：没能看画面（%s）：%s %s", info.bvid, exc, getattr(exc, "detail", ""))
                notes.append(f"这次没能看到画面：{exc}")
        elif self.qwen is None:
            notes.append("没有配置看视频的模型，只读了字幕和简介")
        else:
            notes.append(f"视频较长（{format_seconds(info.duration)}），没有看画面，只读了字幕")

        subtitle_text, chapters = "", []
        if video_text is None:
            subtitle_text, chapters = await self._subtitle_text(ref, info, notes)

        text = compose(info, video_text, subtitle_text, danmaku, notes, self.settings.max_chars, chapters)
        if video_text or subtitle_text:
            self.cache.put(ref.key, text)  # 只缓存真正看到内容的结果，失败了下次还能重试
        return text

    async def _watch_video(self, info: VideoInfo) -> str:
        assert self.qwen is not None
        cfg = self.settings
        async with self._heavy:
            plan = await self.bili.media_plan(info, cfg.quality)
            if plan.size_hint and plan.size_hint > cfg.max_mb * 1048576:
                raise BiliError(f"视频文件太大（约 {plan.size_hint // 1048576}MB）")
            self.temp_root.mkdir(parents=True, exist_ok=True)
            workdir = Path(tempfile.mkdtemp(prefix="bili-", dir=self.temp_root))
            try:
                video_path = workdir / f"{info.bvid}.mp4"
                audio_path = workdir / f"{info.bvid}.m4a" if plan.audio_url else None
                try:
                    await asyncio.wait_for(self._download_all(plan.video_url, plan.audio_url, video_path, audio_path, info.url), cfg.download_timeout)
                except asyncio.TimeoutError as exc:
                    raise BiliError(f"下载视频超过 {cfg.download_timeout:.0f} 秒") from exc
                prompt = build_video_prompt(
                    info.title, info.owner, format_seconds(info.duration), info.description, max(300, cfg.max_chars - 400)
                )
                return await self.qwen.describe(video_path, prompt, audio_path)
            finally:
                shutil.rmtree(workdir, ignore_errors=True)

    async def _download_all(self, video_url: str, audio_url: str, video: Path, audio: Optional[Path], referer: str) -> None:
        budget = self.settings.max_mb * 1048576
        used = await self.bili.download(video_url, video, referer=referer, max_bytes=budget)
        if audio is not None:
            await self.bili.download(audio_url, audio, referer=referer, max_bytes=max(1, budget - used))

    async def _subtitle_text(self, ref: VideoRef, info: VideoInfo, notes: List[str]) -> tuple[str, List[Chapter]]:
        """字幕（短的原样、长的压缩，都带时间）和 UP 主标的分段。完整字幕另外缓存，供只看某一段时用。"""
        try:
            subtitles, chapters = await self.bili.subtitles(info)
        except BiliError as exc:
            self.log.warning("B站视频：字幕没拿到（%s）：%s %s", info.bvid, exc, exc.detail)
            notes.append(f"字幕没拿到：{exc}")
            return "", []
        if subtitles:
            self.cache.put(f"{ref.key}:subtitles", subtitles)
        if not subtitles:
            hint = "" if self.bili.has_login_cookie else "（B 站的 AI 字幕需要在插件里填登录 Cookie）"
            notes.append(f"这个视频没有可用的字幕{hint}")
            return "", chapters
        limit = int(self.settings.max_chars * 0.7)
        if len(subtitles) <= limit:
            return subtitles, chapters
        if self.summarize is not None:
            try:
                summary = (await self.summarize(subtitles, limit, format_chapters(chapters, limit=50))).strip()
                if summary:
                    return summary, chapters
            except Exception as exc:  # noqa: BLE001 - 压缩失败就截断，不影响结果
                self.log.warning("B站视频：字幕压缩失败：%s", exc)
        notes.append("字幕太长，只保留了开头部分")
        return subtitles[:limit], chapters


def compose(
    info: VideoInfo,
    video_text: Optional[str],
    subtitle_text: str,
    danmaku: List[str],
    notes: List[str],
    max_chars: int,
    chapters: Optional[List[Chapter]] = None,
) -> str:
    """拼成交给 AI 的一段文字，总长不超过 max_chars。"""
    title = f"《{info.title}》" + (f"（P{info.page}：{info.part_title}）" if info.page_count > 1 and info.part_title else "")
    meta = [f"UP主：{info.owner or '未知'}", f"时长：{format_seconds(info.duration)}"]
    if info.category:
        meta.append(f"分区：{info.category}")
    head = [f"【B站视频】{title}", "｜".join(meta)]
    if info.description and info.description != "-":
        desc = info.description.replace("\n", " ")
        head.append("简介：" + desc[:120] + ("…" if len(desc) > 120 else ""))
    if chapters:
        head.append("分段：" + format_chapters(chapters))
    tail: List[str] = []
    if danmaku:
        tail.append("热门弹幕：" + " ".join(f"「{d}」" for d in danmaku))
    if notes:
        tail.append("说明：" + "；".join(notes))

    if video_text:
        label, body = "视频内容（看画面和声音得到的描述）：", video_text
    elif subtitle_text:
        label, body = "字幕内容（行首或段首是视频里的时间）：", subtitle_text
    else:
        label, body = "", ""
    fixed = "\n".join(head + tail)
    room = max_chars - len(fixed) - len(label) - 4
    if body and room > 50:
        if len(body) > room:
            body = body[: room - 1] + "…"
        return "\n".join(head + [label + "\n" + body] + tail)
    return fixed[:max_chars]
