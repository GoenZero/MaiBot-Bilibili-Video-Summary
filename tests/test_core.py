from __future__ import annotations

from pathlib import Path

import asyncio
import json

import aiohttp
import pytest

from bili_video_under_test.core import bilibili as bili_module
from bili_video_under_test.core import qwen as qwen_module
from bili_video_under_test.core.bilibili import BiliClient, BiliError, line_seconds, mixin_key, parse_cookie, parse_time, sign_wbi
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

        text, chapters = await client.subtitles(info)
        assert text == "00:01 大家好\n01:05 今天做饭"
        assert [(c.start, c.title) for c in chapters] == [(0, "开场"), (65, "开始 做饭")]  # 按时间排好
        world.view_points = [{"from": "坏数据"}, "也不对", {"from": 3, "content": ""}]
        assert (await client.subtitles(info))[1] == []
        world.subtitle_aid = 999  # 返回了别的视频的信息：不用
        assert await client.subtitles(info) == ("", [])

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
        assert "分段：00:00 开场 / 01:05 开始 做饭" in text
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
        assert "分段：00:00 开场" in text  # 没字幕也能看到分段
        assert len(watcher3.cache) == 0

    run_with_world(test)


def test_long_subtitles_are_summarized_within_budget(tmp_path, monkeypatch):
    async def test(world, session):
        world.duration = 900
        world.subtitles = [{"from": i, "to": i + 1, "content": f"第{i}句很长的字幕内容"} for i in range(400)]
        watcher = make_watcher(world, session, monkeypatch, tmp_path, max_chars=1000)
        calls = []

        async def summarize(text: str, limit: int, chapters: str) -> str:
            calls.append(limit)
            assert chapters == "00:00 开场 / 01:05 开始 做饭"
            return "这是压缩后的字幕摘要"

        watcher.summarize = summarize
        text = await watcher.watch(BVID)
        assert calls == [700] and "这是压缩后的字幕摘要" in text and len(text) <= 1000

        async def broken(text: str, limit: int, chapters: str) -> str:
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


def test_parse_time_formats():
    assert parse_time("06:30") == 390 and parse_time("6:30") == 390 and parse_time("1:02:30") == 3750
    assert parse_time("390") == 390 and parse_time("6分30秒") == 390 and parse_time("10分钟") == 600
    assert parse_time("1小时2分") == 3720
    assert parse_time("") is None and parse_time("半路") is None and parse_time("6:3x") is None
    assert line_seconds("06:30 内容") == 390 and line_seconds("没有时间的行") is None


def test_blocked_view_falls_back_to_the_video_page():
    async def test(world, session):
        client = client_for(world, session)
        client.WWW = world.base
        world.block_view = 99
        info = await client.view(VideoRef(bvid=BVID))
        assert (info.cid, info.title, info.owner) == (170002, "猫猫跳桌子", "养猫的人")
        assert world.view_hits == 1 and world.page_hits == 1
        # 自动向 B 站要了设备标识
        assert "buvid3=REAL-BUVID3" in world.cookies_seen[-1] and "buvid4=REAL-BUVID4" in world.cookies_seen[-1]
        # 接口暂停中：不再请求接口，直接走网页
        assert (await client.view(VideoRef(bvid=BVID))).cid == 170002
        assert world.view_hits == 1 and world.page_hits == 2 and client.blocked_seconds > 0

        # 接口和网页都被拦：如实报错，日志里写清楚两条路都失败了
        world.block_page = True
        with pytest.raises(BiliError, match="请求太频繁|秒后再试") as info:
            await client.view(VideoRef(bvid=BVID))
        assert "网页后备也失败" in info.value.detail

    run_with_world(test)


def test_blocked_with_login_retries_without_login():
    async def test(world, session):
        client = client_for(world, session, cookie="SESSDATA=secret; bili_jct=j; buvid3=MINE")
        world.block_logged_in = True
        info = await client.view(VideoRef(bvid=BVID))
        assert info.cid == 170002 and world.page_hits == 0
        assert "SESSDATA" not in world.cookies_seen[-1] and "bili_jct" not in world.cookies_seen[-1]
        assert "buvid3=MINE" in world.cookies_seen[-1]  # 设备标识照样带着

    run_with_world(test)


def test_blocked_playurl_falls_back_to_the_video_page():
    async def test(world, session):
        client = client_for(world, session)
        client.WWW = world.base
        info = await client.view(VideoRef(bvid=BVID))
        world.block_playurl = True
        plan = await client.media_plan(info, 16)
        assert plan.video_url.endswith("/media/v.mp4") and world.page_hits == 1

    run_with_world(test)


def test_user_cookie_device_id_is_kept():
    async def test(world, session):
        client = client_for(world, session, cookie="SESSDATA=s; buvid3=MINE")
        await client.view(VideoRef(bvid=BVID))
        assert "buvid3=MINE" in world.cookies_seen[-1] and "REAL-BUVID3" not in world.cookies_seen[-1]

    run_with_world(test)


