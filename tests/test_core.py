from __future__ import annotations

from pathlib import Path

import asyncio
import json

import aiohttp
import pytest

from bili_video_under_test.core import bilibili as bili_module
from bili_video_under_test.core import qwen as qwen_module
from bili_video_under_test.core.bilibili import BiliClient, BiliError, mixin_key, parse_cookie, sign_wbi
from bili_video_under_test.core.cache import ResultCache
from bili_video_under_test.core.links import VideoRef, find_links, parse_video_url, ref_from_text
from bili_video_under_test.core.qwen import QwenClient, QwenError, QwenSettings
from bili_video_under_test.core.watcher import WatchSettings, Watcher

from fake_servers import BVID, FakeWorld, start


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)


def test_find_links_in_chat_text():
    text = (
        "看这个 https://www.bilibili.com/video/BV1xx411c7mD?p=2&spm=1，"
        "还有 https://b23.tv/AbC123 和 m 站 https://m.bilibili.com/video/av170001 以及 BV1yy411c7mE。"
        "重复的 https://www.bilibili.com/video/BV1xx411c7mD?p=2&spm=1"
    )
    assert find_links(text) == [
        "https://www.bilibili.com/video/BV1xx411c7mD?p=2&spm=1",
        "https://b23.tv/AbC123",
        "https://m.bilibili.com/video/av170001",
        "BV1yy411c7mE",
    ]
    assert parse_video_url("https://www.bilibili.com/video/BV1xx411c7mD?p=2") == VideoRef(bvid="BV1xx411c7mD", page=2)
    assert parse_video_url("https://m.bilibili.com/video/av170001") == VideoRef(aid=170001)
    assert ref_from_text("BV1xx411c7mD") == VideoRef(bvid="BV1xx411c7mD")
    assert ref_from_text("https://b23.tv/AbC123") is None
    assert find_links("没有链接，只有 bilibili.com 主页") == []


def test_cookie_and_wbi_signature():
    assert parse_cookie("SESSDATA=abc%2C1; bili_jct=x ;buvid3=y") == {"SESSDATA": "abc%2C1", "bili_jct": "x", "buvid3": "y"}
    assert parse_cookie("abc123") == {"SESSDATA": "abc123"}
    assert parse_cookie("  ") == {}
    # B 站文档里的公开示例：这两个 key 拼出的 mixin key 是固定的
    key = mixin_key("7cd084941338484aae1ad9425b84077c", "4932caff0ff746eab6f01bf08b70ac45")
    assert key == "ea1db124af3c7062474693fa704f4ff8"
    signed = sign_wbi({"foo": "114", "bar": "514", "zab": "1919810"}, key, timestamp=1702204169)
    assert signed["w_rid"] == "8f6f2b5b3d485fe1886cec6a0be8c5d4"


def test_cache_expires_and_trims(tmp_path):
    cache = ResultCache(tmp_path / "c.json", ttl_seconds=100, max_entries=2)
    cache.put("a", "A", now=1)
    cache.put("b", "B", now=2)
    cache.put("c", "C", now=3)
    assert cache.get("a", now=3) is None and cache.get("c", now=3) == "C"
    assert ResultCache(tmp_path / "c.json", ttl_seconds=100).get("b", now=50) == "B"
    assert cache.get("b", now=500) is None
    (tmp_path / "bad.json").write_text("不是 json")
    assert len(ResultCache(tmp_path / "bad.json")) == 0


def run_with_world(test):
    async def main():
        world = FakeWorld()
        server = await start(world)
        session = aiohttp.ClientSession()
        try:
            await test(world, session)
        finally:
            await session.close()
            await server.close()

    asyncio.run(main())


def client_for(world: FakeWorld, session, cookie: str = "") -> BiliClient:
    client = BiliClient(session, cookie)
    client.API = world.base
    client.COMMENT = world.base
    return client


