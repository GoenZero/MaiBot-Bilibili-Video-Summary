# 给接手的 Claude 的说明

这个仓库是 MaiBot 的 B 站视频插件，开发在 `develop` 分支，用户在真机上确认没问题后才合并到 `main`。

## 用户和沟通方式

- 用户不是程序员。**只用简体中文、用大白话交流**，不要大段英文，少用术语。
- 用户的 AI 角色叫「大肥鱼」，跑在 MaiBot 上，只通过 iMessage 和用户一对一私聊（不用考虑群聊和别的适配器）。
- 用户人在日本。MaiBot 1.3.9 跑在树莓派的 Docker 里。用户的 Mac 配好了连树莓派的 SSH 密钥。

## 必须遵守

- **不要读取、打印、记录或提交任何密钥**：B 站 Cookie、百炼 API Key、Gemini API Key、Photon 的 Project Secret 等。它们只在 WebUI 或配置文件里由用户自己填。看日志时也不要把它们贴出来。
- **不要改 MaiBot 本身或 Docker 配置**，除非先解释清楚、用户同意。
- **删文件、重启容器、改系统设置之前先问用户。** 只读的操作（看日志、看状态）可以直接做。
- 保留 GPL-3.0 许可证和原作者署名；参考过的 MIT 代码的许可证在 `THIRD_PARTY_NOTICES.txt`。不要抄 AGPL 的代码。
- 提交说明和 PR 里不要写模型型号。推送失败（网络问题）按 2、4、8、16 秒间隔重试。
- 稳定优先，改动要小，改完先跑测试。

## 开发和测试

```sh
pip install maibot-plugin-sdk aiohttp pytest
python -m pytest tests          # 本地假的 B 站、百炼、Gemini 服务器，不连外网、不要密钥
```

插件要同时兼容插件 SDK 2.10 和 2.12（MaiBot 1.3.5 起）。

## 真机测试的流程

1. 推送到 `develop` 后，让用户在 MaiBot 的 `plugins/MaiBot-Bilibili-Video-Summary` 目录里 `git pull origin develop`，再重启 MaiBot（重启要用户同意）。
2. 在 iMessage 里给大肥鱼发一个 B 站短视频链接。
3. 看 MaiBot 日志里以「B站视频：」开头的行；也可以让用户发 `/bili_status`，里面有最近一次下载、发送的大小和用时，以及最近一次失败的原因。
   用 SSH 看日志时，可以用 `docker logs <容器名> 2>&1 | grep "B站视频"`（容器名先问用户或用 `docker ps` 看）。

## 现在的进度（2026-10-18）

- B 站这边已经稳定：视频信息接口常被风控（HTTP 412）拦下，会自动改从网页读；下载 8MB 只要一两秒。
- **卡住的问题**：从日本往北京的百炼发视频极慢。真机实测 11.2MB 的请求 10 分钟只发出 8MB（每秒十几 KB）；传到百炼临时空间（OSS）同样慢。这不是代码问题，是线路问题。
- 为此加了 **Gemini** 作为可选的看视频模型（WebUI「看视频的模型 → 用哪家的模型」选 Gemini，再在「Gemini」分组里填 Key）。**还没在真机上测过**，下一步就是测它：
  - 期望日志：「下载完成」→「视频已发给Gemini，直接发送 xxMB 用了 x 秒」→「看完了（BVxxx，看了画面）」。
  - Gemini 的模型名默认 `gemini-3.5-flash`；如果报「模型不存在」之类的错，让用户在 AI Studio 里看可用的模型名，在 WebUI 里改。
  - 接口格式是按 Gemini REST API（`v1beta/models/{model}:generateContent`，`inlineData`/`fileData`，`videoMetadata.fps`，`generationConfig.mediaResolution`，Files API 可续传上传）写的，开发时查不到最新官方文档，真机报错就按错误信息对照官方文档修。
- 另一个办法是用户换国际版（新加坡）的百炼 Key，插件已支持（「百炼地域」选新加坡）。

## 同一个用户的另外两个仓库

- `GoenZero/MaiBot-iMessage-Adapter`：iMessage 适配器。0.2.1（适配 MaiBot 1.3.9 的 account_id、启动时等网关就绪、命令回复不存进聊天记录）在功能分支上，用户测试满意后再合并到 `main`。
- `GoenZero/MaiBot-Proactive-Chat`：让大肥鱼主动找用户聊天的插件，开发在 `develop` 分支，同样等用户确认后合并。
- 打版本标签（tag）要用户自己在 GitHub 网页上建。
