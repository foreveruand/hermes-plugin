# hermes-plugin

自用的 Hermes Agent 插件集，从内网版本脱敏后独立发布。所有密钥、口令都走环境变量或 Hermes 配置（`config.yaml` / `.env`），本仓库不包含任何真实凭据、内网地址或个人数据。

## 插件一览

| 插件 | 说明 |
|---|---|
| airss | RSS 订阅抓取、AI 摘要与多平台推送（含 Telegram / Webhook）；`dashboard/` 是新增的「RSS 展板」仪表盘页签，数据直接读 airss 本地库 |
| audio-extract | 无损视频切片、Whisper 音频提取与字幕同步 |
| idle-compaction | 空闲上下文压缩（修复 Issue #79357 的时钟重置问题） |
| imap-watcher | IMAP IDLE 实时邮件到达提醒，多账号多文件夹单连接复用 |
| media-downloader | yt-dlp / gallery-dl 统一下载 + ASMR.ONE 管线 + 百度网盘加密中转（bdpan handoff） |
| qbittorrent-manager | 从 Hermes 管理 qBittorrent 种子、标签与 RSS 规则 |
| vibeguard | 模型边界本地脱敏：站点、设备等敏感实体名映射为固定代号，证件号、手机号替换为随机假值，本地工具与最终答复时还原 |

## 仓库结构

与官方插件仓库 NousResearch/hermes-official-plugins 相同的布局：每个插件是仓库根目录下一个自包含目录，目录名即插件名，内含 `plugin.yaml` 清单与代码。Hermes 按 `仓库 + 子目录` 安装单个插件时只会拉取该目录。

```
hermes-plugin/
├── hermes-pack.yaml        # 插件包清单：一键安装全部插件（钉死到确切提交）
├── airss/                  # 每个目录是一个完整插件
│   ├── plugin.yaml
│   ├── __init__.py
│   ├── dashboard/          # 仪表盘扩展（RSS 展板）
│   └── tests/
├── audio-extract/
├── idle-compaction/
├── imap-watcher/
├── media-downloader/
├── qbittorrent-manager/
└── vibeguard/
```

## 安装

**一键全装（插件包）**——把本仓库克隆到本地后，在仓库根目录执行（`hermes-pack.yaml` 把每个插件钉死到确切提交，安装前有审阅屏）：

```bash
hermes plugins pack show ./hermes-pack.yaml     # 预览
hermes plugins pack install ./hermes-pack.yaml  # 审阅 → 确认 → 逐个安装
```

**单个安装**（以 airss 为例，其余把子目录换成对应插件名）：

```bash
hermes plugins install foreveruand/hermes-plugin/airss --enable
# 等价的完整 URL 形式：
hermes plugins install https://github.com/foreveruand/hermes-plugin/tree/main/airss --enable
```

装完按各插件 `plugin.yaml` 的 `config_schema` 在 Hermes 配置里填写参数。带 `dashboard/` 子目录的插件（airss 的「RSS 展板」）会给 Web 仪表盘（`hermes dashboard`）注册页签，安装后需重启 dashboard 才会挂载后端路由。

**私有仓库凭据**：本仓库是私有仓库，`hermes plugins install` 非交互克隆，需要在运行 Hermes 的机器上预先备好一种凭据——Hermes `.env` 里的 `GITHUB_TOKEN`/`GH_TOKEN`、`gh auth login` 的登录态、git 凭据助手，或改用 SSH 地址并把钥匙放进 ssh-agent。

**手动安装**：把对应插件目录复制到 `~/.hermes/plugins/`，再 `hermes plugins enable <name>` 即可。

## 配置与密钥

- 敏感项（机器人 token、qBittorrent 账号密码、IMAP 口令、RSSHub 访问密钥等）一律通过环境变量或 Hermes 配置注入，代码里的默认值均为占位或空。
- 路径类默认值（媒体库、录制目录等）是通用占位路径，请按自己的环境修改。

## 脱敏说明

本仓库由内网在用版本过滤而来：移除了内网 IP、真实 NAS 目录结构、工作文档示例与个人作者署名，替换为占位值；功能代码与在用版本一致（airss 的展板为新增）。