def test_client_view_subtitles_danmaku_and_playurl():
    async def test(world, session):
        client = client_for(world, session)
        info = await client.view(VideoRef(bvid=BVID, page=2))
        assert (info.cid, info.page, info.part_title, info.duration, info.owner) == (170003, 2, "花絮", 30, "养猫的人")
        info = await client.view(VideoRef(bvid=BVID, page=9))  # 超出范围就用最后一 P
        assert info.page == 2
        info = await client.view(VideoRef(aid=170001))
        assert info.cid == 170002 and info.bvid == BVID

        assert await client.subtitles(info) == "00:01 大家好\n01:05 今天做饭"
        world.subtitle_aid = 999  # 返回了别的视频的字幕：不用
        assert await client.subtitles(info) == ""

        assert await client.hot_danmaku(info.cid) == ["哈哈哈哈", "猫：我没摔", "前排"]

        plan = await client.media_plan(info, 16)
        assert plan.video_url.endswith("/media/v.mp4") and plan.audio_url == ""
        world.playurl_mode = "dash"
        plan = await client.media_plan(info, 16)
        assert plan.video_url.endswith("/media/v.mp4") and plan.audio_url.endswith("/media/a.m4a")  # AVC、最低码率的声音
        assert world.bad_signatures == 0

        with pytest.raises(BiliError, match="不存在"):
            await client.view(VideoRef(bvid="BV1missing00"))

    run_with_world(test)


def test_short_link_follows_location_only(monkeypatch):
    monkeypatch.setattr(bili_module, "is_short_url", lambda url: "/short/" in url)

    async def test(world, session):
        client = client_for(world, session)
        assert await client.resolve(f"{world.base}/short/AbC") == VideoRef(bvid=BVID, page=2)
        assert await client.resolve("BV1xx411c7mD") == VideoRef(bvid=BVID)
        with pytest.raises(BiliError):
            await client.resolve("https://example.com/video")

    run_with_world(test)


def test_download_cap_deletes_partial_file(tmp_path):
    async def test(world, session):
        client = client_for(world, session)
        dest = tmp_path / "v.mp4"
        assert await client.download(f"{world.base}/media/v.mp4", dest, referer="https://www.bilibili.com/video/x", max_bytes=10_000) > 0
        with pytest.raises(BiliError, match="上限|太大"):
            await client.download(f"{world.base}/media/v.mp4", tmp_path / "w.mp4", referer="https://www.bilibili.com/", max_bytes=100)
        assert not (tmp_path / "w.mp4").exists()

    run_with_world(test)


def qwen_for(world: FakeWorld, session, monkeypatch, **overrides) -> QwenClient:
    monkeypatch.setitem(qwen_module.REGIONS, "beijing", world.base)
    return QwenClient(session, QwenSettings(api_key="sk-secret", **overrides))


def test_qwen_inline_video_and_untrusted_upload_host(tmp_path, monkeypatch):
    async def test(world, session):
        qwen = qwen_for(world, session, monkeypatch)
        video = tmp_path / "v.mp4"
        video.write_bytes(b"\x00" * 1000)
        assert await qwen.describe(video, "描述一下") == "画面里一只猫跳上了桌子，笑点是它摔了下来。"
        payload = world.qwen_payloads[-1]
        part = payload["messages"][1]["content"][0]
        assert payload["stream"] is True and part["type"] == "video_url"
        assert part["video_url"]["url"].startswith("data:video/mp4;base64,")
        # 大文件走临时上传；上传地址不是阿里云的就拒绝
        monkeypatch.setattr(qwen_module, "DATA_URL_LIMIT", 100)
        with pytest.raises(QwenError, match="不可信"):
            await qwen.describe(video, "描述一下")
        world.qwen_status = 400
        monkeypatch.setattr(qwen_module, "DATA_URL_LIMIT", 10 * 1024 * 1024)
        with pytest.raises(QwenError, match="Arrearage") as info:
            await qwen.describe(video, "描述一下")
        assert "sk-secret" not in str(info.value)

    run_with_world(test)


