"""B 站插件配置（WebUI 配置页由此自动生成）。"""

from __future__ import annotations

from typing import ClassVar, Literal

from maibot_sdk import Field, PluginConfigBase

# 配置结构有增减时提升版本：MaiBot 只在版本升高时把新字段写回已有的 config.toml
SUPPORTED_CONFIG_VERSION = "1.2.0"
PLUGIN_VERSION = "0.2.1"


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
        description="B 站登录 Cookie（旧办法）。现在推荐在和她的私聊里发 /bili_login 扫码登录她自己的账号；扫过码就不用填这里。",
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
    __ui_label__: ClassVar[str] = "看视频的模型"
    __ui_order__: ClassVar[int] = 2

    provider: Literal["百炼", "Gemini"] = Field(
        default="百炼",
        description="短视频交给谁看画面和声音。选 Gemini 时在下面「Gemini」分组里填它的 Key。从国外连北京的百炼上传很慢时，可以换 Gemini。",
        json_schema_extra={"label": "用哪家的模型", "order": 0},
    )
    api_key: str = Field(
        default="",
        description="阿里百炼的 API Key。选了百炼却不填，就只读字幕和简介，不看画面。",
        json_schema_extra={"label": "百炼 API Key", "input_type": "password", "placeholder": "sk-...", "order": 1},
    )
    region: Literal["beijing", "singapore"] = Field(
        default="beijing",
        description="百炼 API Key 所属的地域：中国大陆（北京）或国际（新加坡）。两边的 Key 不通用。",
        json_schema_extra={"label": "百炼地域", "order": 2},
    )
    model: str = Field(
        default="qwen3.5-omni-plus",
        description="能同时看画面、听声音的千问 Omni 模型名。",
        json_schema_extra={"label": "百炼模型", "placeholder": "qwen3.5-omni-plus", "order": 3},
    )
    fps: float = Field(
        default=1.0,
        ge=0.5,
        le=2.0,
        description="每秒看几帧画面（百炼和 Gemini 都用这个）。越高越细，也越贵越慢。",
        json_schema_extra={"label": "每秒帧数", "x-widget": "slider", "step": 0.5, "order": 4},
    )


class GeminiConfig(PluginConfigBase):
    __ui_label__: ClassVar[str] = "Gemini"
    __ui_order__: ClassVar[int] = 3

    api_key: str = Field(
        default="",
        description="Google AI Studio 的 Gemini API Key。只有「用哪家的模型」选了 Gemini 才用。",
        json_schema_extra={"label": "Gemini API Key", "input_type": "password", "placeholder": "AIza...", "order": 0},
    )
    model: str = Field(
        default="gemini-3.5-flash",
        description="Gemini 的模型名。",
        json_schema_extra={"label": "Gemini 模型", "placeholder": "gemini-3.5-flash", "order": 1},
    )
    resolution: Literal["低", "标准"] = Field(
        default="低",
        description="每帧画面看多细。「低」更省钱，一般看懂发生了什么够用；画面上的小字看不清时选「标准」（大约贵三倍）。",
        json_schema_extra={"label": "画面细节", "order": 2},
    )


class WatchConfig(PluginConfigBase):
    __ui_label__: ClassVar[str] = "看视频"
    __ui_order__: ClassVar[int] = 4

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
        description="把视频发给模型（直接放进请求或先上传）最多等多少秒。从国外连北京的百炼会很慢。",
        json_schema_extra={"label": "上传超时（秒）", "order": 7},
    )
    model_timeout: int = Field(
        default=300,
        ge=30,
        le=600,
        description="视频发过去以后，模型看视频最多等多少秒（不含下载和发送）。几分钟的视频，模型可能要想两三分钟。",
        json_schema_extra={"label": "模型超时（秒）", "order": 8},
    )


