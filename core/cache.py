"""看过的视频记一段时间，同一个视频再发不用重新花钱看。"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

import json
import os
import time


class ResultCache:
    def __init__(self, path: Path, ttl_seconds: float = 7 * 86400, max_entries: int = 100) -> None:
        self._path = path
        self._ttl = ttl_seconds
        self._max = max_entries
        self._items: Dict[str, Dict[str, object]] = {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._items = {k: v for k, v in raw.items() if isinstance(v, dict) and isinstance(v.get("text"), str)}
        except (OSError, ValueError):
            self._items = {}

    def __len__(self) -> int:
        return len(self._items)

    def get(self, key: str, now: Optional[float] = None) -> Optional[str]:
        item = self._items.get(key)
        if item is None:
            return None
        if (now or time.time()) - float(item.get("at", 0)) > self._ttl:
            self._items.pop(key, None)
            return None
        return str(item["text"])

    def put(self, key: str, text: str, now: Optional[float] = None) -> None:
        self._items[key] = {"at": now or time.time(), "text": text}
        if len(self._items) > self._max:
            oldest = sorted(self._items, key=lambda k: float(self._items[k].get("at", 0)))
            for stale in oldest[: len(self._items) - self._max]:
                self._items.pop(stale, None)
        self._save()

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temp = self._path.with_suffix(".tmp")
            temp.write_text(json.dumps(self._items, ensure_ascii=False), encoding="utf-8")
            os.replace(temp, self._path)
        except OSError:
            pass  # 缓存写不进去不影响看视频
