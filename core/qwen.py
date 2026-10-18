"""把视频交给阿里百炼的千问 Omni 模型，让它描述画面、声音和笑点。

用百炼的 OpenAI 兼容接口（流式）。小文件直接内嵌成 base64，大文件先传到百炼的临时空间，
只把临时地址交给模型。上传和模型处理各有各的超时。
临时上传的做法参考了 maibot-qwen-video（MIT 许可，© 2026 MaiBot Community）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import asyncio
import base64
import json

import aiohttp

# 内嵌成 base64 后的上限；再大就走临时上传
DATA_URL_LIMIT = 10 * 1024 * 1024

REGIONS = {
    "beijing": "https://dashscope.aliyuncs.com",
    "singapore": "https://dashscope-intl.aliyuncs.com",
}

SYSTEM_PROMPT = (
    "你是视频内容理解器。你的描述会交给另一个聊天 AI，让它像看过这个视频一样和朋友聊。"
    "只说视频里真的出现的东西，不要猜测或编造。"
)


class QwenError(Exception):
    """给人和 AI 看的错误说明（不含密钥）。"""


@dataclass
class QwenSettings:
    api_key: str
    region: str = "beijing"
    model: str = "qwen3.5-omni-plus"
    fps: float = 1.0
    max_pixels: int = 307200
    max_tokens: int = 1200
    upload_timeout: float = 120.0
    model_timeout: float = 180.0

    @property
    def host(self) -> str:
        return REGIONS.get(self.region, REGIONS["beijing"])


def build_video_prompt(title: str, owner: str, duration: str, description: str, max_chars: int) -> str:
    desc = description[:300] + ("…" if len(description) > 300 else "") if description else "（无）"
    return (
        f"B站视频《{title}》，UP主：{owner or '未知'}，时长 {duration}。简介：{desc}\n\n"
        f"请用中文、按时间顺序描述这个视频，控制在 {max_chars} 字以内：\n"
        "1. 画面里发生了什么（人物、动作、场景、画面上的文字）；\n"
        "2. 说了什么、有什么音乐或音效；\n"
        "3. 如果是搞笑或有梗的视频，笑点或梗在哪里；\n"
        "4. 整体氛围。\n"
        "不要评价视频好不好，不要加开场白。"
    )


class QwenClient:
    def __init__(self, session: aiohttp.ClientSession, settings: QwenSettings) -> None:
        self._session = session
        self.settings = settings

    async def describe(self, video: Path, prompt: str, audio: Optional[Path] = None) -> str:
        cfg = self.settings
        if not cfg.api_key:
            raise QwenError("没有填百炼的 API Key")
        uses_oss = False
        try:
            video_ref, oss1 = await asyncio.wait_for(self._prepare(video, "video/mp4"), cfg.upload_timeout)
            content: List[Dict[str, Any]] = [
                {"type": "video_url", "video_url": {"url": video_ref}, "fps": cfg.fps, "max_pixels": cfg.max_pixels}
            ]
            uses_oss = oss1
            if audio is not None:
                audio_ref, oss2 = await asyncio.wait_for(self._prepare(audio, "audio/mp4"), cfg.upload_timeout)
                content.append({"type": "input_audio", "input_audio": {"data": audio_ref, "format": "m4a"}})
                uses_oss = uses_oss or oss2
        except asyncio.TimeoutError as exc:
            raise QwenError(f"上传视频到百炼超过 {cfg.upload_timeout:.0f} 秒") from exc
        content.append({"type": "text", "text": prompt})
        payload = {
            "model": cfg.model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": content}],
            "modalities": ["text"],
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": cfg.max_tokens,
        }
        try:
            return await asyncio.wait_for(self._stream(payload, uses_oss), cfg.model_timeout)
        except asyncio.TimeoutError as exc:
            raise QwenError(f"百炼看视频超过 {cfg.model_timeout:.0f} 秒") from exc

    async def _prepare(self, path: Path, mime: str) -> tuple[str, bool]:
        size = path.stat().st_size
        prefix = f"data:{mime};base64,"
        if 4 * ((size + 2) // 3) + len(prefix) < DATA_URL_LIMIT:
            encoded = await asyncio.to_thread(lambda: base64.b64encode(path.read_bytes()).decode("ascii"))
            return prefix + encoded, False
        return await self._upload(path, mime), True

    async def _upload(self, path: Path, mime: str) -> str:
        cfg = self.settings
        headers = {"Authorization": f"Bearer {cfg.api_key}"}
        try:
            async with self._session.get(
                f"{cfg.host}/api/v1/uploads", params={"action": "getPolicy", "model": cfg.model}, headers=headers
            ) as response:
                body = await response.text()
                if response.status >= 400:
                    raise QwenError(f"获取百炼临时上传凭证失败：HTTP {response.status} {_api_message(body)}")
                payload = json.loads(body)
        except aiohttp.ClientError as exc:
            raise QwenError(f"连接百炼失败（{type(exc).__name__}）") from exc
        except ValueError as exc:
            raise QwenError("百炼返回的上传凭证看不懂") from exc
        policy = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else {}
        needed = ("upload_dir", "oss_access_key_id", "signature", "policy", "x_oss_object_acl", "x_oss_forbid_overwrite", "upload_host")
        if any(not policy.get(name) for name in needed):
            raise QwenError("百炼的临时上传凭证不完整")
        upload_host = str(policy["upload_host"])
        parsed = urlparse(upload_host)
        if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".aliyuncs.com"):
            raise QwenError("百炼给的上传地址不可信，已停止上传")
        key = f"{str(policy['upload_dir']).rstrip('/')}/{path.name}"
        form = aiohttp.FormData()
        for name, value in (
            ("OSSAccessKeyId", policy["oss_access_key_id"]),
            ("Signature", policy["signature"]),
            ("policy", policy["policy"]),
            ("x-oss-object-acl", policy["x_oss_object_acl"]),
            ("x-oss-forbid-overwrite", policy["x_oss_forbid_overwrite"]),
            ("key", key),
            ("success_action_status", "200"),
        ):
            form.add_field(name, str(value))
        try:
            with path.open("rb") as handle:  # aiohttp 边读边传，不把文件整个读进内存
                form.add_field("file", handle, filename=path.name, content_type=mime)
                async with self._session.post(upload_host, data=form) as response:
                    if response.status != 200:
                        raise QwenError(f"上传视频到百炼临时空间失败：HTTP {response.status}")
        except aiohttp.ClientError as exc:
            raise QwenError(f"上传视频到百炼失败（{type(exc).__name__}）") from exc
        return f"oss://{key}"

    async def _stream(self, payload: Dict[str, Any], uses_oss: bool) -> str:
        cfg = self.settings
        headers = {
            "Authorization": f"Bearer {cfg.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        if uses_oss:
            headers["X-DashScope-OssResourceResolve"] = "enable"
        chunks: List[str] = []
        try:
            async with self._session.post(f"{cfg.host}/compatible-mode/v1/chat/completions", json=payload, headers=headers) as response:
                if response.status >= 400:
                    raise QwenError(f"百炼返回 HTTP {response.status}：{_api_message(await response.text())}")
                async for raw in response.content:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except ValueError:
                        continue
                    choices = event.get("choices") if isinstance(event, dict) else None
                    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                        delta = choices[0].get("delta")
                        if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                            chunks.append(delta["content"])
        except aiohttp.ClientError as exc:
            raise QwenError(f"连接百炼失败（{type(exc).__name__}）") from exc
        text = "".join(chunks).strip()
        if not text:
            raise QwenError("百炼没有返回内容")
        return text


def _api_message(body: str) -> str:
    """从错误响应里取出说明，限制长度；不会带出请求头里的密钥。"""
    try:
        payload = json.loads(body)
        error = payload.get("error") if isinstance(payload, dict) else None
        message = error.get("message") if isinstance(error, dict) else payload.get("message")
        return str(message or "")[:160]
    except (ValueError, AttributeError):
        return body.strip()[:160]