def test_segment_returns_only_that_time_range(tmp_path, monkeypatch):
    async def test(world, session):
        world.duration = 1200
        world.subtitles = [{"from": m * 60, "to": m * 60 + 5, "content": f"第{m}分钟的话"} for m in range(20)]
        watcher = make_watcher(world, session, monkeypatch, tmp_path)
        text = await watcher.segment(BVID, 390, 650)
        assert "06:30–10:50 这一段的字幕" in text
        assert "07:00 第7分钟的话" in text and "10:00 第10分钟的话" in text
        assert "06:00" not in text and "11:00" not in text
        # 第二次问别的段落：字幕已缓存，不再向 B 站要
        hits = world.player_hits
        text = await watcher.segment(BVID, 0, 120)
        assert "00:00 第0分钟的话" in text and "01:00 第1分钟的话" in text
        assert world.player_hits == hits
        with pytest.raises(BiliError, match="没有字幕"):
            await watcher.segment(BVID, 1190, 1300)  # 结束时间超过视频长度会被截到结尾
        with pytest.raises(BiliError, match="时间段不对"):
            await watcher.segment(BVID, 1500, 1600)
        world.subtitles = []
        watcher2 = make_watcher(world, session, monkeypatch, tmp_path / "w2")
        with pytest.raises(BiliError, match="没法单独看某一段"):
            await watcher2.segment(BVID, 0, 60)

    run_with_world(test)


def test_slow_model_is_not_cut_off_by_the_session_read_limit(tmp_path, monkeypatch):
    """真机：会话默认「60 秒没数据就断」比模型超时短，百炼还在看视频就被切断了。"""

    async def main():
        world = FakeWorld()
        server = await start(world)
        monkeypatch.setitem(qwen_module.REGIONS, "beijing", world.base)
        # 会话默认 0.2 秒没数据就断（相当于插件里的 60 秒），模型 0.5 秒后才开始回
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_read=0.2))
        try:
            world.qwen_delay = 0.5
            video = tmp_path / "v.mp4"
            video.write_bytes(b"\x00" * 100)
            qwen = QwenClient(session, QwenSettings(api_key="k", model_timeout=5))
            assert "猫" in await qwen.describe(video, "描述")
            # 超过模型超时：说清楚是等太久，而不是连不上
            slow = QwenClient(session, QwenSettings(api_key="k", model_timeout=0.2))
            with pytest.raises(QwenError, match="没有回音|超过"):
                await slow.describe(video, "描述")
        finally:
            await session.close()
            await server.close()

    asyncio.run(main())


def test_short_video_still_watched_when_api_is_blocked(tmp_path, monkeypatch):
    async def test(world, session):
        watcher = make_watcher(world, session, monkeypatch, tmp_path)
        watcher.bili.WWW = world.base
        world.block_view = 99
        world.block_playurl = True
        text = await watcher.watch(BVID)
        assert "笑点是它摔了下来" in text and world.page_hits == 2  # 视频信息、播放地址都从网页拿到

    run_with_world(test)


def test_large_video_is_uploaded_in_an_oss_compatible_form(tmp_path, monkeypatch):
    """真机：aiohttp 自带表单给文本字段加了 Content-Type，阿里云 OSS 返回 HTTP 400。"""

    async def test(world, session):
        qwen = qwen_for(world, session, monkeypatch)
        world.upload_policy_host = f"{world.base}/oss"
        monkeypatch.setattr(qwen_module, "is_trusted_upload_host", lambda url: url.startswith(world.base))
        monkeypatch.setattr(qwen_module, "DATA_URL_LIMIT", 100)  # 让 1000 字节的文件也走上传
        video = tmp_path / "BV1xx.mp4"
        video.write_bytes(b"\x01" * 1000)
        assert "猫" in await qwen.describe(video, "描述")
        upload = world.oss_uploads[-1]
        assert upload["size"] == 1000 and upload["fields"]["key"] == "dashscope-instant/abc/BV1xx.mp4"
        assert upload["fields"]["success_action_status"] == "200"
        part = world.qwen_payloads[-1]["messages"][1]["content"][0]
        assert part["video_url"]["url"] == "oss://dashscope-instant/abc/BV1xx.mp4"
        assert world.qwen_headers[-1].get("X-DashScope-OssResourceResolve") == "enable"

    run_with_world(test)


def test_oss_error_reason_is_reported(tmp_path, monkeypatch):
    async def test(world, session):
        qwen = qwen_for(world, session, monkeypatch)
        world.upload_policy_host = f"{world.base}/oss"
        world.upload_dir = "somewhere-else"  # 假 OSS 会拒绝，并给出 XML 错误说明
        monkeypatch.setattr(qwen_module, "is_trusted_upload_host", lambda url: url.startswith(world.base))
        monkeypatch.setattr(qwen_module, "DATA_URL_LIMIT", 100)
        video = tmp_path / "v.mp4"
        video.write_bytes(b"\x01" * 1000)
        with pytest.raises(QwenError, match="HTTP 400 InvalidArgument missing file or key"):
            await qwen.describe(video, "描述")

    run_with_world(test)
