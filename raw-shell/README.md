# raw-shell — Hermes 插件

在 Telegram（或 Discord、CLI 等任何会话）里发 `/raw <命令>`（同义命令 `/shell`），
直接在运行 Hermes 网关的宿主机上执行该命令并返回输出，**不经过 LLM、不花 token、可以带任意参数**。

```
/raw df -h
/raw docker ps
/raw ls /var/log
```

如果 `/raw` 看起来“参数没传进去”（例如 `/raw ls 某目录` 输出了别的东西或固定报错），
见下面「命令名被 quick_commands 占用」一节，改用 `/shell` 即可绕开。

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
3. 重启网关：`hermes gateway restart`（也可以在聊天里发 `/gateway restart`）。
4. 可选自检：`hermes plugins doctor raw-shell`

本插件要求 Hermes `>= 0.22.3`（命令上下文会提供 `platform` / `session_key` 等
参数），旧版本不会加载。

## 访问控制

插件本身**没有**用户白名单或身份校验：只要消息能到达网关——CLI、Telegram、
Discord，或任何未知平台——`/raw` 就会执行。因此网关自身的访问控制就是唯一
边界，请务必收紧：

- 只配对/允许自己的账号，不要开 `GATEWAY_ALLOW_ALL_USERS`；
- 不要把网关暴露给不受信任的群聊或多人账号。

`platform`、`session_key` 和发送者 ID 只写进日志，不参与放行判断。

## 命令名被 quick_commands 占用（/raw 收不到参数）

Hermes 的分发顺序是 **quick_commands → 插件命令 → bundles → skills**。
如果 `config.yaml` 里还留着旧的 `raw` quick command：

```yaml
quick_commands:
  raw:
    type: exec
    command: timeout 25 /bin/bash -c "$@"
```

那么 `/raw ls 某目录` 会先命中这条 quick command，**插件根本收不到**；
而 `type: exec` 的 quick command 不接收行尾参数（`$@` 永远是空的），
表现就是“命令能跑、参数传不进去”。两处任选其一：

1. 删掉整个 `quick_commands.raw`（`raw-shell` 就是用来替代它的），重启网关；
2. 改用同义命令 `/shell ls 某目录`，它不走 `raw` 这个名字，不受影响。

两个命令的作用、参数与超时行为完全一致。

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
- 每次执行都会写网关日志（logger 名 `raw-shell`），含平台、session、发送者 ID 和命令前 200 字符；这些信息只用于日志。

## 卸载

删掉插件目录（`~/.hermes/plugins/raw-shell/`），并把 `raw-shell` 从 `plugins.enabled` 移除，
重启网关即可。Hermes 本体没有任何改动。
