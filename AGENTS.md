# AGENTS.md

本仓库是 Hermes Agent 的自用插件集合。安装/使用说明见 `README.md`；本文件只记录开发约定。每个根目录子目录是一个**可独立安装**的插件（`<repo>/<plugin>`），Hermes 安装时只拉取该子目录，因此不要添加跨插件的共享包、顶层 `plugins/` 包或跨目录 import。

## 提交前先跑

```bash
hermes plugins doctor ./<plugin> --ci   # 导入/注册/manifest 快速检查，使用临时 HERMES_HOME
hermes plugins validate ./<plugin>      # catalog 门禁；--install-deps 可安装声明依赖
```

仓库没有根级 pyproject、CI、lint、formatter 或 typecheck 配置；不要声称跑过不存在的检查。

修改插件代码后，先提交插件代码，再把 `hermes-pack.yaml` 中对应条目的 `ref` 改成包含该改动的完整 40 位 commit SHA（分支名/标签会被拒绝），另起一次提交更新 pack。ref 指向代码提交，通常不是 pack 更新提交自身。

## 插件契约

- `__init__.py` 必须提供 `register(ctx)`；`plugin.yaml` 声明 `name`、`version`、`description`、`provides_tools`、`provides_hooks`、`provides_middleware` 等。实际注册的工具/命令/hook 要与 manifest 一致，`doctor`/`validate` 会比对。
- 插件目录名带连字符，不能按普通包名导入。Hermes 按路径加载，插件内统一用“相对导入优先、顶层导入兜底”的写法：
  ```python
  try:
      from .foo import bar
  except ImportError:
      from foo import bar
  ```
  参考 `audio-extract/__init__.py`、`media-downloader/__init__.py`。
- 工具 handler 接收 `args: dict, **kwargs`，**始终返回 JSON 字符串**（成功和失败都是），不要向调用方抛异常。用 `ctx.register_tool(name=..., toolset=..., schema=..., handler=...)` 注册。
- 斜杠命令用 `ctx.register_command(...)`；hook/middleware 用 `ctx.register_hook` / `ctx.register_middleware`，回调接受 `**kwargs` 以兼容后续新增字段。
- Python 依赖写在 `plugin.yaml` 的 `python_dependencies`（或同目录 `pyproject.toml`），并带上界；`airss` 现有 3 个无上界依赖是已知 warning。
- 不要新增 Hermes 私有模块导入（`gateway.session_context`、`agent.secret_scope`、`agent.turn_context*` 等）。`validate` 会把 `airss`、`audio-extract`、`media-downloader` 现有的私有导入标为 warning；`idle-compaction` 还故意 patch 核心模块。这些都是历史例外，不要复制到新代码。
- 后台线程必须在 `ctx.on_unload(...)` 中停止。`airss` 是带 dashboard 的插件，改 `dashboard/` 后要重启 `hermes dashboard` 才会挂载新路由。

## 配置与持久化（profile-aware）

- 新代码一律写 `<profile>/data/<plugin>/`：默认 home（default profile）下是 `~/.hermes/data/<plugin>/`，命名 profile 下是 `~/.hermes/profiles/<name>/data/<plugin>/`。**绝不能写插件安装目录** `<profile>/plugins/<plugin>/`，`hermes plugins update/remove` 会替换或删除它。
- 不要只信 `HERMES_HOME`：在 gateway/dashboard 多 profile 进程中，它可能指向 Hermes 根目录，而不是当前插件所属的 profile。按安装位置解析：插件位于 `<profile>/plugins/<plugin>/`，所以 `Path(__file__).resolve().parents[2]` 就是 `<profile>`；再回退 `HERMES_HOME`、`~/.hermes`。参考 `audio-extract/config.py`、`media-downloader/config.py`、`qbittorrent-manager/client.py` 中的 `resolve_hermes_profile_dir` / `profile_home`。
- 配置分两层：`plugin.yaml` 的 `config_schema` 是 Hermes 设置界面和 `ctx.get_config` 的键；host 私有或不可公开的值放 `<profile>/data/<plugin>/config.json`（`audio-extract` 还会迁移旧插件目录里的 `config.json`）。不要把真实配置写进仓库里的 `config.sample.json`。
- 含密钥的文件（如 qBittorrent 的 `.env`）放 data 目录并 `chmod 0600`；不要把 token、密码、Cookie 写进 `plugin.yaml`、README、测试或日志。
- 已知例外：`vibeguard` 的 `vibeguard.json` 仍读 profile 根目录（`vibeguard/config.py`）；`imap-watcher` 的 `email_accounts.json` 在 profile 根目录、`email_state.json` 在 `<profile>/cache/`（`imap-watcher/watcher.py`）；`airss` 及其 dashboard 仍直接用 `HERMES_HOME` 拼 `data/airss`。改这些插件时逐步迁移到上面的规则，不要新增同类路径。
- Hermes 官方 `plugins.plugin_storage.plugin_data_dir()` 写的是 `<profile>/plugin-data/<name>/`；本仓库的约定和测试是 `<profile>/data/<plugin>/`，不要在同一个插件里混用两种位置。
- 后台线程里调用 LLM/工具或跨 profile 发消息，要绑定 owning profile：`airss/scheduler.py` 用 `set_hermes_home_override` 加 secret scope；`audio-extract/tasks.py`、`media-downloader/tasks.py` 的 `hermes send` 显式带 `--profile`（gateway 不导出 `HERMES_PROFILE`）。

