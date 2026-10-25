"""她自己的 B 站账号：扫码登录、登录凭证、风控暂停、每日配额。

- 登录凭证存在插件数据目录的 credential.json（只有插件读），不出现在 WebUI 配置里。
- 被 B 站拦下（未登录、风控、请求过多）就持久暂停：重启也不恢复，不自动重试，只有主人发 /bili_resume 才解除。
- 每天的请求总数、各种操作的次数有上限；两次请求、两次操作之间有最短间隔；同一个视频的同一种操作只做一次。
思路参考 dsh-bili-humanism 与 bilibili_autopilot（均为 MIT 许可），代码为重写。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import json
import os
import time

# 扫码登录轮询的状态码
QR_WAITING, QR_SCANNED, QR_EXPIRED, QR_DONE = 86101, 86090, 86038, 0
# 这些情况一律暂停账号操作，等主人处理
PAUSE_HTTP = {401, 403, 412, 429}
PAUSE_CODES = {
    -101: "账号没登录或登录已失效，需要重新扫码",
    -111: "csrf 校验失败，登录信息可能已失效，需要重新扫码",
    -352: "被 B 站风控拦下了",
    -412: "请求被 B 站拦截了",
    -509: "请求太频繁",
    -799: "请求太频繁",
    22015: "账号被限制操作",
}
ACTIONS = ("like", "coin", "favorite", "later", "follow", "history")
ACTION_NAMES = {"like": "点赞", "coin": "投币", "favorite": "收藏", "later": "稍后再看", "follow": "关注 UP 主", "history": "观看记录"}


@dataclass
class Limits:
    requests_per_day: int = 600
    request_interval: float = 2.0
    action_interval: float = 30.0
    per_day: Dict[str, int] = field(
        default_factory=lambda: {"like": 30, "coin": 2, "favorite": 15, "later": 15, "follow": 5, "history": 40}
    )


class AccountPaused(Exception):
    """账号操作暂停中（给 AI 看的原因）。"""


class QuotaExceeded(Exception):
    """今天这种操作的次数用完了，或者这个视频已经做过了。"""


class Account:
    """凭证与守卫的状态都存在数据目录，重启不丢。"""

    def __init__(self, data_dir: Path, limits: Optional[Limits] = None) -> None:
        self.dir = data_dir
        self.limits = limits or Limits()
        self._state = self._read("account_state.json") or {}
        self._last_request = 0.0
        self._last_action = 0.0
        self.on_pause: Callable[[], None] = lambda: None  # 插件用来把看视频那边的登录信息也撤掉

    # ── 凭证 ──────────────────────────────────────

    def credential(self) -> Dict[str, Any]:
        return self._read("credential.json") or {}

    def cookies(self) -> Dict[str, str]:
        raw = self.credential().get("cookies")
        return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}

    @property
    def logged_in(self) -> bool:
        return bool(self.cookies().get("SESSDATA"))

    @property
    def csrf(self) -> str:
        return self.cookies().get("bili_jct", "")

    def save_credential(self, cookies: Dict[str, str], refresh_token: str = "", *, mid: str = "", uname: str = "") -> None:
        self._write(
            "credential.json",
            {"cookies": cookies, "refresh_token": refresh_token, "mid": mid, "uname": uname, "saved_at": time.time()},
        )
        self.resume(reason_contains="登录")  # 重新登录后，因为登录失效而暂停的状态自然解除

    def update_profile(self, mid: str, uname: str) -> None:
        cred = self.credential()
        if cred:
            cred.update(mid=mid, uname=uname)
            self._write("credential.json", cred)

    # ── 暂停 ──────────────────────────────────────

    @property
    def paused(self) -> str:
        return str(self._state.get("paused_reason") or "")

    def pause(self, reason: str) -> None:
        if not self.paused:
            self._state["paused_reason"] = reason
            self._state["paused_at"] = time.time()
            self._save_state()
            self.on_pause()

    def resume(self, *, reason_contains: str = "") -> bool:
        if not self.paused or (reason_contains and reason_contains not in self.paused):
            return False
        self._state.pop("paused_reason", None)
        self._state.pop("paused_at", None)
        self._save_state()
        return True

    def pause_reason_for(self, *, http_status: int = 0, code: Optional[int] = None) -> str:
        if http_status in PAUSE_HTTP:
            return {401: "B 站返回未授权（HTTP 401）", 403: "B 站拒绝访问（HTTP 403）", 412: "被 B 站风控拦下（HTTP 412）", 429: "请求太频繁（HTTP 429）"}[http_status]
        if code is not None and code in PAUSE_CODES:
            return f"{PAUSE_CODES[code]}（code {code}）"
        return ""

    # ── 配额与节奏 ────────────────────────────────

    def before_request(self) -> float:
        """请求前调用：暂停或今天请求数用完时抛异常；返回还需要等几秒（调用方去 sleep）。"""
        if self.paused:
            raise AccountPaused(f"B 站那边暂停了：{self.paused}。等主人处理后再试")
        day = self._roll()
        if int(day.get("requests", 0)) >= self.limits.requests_per_day:
            raise QuotaExceeded("今天逛 B 站的次数用完了，明天再来")
        wait = max(0.0, self._last_request + self.limits.request_interval - time.monotonic())
        day["requests"] = int(day.get("requests", 0)) + 1
        self._last_request = time.monotonic() + wait
        self._save_state()
        return wait

    def check_action(self, action: str, key: str) -> float:
        """操作前检查：今天次数、这个视频是否做过。返回还需要等几秒。"""
        if action not in ACTIONS:
            raise QuotaExceeded(f"不支持的操作：{action}")
        if self.paused:
            raise AccountPaused(f"B 站那边暂停了：{self.paused}。等主人处理后再试")
        day = self._roll()
        counts = day.setdefault("actions", {})
        if int(counts.get(action, 0)) >= int(self.limits.per_day.get(action, 0)):
            raise QuotaExceeded(f"今天{ACTION_NAMES[action]}的次数用完了")
        if key in self._done(action):
            raise QuotaExceeded(f"这个已经{ACTION_NAMES[action]}过了")
        return max(0.0, self._last_action + self.limits.action_interval - time.monotonic())

    def record_action(self, action: str, key: str) -> None:
        day = self._roll()
        counts = day.setdefault("actions", {})
        counts[action] = int(counts.get(action, 0)) + 1
        done = self._done(action)
        done.append(key)
        del done[:-500]  # 只记最近 500 个，够判断重复
        self._last_action = time.monotonic()
        self._save_state()

    def today(self) -> Dict[str, Any]:
        return self._roll()

    # ── 内部 ──────────────────────────────────────

    def _done(self, action: str) -> list:
        return self._state.setdefault("done", {}).setdefault(action, [])

    def _roll(self) -> Dict[str, Any]:
        today = time.strftime("%Y-%m-%d", time.localtime(time.time()))
        day = self._state.get("day")
        if not isinstance(day, dict) or day.get("date") != today:
            day = {"date": today, "requests": 0, "actions": {}}
            self._state["day"] = day
        return day

    def _save_state(self) -> None:
        self._write("account_state.json", self._state)

    def _read(self, name: str) -> Any:
        try:
            return json.loads((self.dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _write(self, name: str, data: Any) -> None:
        """原子写入；文件只有插件自己能读（credential.json 里有登录信息）。"""
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / name
        temp = path.with_suffix(".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, indent=1))
        os.chmod(temp, 0o600)
        os.replace(temp, path)


def cookies_from_response(response: Any) -> Dict[str, str]:
    """aiohttp 响应里 Set-Cookie 的值。"""
    return {name: morsel.value for name, morsel in response.cookies.items() if morsel.value}


def qr_png_base64(text: str) -> str:
    """把登录链接画成二维码图片（PNG 的 base64）。"""
    import base64
    import io

    import qrcode  # MaiBot 按插件清单自动安装

    image = qrcode.make(text, box_size=8, border=3)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()
