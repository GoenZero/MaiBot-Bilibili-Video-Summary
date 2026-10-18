"""把插件放进假的 MaiBot 环境：收到链接预取、AI 调工具、状态命令。"""

from __future__ import annotations

from typing import Any

import asyncio
import copy

import pytest
from maibot_sdk.context import PluginContext, PluginPaths

import bili_video_under_test as plugin_module
from bili_video_under_test.core import qwen as qwen_module
from bili_video_under_test.core.bilibili import BiliClient

from fake_servers import BVID, FakeWorld, start

CONFIG = {
    "plugin": {"enabled": True, "config_version": "1.0.0"},
    "bilibili": {"cookie": "SESSDATA=topsecretcookie; bili_jct=x", "quality": "360P"},
    "qwen": {"api_key": "sk-topsecret", "region": "beijing", "model": "qwen3.5-omni-plus", "fps": 1.0},
    "watch": {"short_minutes": 5, "prefetch": True, "max_chars": 2000, "danmaku": True, "summary_task": "utils"},
}


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)


class FakeHost:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def rpc(self, method: str, plugin_id: str, payload: dict[str, Any], timeout_ms: int | None = None) -> Any:
        self.calls.append((method, payload))
        if method == "cap.call" and payload["capability"] == "llm.generate":
            return {"success": True, "response": "压缩后的字幕"}
        return {"success": True}

    def sent_texts(self) -> list[dict[str, Any]]:
        return [p["args"] for name, p in self.calls if name == "cap.call" and p["capability"] == "send.text"]


def make_plugin(tmp_path, host: FakeHost, config: dict | None = None) -> Any:
    plugin = plugin_module.create_plugin()
    plugin._set_context(PluginContext("com.goenzero.bilibili_video", host.rpc, PluginPaths(data_dir=tmp_path / "data", runtime_dir=tmp_path / "rt")))
    plugin.set_plugin_config(copy.deepcopy(config or CONFIG))
    return plugin


def with_world(monkeypatch, test):
    async def main():
        world = FakeWorld()
        server = await start(world)
        monkeypatch.setattr(BiliClient, "API", world.base)
        monkeypatch.setattr(BiliClient, "COMMENT", world.base)
        monkeypatch.setattr(BiliClient, "WWW", world.base)
        monkeypatch.setitem(qwen_module.REGIONS, "beijing", world.base)
        try:
            await test(world)
        finally:
            await server.close()

    asyncio.run(main())


def test_prefetch_then_tool_returns_the_same_result(tmp_path, monkeypatch):
    host = FakeHost()
    plugin = make_plugin(tmp_path, host)

    async def test(world):
        await plugin.on_load()
        assert plugin._watcher.qwen.settings.upload_timeout == 300  # 默认放宽到 300 秒，可在 WebUI 调
        message = {"raw_message": [{"type": "text", "data": f"[链接] https://www.bilibili.com/video/{BVID}"}]}
        await plugin.on_inbound(message=message)
        result = await plugin.tool_watch(url=f"看看这个 https://www.bilibili.com/video/{BVID}", stream_id="s1")
        assert result["success"] is True and "笑点是它摔了下来" in result["content"]
        assert len(world.qwen_payloads) == 1  # 预取和工具调用合并成一次
        # 百炼的请求里没有 B 站 Cookie，B 站的请求里没有百炼密钥（各用各的）
        assert "topsecretcookie" not in str(world.qwen_payloads)
        await plugin.on_unload()

    with_world(monkeypatch, test)


def test_bad_link_and_disabled(tmp_path, monkeypatch):
    host = FakeHost()
    plugin = make_plugin(tmp_path, host)

    async def test(world):
        await plugin.on_load()
        bad = await plugin.tool_watch(url="https://example.com/x")
        assert bad["success"] is False and "没看成" in bad["error"]
        missing = await plugin.tool_watch(url="BV1missing00")
        assert missing["success"] is False and "不存在" in missing["error"]
        data = copy.deepcopy(CONFIG)
        data["plugin"]["enabled"] = False
        plugin.set_plugin_config(data)
        await plugin.on_config_update("self", data, "")
        off = await plugin.tool_watch(url=BVID)
        assert off["success"] is False and "关着" in off["error"]
        await plugin.on_inbound(message={"raw_message": [{"type": "text", "data": BVID}]})
        await plugin.on_unload()

    with_world(monkeypatch, test)


def test_long_video_uses_maibot_text_model_for_long_subtitles(tmp_path, monkeypatch):
    host = FakeHost()
    data = copy.deepcopy(CONFIG)
    data["watch"]["max_chars"] = 800
    plugin = make_plugin(tmp_path, host, data)

    async def test(world):
        world.duration = 1200
        world.subtitles = [{"from": i, "to": i + 1, "content": f"第{i}句字幕"} for i in range(300)]
        await plugin.on_load()
        result = await plugin.tool_watch(url=BVID)
        assert result["success"] is True and "压缩后的字幕" in result["content"]
        llm = [p for name, p in host.calls if name == "cap.call" and p["capability"] == "llm.generate"]
        assert llm and llm[0]["args"]["task_name"] == "utils"
        prompt = llm[0]["args"]["prompt"]
        assert "按这些分段逐段概括" in prompt and "00:00 开场 / 01:05 开始 做饭" in prompt
        assert "分段：00:00 开场" in result["content"]

        # 没有分段的视频：按时间分段概括，每段也带时间
        world.view_points = []
        result = await plugin.tool_watch(url="BV1zz411c7mF")
        prompt = [p for name, p in host.calls if name == "cap.call" and p["capability"] == "llm.generate"][-1]["args"]["prompt"]
        assert "视频没有分段" in prompt and "行首写这一段的开始时间" in prompt
        assert "分段：" not in result["content"]
        assert world.qwen_payloads == []
        await plugin.on_unload()

    with_world(monkeypatch, test)


def test_status_is_not_stored_and_hides_secrets(tmp_path, monkeypatch):
    host = FakeHost()
    plugin = make_plugin(tmp_path, host)

    async def test(world):
        await plugin.on_load()
        await plugin.handle_status(stream_id="s1")
        sent = host.sent_texts()[-1]
        assert sent["storage_message"] is False
        assert "Cookie 已失效" in sent["text"]  # 假服务器总说没登录
        assert "topsecret" not in sent["text"]
        await plugin.on_unload()

    with_world(monkeypatch, test)


def test_tool_reads_a_time_range(tmp_path, monkeypatch):
    host = FakeHost()
    plugin = make_plugin(tmp_path, host)

    async def test(world):
        world.duration = 1200
        world.subtitles = [{"from": m * 60, "to": m * 60 + 5, "content": f"第{m}分钟"} for m in range(20)]
        await plugin.on_load()
        result = await plugin.tool_watch(url=BVID, start="06:30", end="10:50")
        assert result["success"] is True and "07:00 第7分钟" in result["content"] and "11:00" not in result["content"]
        only_start = await plugin.tool_watch(url=BVID, start="10分钟")
        assert "10:00–15:00" in only_start["content"]
        bad = await plugin.tool_watch(url=BVID, start="半路")
        assert bad["success"] is False and "格式" in bad["error"]
        await plugin.on_unload()

    with_world(monkeypatch, test)