## 测试

- 大多数插件：`python3 -m pytest <plugin>/tests -q`；单测用 `python3 -m pytest <plugin>/tests/test_x.py::test_name -q`。只依赖标准库或已装依赖的测试，也可直接 `python3 <plugin>/tests/test_*.py`（很多文件带 `__main__` runner）。
- `airss/tests` 使用 `from plugins.airss...`，必须在父目录能看到名为 `plugins` 的仓库根（下面命令在仓库根目录执行）：
  ```bash
  tmp=$(mktemp -d)
  ln -s "$PWD" "$tmp/plugins"
  (cd "$tmp" && python3 -m pytest plugins/airss/tests -q)
  ```
  运行前需要 `aiosqlite`、`feedparser`、`jinja2`、`Pillow`、`pytest-asyncio`（后两者不在 `plugin.yaml`）。
- `audio-extract` 和 `media-downloader` 的 `test_config_resolution.py` 假设插件已安装到 `<profile>/plugins/<plugin>`；在 checkout 里直接跑会失败 1–2 个 profile 断言，这是预期。验证时复制到假 profile：
  ```bash
  tmp=$(mktemp -d)
  mkdir -p "$tmp/profiles/p1/plugins"
  cp -a audio-extract "$tmp/profiles/p1/plugins/"
  (cd "$tmp/profiles/p1/plugins/audio-extract" && python3 -m pytest tests -q)
  ```
  `media-downloader` 同理替换目录名。
- `qbittorrent-manager/tests/test_config_paths.py` 若 `pytest <dir>` 报 `attempted relative import`，直接运行 `python3 qbittorrent-manager/tests/test_config_paths.py`。
- `imap-watcher` 没有测试；改动至少跑 `hermes plugins doctor ./imap-watcher --ci`。
- 仓库没有统一的测试依赖或虚拟环境；用哪个 Python 跑 pytest，就先把 `plugin.yaml` 声明的依赖装进那个 Python。

## 提交前脱敏（硬性）

- 本仓库是脱敏后的镜像；禁止提交作者工作身份、工作数据或私人数据：真实姓名、工作邮箱、公司/团队、内网地址/主机名、NAS/工作盘路径、真实 RSS/邮件/种子/媒体标题、账号、Cookie、密钥、证件号、手机号、截图和日志。
- 测试脚本同样是提交内容：fixture 只用 `example.com`、`/tmp/...`、`user`、`測試甲` 等假值；不要把真实库、真实响应、真实路径复制进测试。
- `.gitignore` 已忽略 `data/`、`*.db`、`*.log`、`.env`、`config.json`、`*.local.json`，但它不是保证；不要 `git add -f`，也不要新增会写入仓库的默认配置路径。
- 提交前检查 `git diff --cached` 全文，以及 `git config user.name` / `user.email`（不要用工作邮箱）；对 staged 内容做一次敏感模式扫描（token、私钥、IP、手机/证件号、绝对家目录/挂载路径）。grep 只是 tripwire，不能代替人工审阅。
- 从内网版本同步代码时，先脱敏再提交；不要为了让测试通过把真实路径或真实数据加回 fixture。
