"""从聊天文字里找出 B 站视频链接。

认得这些写法：
- https://www.bilibili.com/video/BV1xx411c7mD?p=2（也包括 m.bilibili.com、av 号）
- b23.tv / bili2233.cn / bili23.cn 短链接（要联网解析，见 bilibili.py）
- 单独发的 BV 号
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import parse_qs, urlparse

import re

SHORT_HOSTS = ("b23.tv", "bili2233.cn", "bili23.cn")

_VIDEO_URL = re.compile(
    r"https?://(?:www\.|m\.)?bilibili\.com/video/(BV[0-9A-Za-z]{10}|av\d+)[^\s，。！？、）)\]】]*",
    re.IGNORECASE,
)
_SHORT_URL = re.compile(r"https?://(?:b23\.tv|bili2233\.cn|bili23\.cn)/[0-9A-Za-z]+", re.IGNORECASE)
_BARE_BV = re.compile(r"(?<![0-9A-Za-z])(BV[0-9A-Za-z]{10})(?![0-9A-Za-z])")


@dataclass(frozen=True)
class VideoRef:
    """一个视频（的某一 P）。bvid 和 aid 至少有一个。"""

    bvid: str = ""
    aid: int = 0
    page: int = 1

    @property
    def key(self) -> str:
        return f"{self.bvid or f'av{self.aid}'}:p{self.page}"

    @property
    def url(self) -> str:
        ident = self.bvid or f"av{self.aid}"
        return f"https://www.bilibili.com/video/{ident}" + (f"?p={self.page}" if self.page > 1 else "")


def parse_video_url(url: str) -> Optional[VideoRef]:
    """完整的视频页链接 → VideoRef；不是视频页返回 None。"""
    match = _VIDEO_URL.match(url.strip())
    if not match:
        return None
    ident = match.group(1)
    page = 1
    try:
        values = parse_qs(urlparse(match.group(0)).query).get("p")
        if values:
            page = max(1, int(values[0]))
    except ValueError:
        page = 1
    if ident.lower().startswith("av"):
        return VideoRef(aid=int(ident[2:]), page=page)
    return VideoRef(bvid="BV" + ident[2:], page=page)


def is_short_url(url: str) -> bool:
    host = (urlparse(url.strip()).hostname or "").lower()
    return host in SHORT_HOSTS


def find_links(text: str) -> List[str]:
    """按出现顺序找出文字里的 B 站链接（视频页链接、短链接、单独的 BV 号），去重。"""
    found: List[tuple[int, str]] = []
    for pattern in (_VIDEO_URL, _SHORT_URL):
        found.extend((m.start(), m.group(0)) for m in pattern.finditer(text))
    covered = [(start, start + len(link)) for start, link in found]
    for match in _BARE_BV.finditer(text):
        if not any(a <= match.start() < b for a, b in covered):
            found.append((match.start(), match.group(1)))
    result: List[str] = []
    for _, link in sorted(found):
        if link not in result:
            result.append(link)
    return result


def ref_from_text(link: str) -> Optional[VideoRef]:
    """不用联网就能认出的写法（视频页链接或 BV 号）；短链接返回 None。"""
    link = link.strip()
    if _BARE_BV.fullmatch(link):
        return VideoRef(bvid="BV" + link[2:])
    return parse_video_url(link)
