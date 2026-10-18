# MaiBot-Bilibili-Video-Summary

**让 MaiBot 能「看」B 站视频的插件**

[![License](https://img.shields.io/badge/license-GPL--3.0-green.svg)](LICENSE)
[![MaiBot](https://img.shields.io/badge/MaiBot-1.3.5%2B-blue)](https://github.com/Mai-with-u/MaiBot)

你发一个 B 站链接，AI 可以像自己看过一样和你聊这个视频：

- **短视频**：交给阿里百炼的千问 Omni 模型看画面、听声音，知道发生了什么、说了什么、笑点在哪。
- **长视频**：读字幕，每段都带视频里的时间；UP 主标了分段的，按分段整理。字幕太长时先压缩。
- 都附上标题、UP 主、简介和热门弹幕。

## 设计思路

- **由 AI 决定看不看**：插件给 AI 一个「看视频」工具，和其他工具一样由她自己决定用不用。
- **不让人干等**：收到 B 站链接时插件先在后台看起来，等她想看的时候，结果多半已经好了。
- **不会被长视频卡死**：只有短视频才下载给模型看；下载、上传、模型处理各有各的超时，失败了就如实告诉她没看成，并退回去读字幕。
- **省钱**：默认下载 360P；同一个视频看过就缓存 7 天，你重复发不会重复花钱；交给 AI 的内容默认最多 2000 字。
- **树莓派友好**：下载前先检查文件大小，边下边写临时文件，不把整个视频读进内存，用完就删。
- **不污染记忆**：视频内容以工具结果的形式交给 AI，不会被当成你说的话学进记忆。她对视频的评价是她自己说的话，会正常进入记忆。

## 安装

把本仓库放到 MaiBot 的 `plugins/` 目录下，重启 MaiBot：

```sh
cd /path/to/MaiBot/plugins
git clone https://github.com/GoenZero/MaiBot-Bilibili-Video-Summary.git
```

要求 MaiBot 1.3.5 及以上（插件 SDK 2.10 及以上）。依赖 aiohttp，MaiBot 本身已经带了。已在 MaiBot 1.3.9 上验证能加载。

## 配置

在 WebUI 的插件配置页填写，改了立即生效。

| 分组 | 设置 | 默认 | 说明 |
|---|---|---|---|
| B 站 | 登录 Cookie | （空） | 只有读 **AI 字幕**需要它，短视频不需要。建议用小号 |
| B 站 | 下载清晰度 | 360P | 也可以选 480P，文件更大、更贵 |
| 看视频的模型 | API Key | （空） | 阿里百炼的 Key。不填就只读字幕和简介 |
| 看视频的模型 | 地域 | 北京 | Key 所属地域：中国大陆（北京）或国际（新加坡），两边的 Key 不通用 |
| 看视频的模型 | 模型 | qwen3.5-omni-plus | 能同时看画面、听声音的千问 Omni 模型 |
| 看视频的模型 | 每秒帧数 | 1 | 滑块 0.5–2，越高越细也越贵 |
| 看视频 | 看画面的视频时长上限 | 5 分钟 | 滑块 1–30 分钟，更长的只读字幕 |
| 看视频 | 收到链接先在后台看 | 开 | |
| 看视频 | 交给 AI 的字数上限 | 2000 | 滑块 800–4000 |
| 看视频 | 附上热门弹幕 | 开 | |
| 看视频 | 压缩字幕用的模型任务 | utils | MaiBot「模型配置」里的任务名。压缩时按 UP 主标的分段逐段概括，每段带开始时间 |
| 看视频 | 视频大小上限 / 下载超时 / 上传超时 / 模型超时 | 60MB / 120 秒 / 300 秒 / 180 秒 | 视频超过约 7MB 时要先传到百炼的临时空间，从国外连北京的百炼会比较慢，日志里能看到上传速度 |

### 怎么获取 B 站登录 Cookie

1. 用电脑浏览器登录 B 站（建议用小号）。
2. 按 F12 打开开发者工具，切到「网络（Network）」。
3. 刷新页面，随便点一个发往 `bilibili.com` 的请求，在「请求标头」里找到 `Cookie`。
4. 把整段值复制，粘贴到插件配置的「登录 Cookie」里。只粘 `SESSDATA=` 后面的值也可以。

Cookie 过一段时间会过期。发 `/bili_status` 可以看到登录是否还有效，失效了重新粘贴一次就行。

> Cookie 和 API Key 相当于账号密码，只在 WebUI 里填写。插件不会把它们写进日志或回复。

## AI 能用的工具

| 工具 | 作用 |
|---|---|
| `bilibili_watch` | 看一个 B 站视频。认得视频页链接、`b23.tv` 等短链接、BV 号、分 P。填了 `start`、`end`（例如 06:30 到 10:50）时只读这段时间的字幕原文 |

如果你还给 AI 配了网页抓取工具（例如 Firecrawl），工具说明里已经告诉她 B 站链接用这个插件：网页抓取只能拿到标题、简介这类页面文字，看不到视频本身。

## 管理命令

| 命令 | 说明 |
|---|---|
| `/bili_status` | 查看 B 站登录是否有效、看画面的模型配置、缓存了多少个视频、最近一次失败的原因 |

## 已知限制

- 用到的都是 B 站网页自己用的接口，不是正式开放接口；B 站改了就要跟着修。
- B 站有风控，请求太频繁会被暂时拦下（填了 Cookie 也一样）。插件被拦后会先换成不带登录信息再试，再不行就改从视频网页读；字幕只能从接口读，被拦时就读不到。同一时间别让好几个插件一起去抓同一个视频。
- 从国外连北京的百炼，上传几十 MB 的视频可能要好几分钟。日志里写了上传速度；太慢的话可以调大「上传超时」，或者改用国际版（新加坡）的百炼 Key。
- 「只看一段」只能读这段的字幕，看不到这段的画面。
- 长视频没有字幕时，只能看到标题、简介和弹幕。听音频转文字计划放在以后的版本。
- 纯画面、没有人说话的长视频（风景、舞蹈等）只能大概知道内容。
- 只支持普通视频，不支持番剧、直播、专栏。

## 开发

```sh
pip install maibot-plugin-sdk aiohttp pytest
python -m pytest tests
```

测试用本地的假 B 站和假百炼服务器，不连外网，也不需要任何密钥。

## 贡献者

| 贡献者 | 做了什么 |
|---|---|
| [GoenZero](https://github.com/GoenZero) | 发起与维护：提出需求，真机测试与验收 |
| Claude（Anthropic） | 开发：设计、编写代码、测试和文档 |

参考了 [maibot-qwen-video](https://github.com/socialistearth/maibot-qwen-video)（MIT 许可，许可证见 [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt)），以及 [astrbot_plugin_biliread](https://github.com/SodaCodeSave/astrbot_plugin_biliread)、[astrbot-plugin-bilibili-video-summary](https://github.com/RankoP-114/astrbot-plugin-bilibili-video-summary) 的思路（没有使用这两个插件的代码）。

## 许可证

GPL-3.0，见 [LICENSE](LICENSE)。
