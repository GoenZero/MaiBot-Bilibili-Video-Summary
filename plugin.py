"""MaiBot B 站视频插件：让 AI 能「看」B 站视频。

短视频交给阿里百炼的千问 Omni 看画面和声音；长视频读字幕；都附上标题、简介和热门弹幕。
AI 用 bilibili_watch 工具决定看不看；收到链接时插件先在后台看起来，省得干等。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional

import shutil

import aiohttp
from maibot_sdk import Command, HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, ToolParameterInfo, ToolParamType

from .config import PLUGIN_VERSION, BilibiliVideoConfig
from .core.bilibili import BiliClient, BiliError
from .core.cache import ResultCache
from .core.links import find_links
from .core.qwen import QwenClient, QwenSettings
from .core.watcher import WatchSettings, Watcher

WATCH_TOOL = "bilibili_watch"
WATCH_DESCRIPTION = (
    "看一个 B 站视频，拿到视频的内容描述（短视频会看画面和声音，长视频读字幕），以及标题、UP 主、简介和热门弹幕。"
    "对方发来 B 站链接（包括 b23.tv 短链接或 BV 号）、想让你看看，或者你好奇视频内容时用。"
    "B 站链接请用这个工具，不必再用网页抓取工具去抓 B 站页面。"
    "看完像自己看过一样自然地聊，不要逐条复述描述，也不要提到工具。"
)
# 一条消息里最多预取几个链接
PREFETCH_PER_MESSAGE = 2


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

    # ── 生命周期 ──────────────────────────────────

    async def on_load(self) -> None:
        data_dir = Path(self.ctx.paths.data_dir)
        shutil.rmtree(data_dir / "tmp", ignore_errors=True)  # 上次没清掉的临时文件
        self._cache = ResultCache(data_dir / "cache.json")
        await self._build()
        self.ctx.logger.info("B站视频插件已加载（v%s）", PLUGIN_VERSION)

    async def on_unload(self) -> None:
        await self._close()

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        del scope, config_data, version
        await self._close()
        await self._build()
        self.ctx.logger.info("B站视频插件配置已生效")

    async def _build(self) -> None:
        cfg = self.config
        # trust_env：沿用 MaiBot 容器里设置的 HTTP(S)_PROXY
        self._session = aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=60))
        bili = BiliClient(self._session, cfg.bilibili.cookie)
        qwen = None
        if cfg.qwen.api_key.strip():
            qwen = QwenClient(
                self._session,
                QwenSettings(
                    api_key=cfg.qwen.api_key.strip(),
                    region=cfg.qwen.region,
                    model=cfg.qwen.model.strip() or "qwen3.5-omni-plus",
                    fps=cfg.qwen.fps,
                    model_timeout=float(cfg.watch.model_timeout),
                ),
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
            qwen,
            self._cache,
            settings,
            Path(self.ctx.paths.data_dir) / "tmp",
            summarize=self._summarize,
            logger=self.ctx.logger,
        )

    async def _close(self) -> None:
        watcher, self._watcher = self._watcher, None
        if watcher is not None:
            watcher.close()
        session, self._session = self._session, None
        if session is not None:
            await session.close()

    async def _summarize(self, subtitles: str, limit: int) -> str:
        prompt = (
            f"下面是一个 B 站视频的字幕，每行开头是时间。请用中文按时间顺序概括主要内容和关键细节，{limit} 字以内，"
            "可以保留几个关键时间点。只根据字幕内容，不要编造，不要加开场白。\n\n字幕：\n" + subtitles[:30000]
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
        ],
        timeout_ms=600_000,
    )
    async def tool_watch(self, url: str = "", **kwargs: Any) -> Dict[str, Any]:
        del kwargs
        watcher = self._watcher
        if watcher is None or not self.config.plugin.enabled:
            return {"success": False, "error": "看视频的功能现在关着"}
        links = find_links(str(url or "")) or [str(url or "").strip()]
        try:
            content = await watcher.watch(links[0])
        except BiliError as exc:
            self._last_error = str(exc)
            return {"success": False, "error": f"这个视频没看成：{exc}"}
        except Exception as exc:  # noqa: BLE001 - 任何意外都如实告诉 AI，而不是让工具调用报错
            self.ctx.logger.exception("B站视频：看视频时出错")
            self._last_error = type(exc).__name__
            return {"success": False, "error": "这个视频没看成：插件出了点问题"}
        return {"success": True, "content": content}

    # ── 状态命令 ──────────────────────────────────

    @Command("bili_status", description="查看 B 站视频插件的状态", pattern=r"^/bili_status$")
    async def handle_status(self, stream_id: str = "", **kwargs: Any) -> tuple:
        del kwargs
        text = await self.status_text()
        # 不存进聊天记录：否则 MaiBot 重启后会把状态当成 AI 说过的话
        await self.ctx.send.text(text, stream_id, storage_message=False)
        return True, "状态已显示", True

    async def status_text(self) -> str:
        cfg = self.config
        lines = [f"📺 B站视频 v{PLUGIN_VERSION}", "状态：" + ("已启用" if cfg.plugin.enabled else "已关闭")]
        watcher = self._watcher
        if watcher is not None and watcher.bili.has_login_cookie:
            try:
                logged_in, name = await watcher.bili.login_status()
                lines.append(f"B 站登录：✅ {name}" if logged_in else "B 站登录：❌ Cookie 已失效，请重新粘贴")
            except BiliError as exc:
                lines.append(f"B 站登录：查不到（{exc}）")
        else:
            lines.append("B 站登录：未填写（读不到 AI 字幕）")
        lines.append(f"看画面的模型：{cfg.qwen.model}（{'北京' if cfg.qwen.region == 'beijing' else '新加坡'}）" if cfg.qwen.api_key.strip() else "看画面的模型：未填写百炼 API Key（只读字幕和简介）")
        lines.append(f"看画面的时长上限：{cfg.watch.short_minutes} 分钟")
        lines.append(f"已缓存：{len(self._cache) if self._cache else 0} 个视频")
        if self._last_error:
            lines.append(f"最近一次失败：{self._last_error}")
        return "\n".join(lines)


def create_plugin() -> BilibiliVideoPlugin:
    """MaiBot 插件工厂函数。"""
    return BilibiliVideoPlugin()
