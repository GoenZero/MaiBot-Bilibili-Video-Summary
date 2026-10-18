"""把视频交给阿里百炼的千问 Omni 模型，让它描述画面、声音和笑点。

用百炼的 OpenAI 兼容接口（流式）。小文件直接内嵌成 base64，大文件先传到百炼的临时空间，
只把临时地址交给模型。上传和模型处理各有各的超时。
临时上传的做法参考了 maibot-qwen-video（MIT 许可，© 2026 MaiBot Community）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import urlparse

import asyncio
import base64
import json
import logging
import re
import time
import uuid

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
    upload_timeout: float = 300.0
    model_timeout: float = 300.0

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
    def __init__(self, session: aiohttp.ClientSession, settings: QwenSettings, logger: Optional[Any] = None) -> None:
        self._session = session
        self.settings = settings
        self.log = logger or logging.getLogger(__name__)
        self._upload_body: Optional[OssFormPayload] = None
        self.last_upload = ""  # 最近一次上传的情况，给 /bili_status 看（日志会滚走）

    @property
    def inline_limit(self) -> int:
        """多大以内的文件可以直接放进请求里（不用先传到临时空间）。"""
        return (DATA_URL_LIMIT - 64) * 3 // 4

    async def describe(self, video: Path, prompt: str, audio: Optional[Path] = None) -> str:
        cfg = self.settings
        if not cfg.api_key:
            raise QwenError("没有填百炼的 API Key")
        try:
            # 画面和声音（如果分开）一起算在同一个上传时限里
            content, uses_oss = await asyncio.wait_for(self._prepare_all(video, audio), cfg.upload_timeout)
        except asyncio.TimeoutError as exc:
            raise QwenError(f"上传视频到百炼超过 {cfg.upload_timeout:.0f} 秒（{self._upload_progress(video, audio)}）") from exc
        content.append({"type": "text", "text": prompt})
        payload = {
            "model": cfg.model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": content}],
            "modalities": ["text"],
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": cfg.max_tokens,
        }
        # 视频直接放在请求里时，请求本身就有十几 MB，从国外发过去也要时间（真机发现）：
        # 发送算在上传时限里，发完以后模型思考算在模型时限里，超时时说清楚卡在哪一步
        body = RequestBody(json.dumps(payload, ensure_ascii=False).encode())
        try:
            return await asyncio.wait_for(self._stream(body, uses_oss), cfg.upload_timeout + cfg.model_timeout)
        except asyncio.TimeoutError as exc:
            if body.finished_at is None:
                note = f"请求约 {_mb(body.size)}，只发出了 {_mb(body.sent)}，网络上传太慢"
                self.last_upload = f"超时：{note}"
                raise QwenError(f"把视频发给百炼超过时限（{note}）") from exc
            raise QwenError(f"百炼看视频超过 {time.monotonic() - body.finished_at:.0f} 秒（视频已经发过去了，是模型那边慢）") from exc

    def _upload_progress(self, video: Path, audio: Optional[Path]) -> str:
        """上传超时时说明传到哪了：只传了一部分是网络慢；全传完了还没回音是阿里云那边没响应。"""
        body = self._upload_body
        if body is None:
            total = video.stat().st_size + (audio.stat().st_size if audio is not None else 0)
            note = f"视频约 {_mb(total)}，还没开始传文件"
        elif body.sent >= body.size:
            note = f"{_mb(body.size)} 已经全部传出，但阿里云一直没回应"
        else:
            note = f"{_mb(body.size)} 只传出了 {_mb(body.sent)}，网络上传太慢"
        self.last_upload = f"超时：{note}"
        return note

    async def _prepare_all(self, video: Path, audio: Optional[Path]) -> tuple[List[Dict[str, Any]], bool]:
        cfg = self.settings
        self._upload_body = None
        video_ref, uses_oss = await self._prepare(video, "video/mp4")
        content: List[Dict[str, Any]] = [
            {"type": "video_url", "video_url": {"url": video_ref}, "fps": cfg.fps, "max_pixels": cfg.max_pixels}
        ]
        if audio is not None:
            audio_ref, audio_oss = await self._prepare(audio, "audio/mp4")
            content.append({"type": "input_audio", "input_audio": {"data": audio_ref, "format": "m4a"}})
            uses_oss = uses_oss or audio_oss
        return content, uses_oss

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
        if not is_trusted_upload_host(upload_host):
            raise QwenError("百炼给的上传地址不可信，已停止上传")
        key = f"{str(policy['upload_dir']).rstrip('/')}/{path.name}"
        fields = [
            ("OSSAccessKeyId", str(policy["oss_access_key_id"])),
            ("Signature", str(policy["signature"])),
            ("policy", str(policy["policy"])),
            ("x-oss-object-acl", str(policy["x_oss_object_acl"])),
            ("x-oss-forbid-overwrite", str(policy["x_oss_forbid_overwrite"])),
            ("key", key),
            ("success_action_status", "200"),
        ]
        size = path.stat().st_size
        self.log.info("B站视频：开始把 %s 传到百炼临时空间", _mb(size))
        started = time.monotonic()
        try:
            body = self._upload_body = OssFormPayload(path, fields=fields, content_type=mime)
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=cfg.upload_timeout)
            async with self._session.post(upload_host, data=body, timeout=timeout) as response:
                if response.status != 200:
                    reason = _oss_message(await response.text(errors="replace"))
                    raise QwenError(f"上传视频到百炼临时空间失败：HTTP {response.status} {reason}".rstrip())
        except aiohttp.ServerTimeoutError as exc:
            raise QwenError(f"上传视频到百炼超过 {cfg.upload_timeout:.0f} 秒没有回音") from exc
        except aiohttp.ClientError as exc:
            raise QwenError(f"上传视频到百炼失败（{type(exc).__name__}）") from exc
        elapsed = max(time.monotonic() - started, 0.001)
        self.last_upload = f"{_mb(size)} 用了 {elapsed:.0f} 秒（约 {_mb(size / elapsed)}/秒）"
        self.log.info("B站视频：上传完成，%s", self.last_upload)
        return f"oss://{key}"

    async def _stream(self, body: "RequestBody", uses_oss: bool) -> str:
        cfg = self.settings
        headers = {
            "Authorization": f"Bearer {cfg.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        if uses_oss:
            headers["X-DashScope-OssResourceResolve"] = "enable"
        chunks: List[str] = []
        # 模型要先把整段视频看完才开始回文字，这期间连接上一个字节都没有；
        # 所以「多久没收到数据就断开」要按模型超时来，不能用会话默认的 60 秒（真机发现）
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=cfg.model_timeout)
        try:
            async with self._session.post(
                f"{cfg.host}/compatible-mode/v1/chat/completions", data=body, headers=headers, timeout=timeout
            ) as response:
                body.log_sent(self)
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
        except aiohttp.ServerTimeoutError as exc:
            body.log_sent(self)
            raise QwenError(f"百炼看视频超过 {cfg.model_timeout:.0f} 秒没有回音（视频已经发过去了，是模型那边慢）") from exc
        except aiohttp.ClientError as exc:
            raise QwenError(f"连接百炼失败（{type(exc).__name__}）") from exc
        text = "".join(chunks).strip()
        if not text:
            raise QwenError("百炼没有返回内容")
        return text


def is_trusted_upload_host(url: str) -> bool:
    """只往阿里云的地址传视频，防止被引到别处。"""
    parsed = urlparse(url)
    return parsed.scheme == "https" and (parsed.hostname or "").endswith(".aliyuncs.com")


class RequestBody(aiohttp.payload.Payload):
    """发给模型的请求体：分小块发出，记下发出了多少、什么时候发完，用来区分「网络慢」和「模型慢」。"""

    def __init__(self, data: bytes) -> None:
        super().__init__(None, content_type="application/json")
        self._data = data
        self._size = len(data)
        self.sent = 0
        self.started_at = time.monotonic()
        self.finished_at: Optional[float] = None
        self._logged = False

    def decode(self, encoding: str = "utf-8", errors: str = "strict") -> str:
        return self._data.decode(encoding, errors)

    async def write(self, writer: Any) -> None:
        view = memoryview(self._data)
        for start in range(0, len(view), 256 * 1024):
            piece = view[start : start + 256 * 1024]
            await writer.write(piece)
            self.sent += len(piece)
        self.finished_at = time.monotonic()

    def log_sent(self, client: "QwenClient") -> None:
        """请求发完后写一行日志（大请求才写，只写一次）；/bili_status 也能看到。"""
        if self._logged or self.finished_at is None or self.size < 1048576:
            return
        self._logged = True
        elapsed = max(self.finished_at - self.started_at, 0.001)
        client.last_upload = f"直接发送 {_mb(self.size)} 用了 {elapsed:.0f} 秒（约 {_mb(self.size / elapsed)}/秒）"
        client.log.info("B站视频：视频已发给百炼，%s；等模型看完", client.last_upload)


class OssFormPayload(aiohttp.payload.Payload):
    """阿里云 OSS 表单上传用的请求体：总长度事先算好，文本字段不带 Content-Type，文件放最后，边读边发。

    aiohttp 自带的表单会给每个文本字段加「Content-Type: text/plain」，OSS 不认，返回 HTTP 400（真机发现）。
    做法参考 maibot-qwen-video（MIT 许可，© 2026 MaiBot Community）。
    """

    def __init__(self, path: Path, *, fields: List[tuple[str, str]], content_type: str) -> None:
        self._path = path
        boundary = uuid.uuid4().hex
        parts: List[bytes] = []
        for name, value in fields:
            if any(ch in name + value for ch in "\r\n\""):
                raise QwenError("上传表单里有不该出现的字符")
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n".encode()
        )
        self._head = b"".join(parts)
        self.sent = 0  # 已经交给网络发出的字节数，超时时用来判断是慢还是卡住
        self._tail = f"\r\n--{boundary}--\r\n".encode()
        super().__init__(None, content_type=f"multipart/form-data; boundary={boundary}")
        self._size = len(self._head) + path.stat().st_size + len(self._tail)

    def decode(self, encoding: str = "utf-8", errors: str = "strict") -> str:
        raise TypeError("上传表单不能当文字读")

    async def write(self, writer: Any) -> None:
        for piece in self._pieces():
            await writer.write(piece)
            self.sent += len(piece)

    def _pieces(self) -> Iterator[bytes]:
        yield self._head
        with self._path.open("rb") as handle:
            while chunk := handle.read(256 * 1024):
                yield chunk
        yield self._tail


def _mb(size: float) -> str:
    """字节数写成「12.3MB」或「850KB」，给日志和错误说明用。"""
    if size >= 1048576:
        return f"{size / 1048576:.1f}MB"
    return f"{max(size / 1024, 0.1):.0f}KB" if size >= 1024 else f"{size:.0f}B"


def _oss_message(body: str) -> str:
    """阿里云 OSS 的错误是 XML：取出 Code 和 Message，方便看日志排查。"""
    code = re.search(r"<Code>(.*?)</Code>", body or "")
    message = re.search(r"<Message>(.*?)</Message>", body or "")
    return " ".join(m.group(1) for m in (code, message) if m)[:200]


def _api_message(body: str) -> str:
    """从错误响应里取出说明，限制长度；不会带出请求头里的密钥。"""
    try:
        payload = json.loads(body)
        error = payload.get("error") if isinstance(payload, dict) else None
        message = error.get("message") if isinstance(error, dict) else payload.get("message")
        return str(message or "")[:160]
    except (ValueError, AttributeError):
        return body.strip()[:160]
