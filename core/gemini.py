"""把视频交给 Google Gemini，让它描述画面、声音和笑点（和百炼二选一）。

小文件直接内嵌在请求里；大文件先用 Gemini 的文件接口（Files API）上传，等它处理好再交给模型，用完就删。
真机发现：从日本往北京的百炼发十几 MB 的视频，每秒只有十几 KB；Google 的服务器离得近，快得多。
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
import time

import aiohttp

from .qwen import SYSTEM_PROMPT, ModelError, RequestBody, _mb

# 整个请求多大以内可以把视频直接放进去（官方各语言文档写的是 20MB 或 100MB，取保守的）
REQUEST_LIMIT = 20 * 1024 * 1024

RESOLUTIONS = {"低": "MEDIA_RESOLUTION_LOW", "标准": "MEDIA_RESOLUTION_MEDIUM"}


class GeminiError(ModelError):
    """Gemini 出的错。"""


@dataclass
class GeminiSettings:
    api_key: str
    model: str = "gemini-3.5-flash"
    fps: float = 1.0
    resolution: str = "低"
    max_tokens: int = 8192  # 新模型会先「思考」，思考也算在里面，所以给宽一点
    upload_timeout: float = 300.0
    model_timeout: float = 300.0


class GeminiClient:
    name = "Gemini"
    API = "https://generativelanguage.googleapis.com"
    POLL_SECONDS = 2.0  # 上传的大文件要等 Gemini 处理好，每隔几秒问一次

    def __init__(self, session: aiohttp.ClientSession, settings: GeminiSettings, logger: Optional[Any] = None) -> None:
        self._session = session
        self.settings = settings
        self.log = logger or logging.getLogger(__name__)
        self.last_upload = ""  # 最近一次发送或上传的情况，给 /bili_status 看
        self._upload_body: Optional[FileBody] = None

    @property
    def inline_limit(self) -> int:
        """多大以内的文件可以直接放进请求里（base64 会变大三分之一，再留点给文字）。"""
        return (REQUEST_LIMIT - 65536) * 3 // 4

    @property
    def inline_total(self) -> int:
        """Gemini 按整个请求限制，分开的画面和声音合起来也不能超过。"""
        return self.inline_limit

    async def describe(self, video: Path, prompt: str, audio: Optional[Path] = None) -> str:
        cfg = self.settings
        if not cfg.api_key:
            raise GeminiError("没有填 Gemini 的 API Key")
        uploaded: List[str] = []
        try:
            try:
                parts = await asyncio.wait_for(self._prepare_parts(video, audio, uploaded), cfg.upload_timeout)
            except asyncio.TimeoutError as exc:
                raise GeminiError(f"上传视频到 Gemini 超过 {cfg.upload_timeout:.0f} 秒（{self._upload_progress()}）") from exc
            if audio is not None:
                parts.append({"text": "上面第二个文件是这个视频的声音（画面和声音是分开的两个文件）。"})
            parts.append({"text": prompt})
            payload = {
                "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {
                    "maxOutputTokens": cfg.max_tokens,
                    "mediaResolution": RESOLUTIONS.get(cfg.resolution, RESOLUTIONS["低"]),
                },
            }
            body = RequestBody(json.dumps(payload, ensure_ascii=False).encode())
            try:
                return await asyncio.wait_for(self._generate(body), cfg.upload_timeout + cfg.model_timeout)
            except asyncio.TimeoutError as exc:
                if body.finished_at is None:
                    note = f"请求约 {_mb(body.size)}，只发出了 {_mb(body.sent)}，网络上传太慢"
                    self.last_upload = f"超时：{note}"
                    raise GeminiError(f"把视频发给 Gemini 超过时限（{note}）") from exc
                waited = time.monotonic() - body.finished_at
                raise GeminiError(f"Gemini 看视频超过 {waited:.0f} 秒（视频已经发过去了，是模型那边慢）") from exc
        finally:
            for name in uploaded:
                await self._delete(name)

    def _upload_progress(self) -> str:
        body = self._upload_body
        if body is None:
            note = "还没开始传文件"
        elif body.sent >= body.size:
            note = f"{_mb(body.size)} 已经全部传出，在等 Gemini 处理视频"
        else:
            note = f"{_mb(body.size)} 只传出了 {_mb(body.sent)}，网络上传太慢"
        self.last_upload = f"超时：{note}"
        return note

    async def _prepare_parts(self, video: Path, audio: Optional[Path], uploaded: List[str]) -> List[Dict[str, Any]]:
        self._upload_body = None
        files = [(video, "video/mp4")] + ([(audio, "audio/mp4")] if audio is not None else [])
        total = sum(path.stat().st_size for path, _ in files)
        parts: List[Dict[str, Any]] = []
        for path, mime in files:
            if total <= self.inline_limit:
                data = await asyncio.to_thread(lambda p=path: base64.b64encode(p.read_bytes()).decode("ascii"))
                part: Dict[str, Any] = {"inlineData": {"mimeType": mime, "data": data}}
            else:
                name, uri = await self._upload(path, mime)
                uploaded.append(name)
                part = {"fileData": {"mimeType": mime, "fileUri": uri}}
            if mime.startswith("video/"):
                part["videoMetadata"] = {"fps": self.settings.fps}
            parts.append(part)
        return parts

    def _headers(self) -> Dict[str, str]:
        return {"x-goog-api-key": self.settings.api_key}

    async def _upload(self, path: Path, mime: str) -> tuple[str, str]:
        """Files API 的「可续传上传」：先要一个上传地址，再把文件发过去，最后等它处理完（状态变成 ACTIVE）。"""
        cfg = self.settings
        size = path.stat().st_size
        start_headers = {
            **self._headers(),
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(size),
            "X-Goog-Upload-Header-Content-Type": mime,
        }
        try:
            async with self._session.post(
                f"{self.API}/upload/v1beta/files", json={"file": {"display_name": path.name}}, headers=start_headers
            ) as response:
                if response.status >= 400:
                    raise GeminiError(f"Gemini 拒绝了上传：HTTP {response.status} {_api_message(await response.text())}")
                upload_url = response.headers.get("X-Goog-Upload-URL", "")
        except aiohttp.ClientError as exc:
            raise GeminiError(f"连接 Gemini 失败（{type(exc).__name__}）") from exc
        if not is_trusted_google_url(upload_url, self.API):
            raise GeminiError("Gemini 给的上传地址不可信，已停止上传")

        self.log.info("B站视频：开始把 %s 传到 Gemini", _mb(size))
        started = time.monotonic()
        body = self._upload_body = FileBody(path, mime)
        headers = {"X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize"}
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=cfg.upload_timeout)
        try:
            async with self._session.post(upload_url, data=body, headers=headers, timeout=timeout) as response:
                text = await response.text()
                if response.status >= 400:
                    raise GeminiError(f"上传视频到 Gemini 失败：HTTP {response.status} {_api_message(text)}")
                info = _file_info(text)
        except aiohttp.ClientError as exc:
            raise GeminiError(f"上传视频到 Gemini 失败（{type(exc).__name__}）") from exc
        elapsed = max(time.monotonic() - started, 0.001)
        self.last_upload = f"上传 {_mb(size)} 用了 {elapsed:.0f} 秒（约 {_mb(size / elapsed)}/秒）"
        self.log.info("B站视频：上传到 Gemini 完成，%s", self.last_upload)

        name, uri = str(info.get("name") or ""), str(info.get("uri") or "")
        if not name.startswith("files/") or not uri:
            raise GeminiError("Gemini 返回的文件信息看不懂")
        state = str(info.get("state") or "")
        while state == "PROCESSING":
            await asyncio.sleep(self.POLL_SECONDS)
            state = str((await self._file(name)).get("state") or "")
        if state == "FAILED":
            raise GeminiError("Gemini 处理这个视频失败了")
        return name, uri

    async def _file(self, name: str) -> Dict[str, Any]:
        try:
            async with self._session.get(f"{self.API}/v1beta/{name}", headers=self._headers()) as response:
                text = await response.text()
                if response.status >= 400:
                    raise GeminiError(f"查询 Gemini 文件状态失败：HTTP {response.status} {_api_message(text)}")
                return _file_info(text)
        except aiohttp.ClientError as exc:
            raise GeminiError(f"连接 Gemini 失败（{type(exc).__name__}）") from exc

    async def _delete(self, name: str) -> None:
        """用完就删（不删 48 小时后也会自动删）；删不掉不影响结果。"""
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with self._session.delete(f"{self.API}/v1beta/{name}", headers=self._headers(), timeout=timeout):
                pass
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            self.log.info("B站视频：删除 Gemini 上的临时文件没成功（%s），48 小时后会自动删除", type(exc).__name__)

    async def _generate(self, body: RequestBody) -> str:
        cfg = self.settings
        headers = {**self._headers(), "Content-Type": "application/json"}
        # 模型看完整段视频才回复，期间连接上没有数据；「多久没数据就断开」按模型超时来
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=cfg.model_timeout)
        try:
            async with self._session.post(
                f"{self.API}/v1beta/models/{cfg.model}:generateContent", data=body, headers=headers, timeout=timeout
            ) as response:
                body.log_sent(self)
                text = await response.text()
                if response.status >= 400:
                    raise GeminiError(f"Gemini 返回 HTTP {response.status}：{_api_message(text)}")
        except aiohttp.ServerTimeoutError as exc:
            body.log_sent(self)
            raise GeminiError(f"Gemini 看视频超过 {cfg.model_timeout:.0f} 秒没有回音（视频已经发过去了，是模型那边慢）") from exc
        except aiohttp.ClientError as exc:
            raise GeminiError(f"连接 Gemini 失败（{type(exc).__name__}）") from exc
        return _reply_text(text)


class FileBody(aiohttp.payload.Payload):
    """上传的文件：边读边发，记下发出了多少，超时时用来判断是慢还是卡住。"""

    def __init__(self, path: Path, content_type: str) -> None:
        super().__init__(None, content_type=content_type)
        self._path = path
        self._size = path.stat().st_size
        self.sent = 0

    def decode(self, encoding: str = "utf-8", errors: str = "strict") -> str:
        raise TypeError("视频文件不能当文字读")

    async def write(self, writer: Any) -> None:
        for piece in self._pieces():
            await writer.write(piece)
            self.sent += len(piece)

    def _pieces(self) -> Iterator[bytes]:
        with self._path.open("rb") as handle:
            while chunk := handle.read(256 * 1024):
                yield chunk


def is_trusted_google_url(url: str, api: str) -> bool:
    """只往 Google 的地址传视频（测试时是本地假服务器的地址）。"""
    parsed, expected = urlparse(url), urlparse(api)
    if expected.scheme == "https":
        return parsed.scheme == "https" and (parsed.hostname or "").endswith(".googleapis.com")
    return (parsed.scheme, parsed.netloc) == (expected.scheme, expected.netloc)


def _file_info(text: str) -> Dict[str, Any]:
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise GeminiError("Gemini 返回的文件信息看不懂") from exc
    info = payload.get("file", payload) if isinstance(payload, dict) else {}
    return info if isinstance(info, dict) else {}


def _reply_text(text: str) -> str:
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise GeminiError("Gemini 的回复看不懂") from exc
    candidates = payload.get("candidates") if isinstance(payload, dict) else None
    if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
        feedback = payload.get("promptFeedback") if isinstance(payload, dict) else None
        reason = feedback.get("blockReason") if isinstance(feedback, dict) else ""
        raise GeminiError(f"Gemini 拒绝了这个视频（{reason}）" if reason else "Gemini 没有返回内容")
    content = candidates[0].get("content") if isinstance(candidates[0].get("content"), dict) else {}
    # 跳过模型的「思考」，只要正式回答
    pieces = [
        str(part.get("text") or "")
        for part in content.get("parts") or []
        if isinstance(part, dict) and not part.get("thought")
    ]
    reply = "".join(pieces).strip()
    if not reply:
        reason = str(candidates[0].get("finishReason") or "")
        raise GeminiError(f"Gemini 没有返回内容（{reason}）" if reason else "Gemini 没有返回内容")
    return reply


def _api_message(body: str) -> str:
    """从错误响应里取出说明，限制长度；不会带出请求头里的密钥。"""
    try:
        payload = json.loads(body)
        error = payload.get("error") if isinstance(payload, dict) else None
        return str(error.get("message") if isinstance(error, dict) else "")[:160]
    except (ValueError, AttributeError):
        return body.strip()[:160]
