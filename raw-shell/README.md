# raw-shell — Hermes 插件

在 Telegram（或 Discord、CLI 等任何会话）里发 `/raw <命令>`，直接在运行
Hermes 网关的宿主机上执行该命令并返回输出，**不经过 LLM、不花 token、可以带任意参数**。

```
/raw df -h
/raw docker ps
/raw systemctl status hermes-gateway
```

## 为什么是插件形态

- 插件走官方公开的 `ctx.register_command()` 接口，`hermes update` **不会覆盖它**，
  升级 Hermes 后无需重新修补。
- 内置 Quick Commands 只能执行写死在配置里的命令、不能带参数；本插件就是补这个缺口。

## 安装

1. 把 `raw-shell/` 整个目录放到 Hermes 的插件目录下
   （默认 profile：`~/.hermes/plugins/raw-shell/`；命名 profile：
   `~/.hermes/profiles/<name>/plugins/raw-shell/`）。
2. 启用插件（二选一）：
   - 命令行：`hermes plugins enable raw-shell`
   - 或在 `~/.hermes/config.yaml` 的 `plugins.enabled` 列表里加上 `raw-shell`。
3. 配置白名单（见下节），然后重启网关：`hermes gateway restart`
   （也可以在聊天里发 `/gateway restart`）。
4. 可选自检：`hermes plugins doctor raw-shell`

本插件要求 Hermes `>= 0.22.3`（命令上下文会提供 `platform` / `session_key` 等
参数），旧版本不会加载。

## 授权（重要）

`/raw` 等于把宿主机 shell 挂到聊天上，授权是**失败即拒绝**的：

- **本机 CLI / TUI / Desktop / ACP**：始终允许。
- **其它任何来源**（Telegram 等消息平台，以及缺失、为空或未知的 platform）：
  一律按远端处理，只有发送者的用户 ID 在白名单里才执行。
- 纵深防御：即使 platform 声称是本地，只要 `session_key` 里出现已知消息平台关键字，
  也按远端处理。

白名单是**并集**，两处都可以配，配了哪处都生效：

`~/.hermes/.env`（推荐，逗号或空格分隔）：

```
RAW_SHELL_ALLOWED_USERS=123456789
```

或 `config.yaml`（默认 profile 为 `~/.hermes/config.yaml`）：

```yaml
plugins:
  entries:
    raw-shell:
      settings:
        allowed_users: ["123456789"]
```

数字是你的 Telegram 用户 ID（不是 @用户名）。查自己的 ID 可以给
@userinfobot 发任意消息。另外请确保网关本身只配对/允许你自己的账号
（不要开 `GATEWAY_ALLOW_ALL_USERS`），这是第一道门，白名单是第二道。

## 行为细节

- 命令以网关进程的用户身份、用 `/bin/bash -c` 在家目录下执行；
  stdin 关闭，所以交互式命令和 sudo 输密码用不了（需要 root 的命令请配 sudoers 免密）。
- 输出是流式读取的，内存里只保留前 `RAW_SHELL_MAX_CHARS` 个字符，
  `yes`、`cat /dev/zero` 之类的命令不会把网关内存吃满。
- 默认超时 120 秒（`RAW_SHELL_TIMEOUT` 可改）；超时会杀掉整个进程组
  （包括命令自己启动的后代进程），并返回已有的部分输出。
- 输出上限默认 3900 字符（`RAW_SHELL_MAX_CHARS` 可改），超了截断并注明总长——
  Telegram 单条消息上限 4096。
- 非零退出码会在输出头部标 `(exit N)`；stdout 和 stderr 合并返回。
- 每次执行和每次拒绝都会写网关日志（logger 名 `raw-shell`），含平台、用户 ID、命令前 200 字符。

## 卸载

删掉插件目录（`~/.hermes/plugins/raw-shell/`），并把 `raw-shell` 从 `plugins.enabled` 移除，
重启网关即可。Hermes 本体没有任何改动。