class AccountConfig(PluginConfigBase):
    __ui_label__: ClassVar[str] = "她的账号"
    __ui_order__: ClassVar[int] = 5

    like: bool = Field(default=True, description="允许她给喜欢的视频点赞。", json_schema_extra={"label": "点赞", "order": 0})
    coin: bool = Field(default=True, description="允许她投币（用她自己账号的硬币）。", json_schema_extra={"label": "投币", "order": 1})
    favorite: bool = Field(default=True, description="允许她收藏到自己的默认收藏夹。", json_schema_extra={"label": "收藏", "order": 2})
    later: bool = Field(default=True, description="允许她加「稍后再看」。", json_schema_extra={"label": "稍后再看", "order": 3})
    follow: bool = Field(default=True, description="允许她关注喜欢的 UP 主（对首页推荐影响很大）。", json_schema_extra={"label": "关注 UP 主", "order": 4})
    history: bool = Field(
        default=True,
        description="她看完视频后给账号留下观看记录，首页推荐会慢慢变成她的口味。",
        json_schema_extra={"label": "留下观看记录", "order": 5},
    )
    requests_per_day: int = Field(
        default=600, ge=50, le=5000, description="她的账号每天最多发多少个请求（逛、看评论、各种操作合计）。", json_schema_extra={"label": "每天请求上限", "order": 6}
    )
    likes_per_day: int = Field(default=30, ge=0, le=200, description="每天最多点赞几次。", json_schema_extra={"label": "每天点赞上限", "order": 7})
    coins_per_day: int = Field(default=2, ge=0, le=10, description="每天最多投几个币。", json_schema_extra={"label": "每天投币上限", "order": 8})
    favorites_per_day: int = Field(default=15, ge=0, le=100, description="每天最多收藏几个。", json_schema_extra={"label": "每天收藏上限", "order": 9})
    follows_per_day: int = Field(default=5, ge=0, le=50, description="每天最多关注几个 UP 主。", json_schema_extra={"label": "每天关注上限", "order": 10})


class BrowseConfig(PluginConfigBase):
    __ui_label__: ClassVar[str] = "逛 B 站"
    __ui_order__: ClassVar[int] = 6

    list_count: int = Field(default=12, ge=5, le=30, description="首页推荐、热门、搜索一次给她看几条。", json_schema_extra={"label": "每次几条", "order": 0})
    max_minutes: int = Field(default=40, ge=1, le=600, description="比这更长的视频不出现在推荐和热门里（搜索不限）。", json_schema_extra={"label": "视频时长上限（分钟）", "order": 1})
    comment_count: int = Field(default=12, ge=3, le=30, description="看评论区一次看几条（每条最多带两条回复）。", json_schema_extra={"label": "每次几条评论", "order": 2})
    comment_images: int = Field(
        default=3, ge=0, le=6, description="评论区一次最多让她看几张图（看到喜欢的可以收藏成表情包）。0 表示不看图。", json_schema_extra={"label": "每次几张评论图", "order": 3}
    )
    blocked_keywords: list[str] = Field(default_factory=list, description="标题里带这些词的视频不出现在推荐和热门里。", json_schema_extra={"label": "屏蔽关键词", "order": 4})
    blocked_ups: list[str] = Field(default_factory=list, description="这些 UP 主的视频不出现在推荐、热门和搜索里。", json_schema_extra={"label": "屏蔽 UP 主", "order": 5})


class BilibiliVideoConfig(PluginConfigBase):
    plugin: PluginOptions = Field(default_factory=PluginOptions)
    bilibili: BilibiliConfig = Field(default_factory=BilibiliConfig)
    qwen: QwenConfig = Field(default_factory=QwenConfig)
    gemini: GeminiConfig = Field(default_factory=GeminiConfig)
    watch: WatchConfig = Field(default_factory=WatchConfig)
    account: AccountConfig = Field(default_factory=AccountConfig)
    browse: BrowseConfig = Field(default_factory=BrowseConfig)