def make_watcher(world, session, monkeypatch, tmp_path, *, with_qwen=True, **settings) -> Watcher:
    qwen = qwen_for(world, session, monkeypatch) if with_qwen else None
    return Watcher(
        client_for(world, session),
        qwen,
        ResultCache(tmp_path / "cache.json"),
        WatchSettings(**settings),
        tmp_path / "tmp",
    )


def test_short_video_is_watched_once_and_cached(tmp_path, monkeypatch):
    async def test(world, session):
        watcher = make_watcher(world, session, monkeypatch, tmp_path)
        texts = await asyncio.gather(watcher.watch(BVID), watcher.watch(f"https://www.bilibili.com/video/{BVID}"))
        assert texts[0] == texts[1]
        text = texts[0]
        assert "【B站视频】《猫猫跳桌子》（P1：正片）" in text and "UP主：养猫的人" in text
        assert "笑点是它摔了下来" in text and "「哈哈哈哈」" in text
        assert len(world.qwen_payloads) == 1  # 同一个视频只看一次
        assert await watcher.watch(BVID) == text and len(world.qwen_payloads) == 1  # 缓存
        assert not any((tmp_path / "tmp").iterdir())  # 临时文件删干净了

    run_with_world(test)


def test_long_video_reads_subtitles_and_failures_fall_back(tmp_path, monkeypatch):
    async def test(world, session):
        world.duration = 900
        watcher = make_watcher(world, session, monkeypatch, tmp_path)
        text = await watcher.watch(BVID)
        assert "字幕内容" in text and "01:05 今天做饭" in text and "没有看画面" in text
        assert world.qwen_payloads == []

        # 短视频但模型出错：退回读字幕，并说明原因；不缓存，下次还能重试
        world.duration = 60
        world.qwen_status = 400
        watcher2 = make_watcher(world, session, monkeypatch, tmp_path / "w2")
        text = await watcher2.watch("BV1zz411c7mF")
        assert "这次没能看到画面" in text and "今天做饭" in text

        # 没字幕、没模型
        world.subtitles = []
        watcher3 = make_watcher(world, session, monkeypatch, tmp_path / "w3", with_qwen=False)
        text = await watcher3.watch("BV1ww411c7mG")
        assert "没有配置看视频的模型" in text and "AI 字幕需要" in text
        assert len(watcher3.cache) == 0

    run_with_world(test)


def test_long_subtitles_are_summarized_within_budget(tmp_path, monkeypatch):
    async def test(world, session):
        world.duration = 900
        world.subtitles = [{"from": i, "to": i + 1, "content": f"第{i}句很长的字幕内容"} for i in range(400)]
        watcher = make_watcher(world, session, monkeypatch, tmp_path, max_chars=1000)
        calls = []

        async def summarize(text: str, limit: int) -> str:
            calls.append(limit)
            return "这是压缩后的字幕摘要"

        watcher.summarize = summarize
        text = await watcher.watch(BVID)
        assert calls == [700] and "这是压缩后的字幕摘要" in text and len(text) <= 1000

        async def broken(text: str, limit: int) -> str:
            raise RuntimeError("模型挂了")

        watcher2 = make_watcher(world, session, monkeypatch, tmp_path / "w2", max_chars=1000)
        watcher2.summarize = broken
        text = await watcher2.watch("BV1zz411c7mF")
        assert "只保留了开头部分" in text and len(text) <= 1000

    run_with_world(test)


def test_oversized_video_is_not_downloaded(tmp_path, monkeypatch):
    async def test(world, session):
        world.video_size = 5 * 1048576
        watcher = make_watcher(world, session, monkeypatch, tmp_path, max_mb=1)
        text = await watcher.watch(BVID)
        assert "视频文件太大" in text and world.media_hits == 0

    run_with_world(test)
