"""B 站视频插件配置（WebUI 配置页由此自动生成）。"""

from __future__ import annotations

from typing import ClassVar, Literal

from maibot_sdk import Field, PluginConfigBase

# 配置结构有增减时提升版本：MaiBot 只在版本升高时把新字段写回已有的 config.toml
SUPPORTED_CONFIG_VERSION = "1.0.0"
PLUGIN_VERSION = "0.1.0"


class PluginOptions(PluginConfigBase):
    __ui_label__: ClassVar[str] = "插件设置"
    __ui_order__: ClassVar[int] = 0

    enabled: bool = Field(default=True, description="是否启用。", json_schema_extra={"label": "启用", "order": 0})
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="当前配置结构版本。",
        json_schema_extra={"label": "配置版本", "disabled": True, "hidden": True, "order": 99},
    )


class BilibiliConfig(PluginConfigBase):
    __ui_label__: ClassVar[str] = "B 站"
    __ui_order__: ClassVar[int] = 1

    cookie: str = Field(
        default="",
        description="B 站登录 Cookie。只有读 AI 字幕需要它；不填也能看短视频。建议用小号。",
        json_schema_extra={
            "label": "登录 Cookie",
            "input_type": "password",
            "placeholder": "SESSDATA=...; bili_jct=...; buvid3=...",
            "hint": "电脑浏览器登录 B 站后按 F12 → 网络（Network）→ 随便点一个请求 → 请求标头里的 Cookie，整段复制粘贴到这里。只粘 SESSDATA 的值也可以。",
            "order": 0,
        },
    )
    quality: Literal["360P", "480P"] = Field(
        default="360P",
        description="短视频下载给模型看的清晰度。360P 文件更小、更快、更省钱，看清画面一般够用。",
        json_schema_extra={"label": "下载清晰度", "order": 1},
    )


class QwenConfig(PluginConfigBase):
    __ui_label__: ClassVar[str] = "看视频的模型（阿里百炼）"
    __ui_order__: ClassVar[int] = 2

    api_key: str = Field(
        default="",
        description="阿里百炼的 API Key。不填就只读字幕和简介，不看画面。",
        json_schema_extra={"label": "API Key", "input_type": "password", "placeholder": "sk-...", "order": 0},
    )
    region: Literal["beijing", "singapore"] = Field(
        default="beijing",
        description="API Key 所属的地域：中国大陆（北京）或国际（新加坡）。两边的 Key 不通用。",
        json_schema_extra={"label": "地域", "order": 1},
    )
    model: str = Field(
        default="qwen3.5-omni-plus",
        description="能同时看画面、听声音的千问 Omni 模型名。",
        json_schema_extra={"label": "模型", "placeholder": "qwen3.5-omni-plus", "order": 2},
    )
    fps: float = Field(
        default=1.0,
        ge=0.5,
        le=2.0,
        description="每秒看几帧画面。越高越细，也越贵越慢。",
        json_schema_extra={"label": "每秒帧数", "x-widget": "slider", "step": 0.5, "order": 3},
    )


class WatchConfig(PluginConfigBase):
    __ui_label__: ClassVar[str] = "看视频"
    __ui_order__: ClassVar[int] = 3

    short_minutes: int = Field(
        default=5,
        ge=1,
        le=30,
        description="多长以内的视频交给模型看画面和声音；更长的只读字幕。",
        json_schema_extra={"label": "看画面的视频时长上限（分钟）", "x-widget": "slider", "step": 1, "order": 0},
    )
    prefetch: bool = Field(
        default=True,
        description="收到 B 站链接就先在后台开始看，等 AI 想看时结果多半已经好了。",
        json_schema_extra={"label": "收到链接先在后台看", "order": 1},
    )
    max_chars: int = Field(
        default=2000,
        ge=800,
        le=4000,
        description="交给 AI 的视频内容最多多少字。",
        json_schema_extra={"label": "交给 AI 的字数上限", "x-widget": "slider", "step": 100, "order": 2},
    )
    danmaku: bool = Field(
        default=True,
        description="附上出现次数最多的几条弹幕，让 AI 知道大家的反应。",
        json_schema_extra={"label": "附上热门弹幕", "order": 3},
    )
    summary_task: str = Field(
        default="utils",
        description="字幕太长时，用 MaiBot「模型配置」里的哪个任务来压缩（填任务名）。",
        json_schema_extra={"label": "压缩字幕用的模型任务", "placeholder": "utils", "order": 4},
    )
    max_mb: int = Field(
        default=60,
        ge=5,
        le=500,
        description="下载视频的大小上限（MB），超过就不看画面。",
        json_schema_extra={"label": "视频大小上限（MB）", "order": 5},
    )
    download_timeout: int = Field(
        default=120,
        ge=10,
        le=600,
        description="下载视频最多等多少秒。",
        json_schema_extra={"label": "下载超时（秒）", "order": 6},
    )
    upload_timeout: int = Field(
        default=300,
        ge=30,
        le=900,
        description="把视频发给百炼（直接放进请求或传到临时空间）最多等多少秒。从国外连北京的百炼会比较慢。",
        json_schema_extra={"label": "上传超时（秒）", "order": 7},
    )
    model_timeout: int = Field(
        default=300,
        ge=30,
        le=600,
        description="视频发过去以后，百炼看视频最多等多少秒（不含下载和发送）。几分钟的视频，模型可能要想两三分钟。",
        json_schema_extra={"label": "模型超时（秒）", "order": 8},
    )


class BilibiliVideoConfig(PluginConfigBase):
    plugin: PluginOptions = Field(default_factory=PluginOptions)
    bilibili: BilibiliConfig = Field(default_factory=BilibiliConfig)
    qwen: QwenConfig = Field(default_factory=QwenConfig)
    watch: WatchConfig = Field(default_factory=WatchConfig)
