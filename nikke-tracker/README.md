# NIKKE 养成看板（Hermes Dashboard 插件）

一个**自包含**的 NIKKE 养成海报墙：进度数据、目标库、诊断与渲染逻辑全部随插件分发，不依赖 `nikke-tracker` 技能，技能后续移除也不影响面板。

- 角色卡：头像、右上角 T0/T1/T2、装备词条的成型度环、收藏星。
- 悬浮详情：四个部位 × 三个词条槽，词条类型下拉 + **阶数下拉** + 锁定；技能 / 收藏品。
- 四件装备**一次保存**：一个「保存四件」按钮，服务端一次写入、一次诊断、一次 Markdown 渲染。
- 建议：复用内置 `nikke_advisor.diagnose()` 的洗练方案（期望石头、锁定建议）、缺口和词条诊断。
- 配队：异常拦截与单人突击参考表（只读）。
- 收藏队列：按梯度 + 成型度自动排序。
- 头像：自动抓取、手动上传覆盖；推荐信息：详情页「目标基准 → 校核」可改梯度/技能/主副词条。

## 安装

```bash
hermes plugins install foreveruand/hermes-plugin/nikke-tracker --enable
```

然后重启 `hermes dashboard`，面板右上角 profile 选对应 profile，页签 `/nikke` 出现在 Skills 之后。若页签不出现，确认插件已 enabled；仍不出现则重启 dashboard（插件 API 在启动时挂载）。

无需安装 `nikke-tracker` 技能。

## 数据

- 进度：`<profile>/data/nikke/player_progress.json`，唯一数据源（与旧技能共用的文件格式）。
- 目标库副本：`<profile>/data/nikke/targets.json`。首次读取时从插件内置 `references/targets.json` 复制，**只补缺、不覆盖**本地校核结果；想恢复内置基准，备份后删掉该文件再刷新。内置文件带 `guide_date`（一图流日期）。
- 校核写回：详情页「校核」写上述 `targets.json`，写前备份到 `<profile>/data/nikke/targets.json.bak`。
- 收藏：`<profile>/data/nikke/dashboard_state.json`，不写进进度。
- 头像缓存：`<profile>/data/nikke/portraits/`，运行时不联网。
- 每次保存后重新渲染 `main/游戏与联机/nikke.md`（内容不变时跳过写入）。

内置文件（随插件仓库分发，只读）：

```
dashboard/nikke_lib/           # nikke_db / nikke_advisor / nikke_obsidian（2026-11-13 从技能同步）
references/targets.json        # 一图流目标库（86 角色，含 guide_date）
references/interception-teams.md
references/solo-raid-teams.md
```

`dashboard/nikke_lib` 是插件私有副本，技能保留自己的脚本；两者只在 `player_progress.json` 的格式上兼容，升级攻略时各改各的。

## 魔方

魔方可随时无代价更换，只作为**目标基准**展示（来自 `targets.json` 的 `cube`），不作为玩家进度保存。详情页的「技能 / 收藏品」不含魔方输入框，`player_progress.json` 里旧的 `cube` 键会被忽略但不会改写。

## 装备词条

数值由词条阶数决定，界面用「N阶」下拉，不再手输百分比。旧数据里不落在标准阶上的数值显示为「自定义 x.xx%」，只有主动选阶才会覆盖。所有 11 个词条（含攻击速度、最大体力）都有 15 阶表。

## 洗练石头预估

每次刷新（洗一次）：未锁定的词条1 必出新词条，词条2/3 分别以 50% / 30% 概率出现，未出现即空槽；锁定槽保留。刷新消耗为 1 石头（不锁定）/ 2 石头（锁 1 条）/ 3 石头（锁 2 条）。

期望石头由 `nikke_advisor` 的 27 状态值迭代模型精确求解，并给出「当前是否锁定、命中后锁哪几条」的建议。命中口径 = 出现概率 × 目标词条类型概率（词条池 11 种、均匀分布假设，即角色优先词条数 / 11）；数值/阶数精洗属于后续阶段，不计入该预估。导出的 `nikke.md` 不再包含旧版「推荐词条（8 词条）/ 12 词条极致」默认行，词条要求以一图流说明为准。

## 构建

运行时不需要 Node，`dashboard/dist/` 已提交。改 `dashboard/src/` 后：

```bash
bash dashboard/build.sh
# 或 ESBUILD=/path/to/esbuild bash dashboard/build.sh
```

React 由面板 SDK 提供，不打包进 bundle。

## 头像

```bash
python3 scripts/resolve_portraits.py --owned-only
python3 scripts/resolve_portraits.py --only 小红帽 --force
```

解析顺序：本地覆盖 → GameKee 图鉴 API（`https://www.gamekee.com/nikke`，中文名直接匹配，**首要图源**）→ Prydwen 角色列表页（`https://www.prydwen.gg/nikke/characters`，英文名 / slug 兜底）→ `portrait_map.json` 的 slug 拼 CDN → 占位。BiliGame 已停用（不再更新角色图）。

名称匹配：`references/portrait_names.json` 内置中文名矫正（变体 / 国服译名 → GameKee 图鉴名），配合名称归一化覆盖 84/86 个角色；剩余角色在详情页「校核 → 头像名称」里填 GameKee 中文名或 Prydwen 英文名，保存写回 `<profile>/data/nikke/targets.json` 的 `portrait_name`，抓取时优先使用。下载限制 3MB / 非 HTML 内容类型 / 仅允许 `*.gamekee.com` 与 `*.prydwen.gg`，请求带浏览器 UA 与 Referer；解析失败原因会打印出来。脚本直接复用插件内置 `nikke_lib` 解析目标库路径。

本地覆盖：

```
<profile>/data/nikke/portraits/overrides/<角色名>.png|jpg|webp
```

详情页右上角 **上传头像** 会写入同一缓存目录并标记 `source: manual`，自动图会被覆盖。

## 推荐信息校核

详情页「目标基准」 → **校核** 可改：

- 梯度 T0/T1/T2
- 技能 1 / 2 / 爆裂
- 主词条（优先刷）
- 副词条（有更好）
- 校核说明 / 来源备注
- 头像名称（图鉴匹配用，抓不到头像时填 GameKee 中文名或 Prydwen 英文名）

保存后写回 `data/nikke/targets.json`，并重渲染 `nikke.md`。旧字段 `affix_priority/affix_targets/affix_targets_max` 只作历史默认值，面板展示以 `affix_main/affix_sub/affix_note` 为准；不会再显示默认 8 词条。

## API

全部在 `/api/plugins/nikke-tracker/`：

| 方法 | 路径 |
| --- | --- |
| GET | `/health` |
| GET | `/bootstrap` |
| GET | `/detail?name=` |
| PUT | `/gear` （单件，兼容旧调用） |
| PUT | `/gear/batch` （四件一次保存） |
| PUT | `/meta` |
| PUT | `/target` |
| POST | `/character` |
| DELETE | `/character/{name}?confirm=1` |
| PUT | `/state` |
| POST | `/render` |
| GET | `/teams` |
| GET | `/portrait/{key}` |
| POST | `/portrait/fetch` |
| POST | `/portrait/upload` |

写入用 `fcntl.flock` + `rev`（progress 文件的 sha1 前 12 位）防并发覆盖；`/gear/batch` 请求体为 `{"name","rev","pieces":{slot:{"level","slots"}}}`，先全量校验再一次性写入，任一件非法则四件都不落盘。服务端会显式覆盖三槽的 `locked`，以绕开 `update_gear` 只加锁不清锁的行为。

`/bootstrap` 只返回墙页摘要（不含每角色详情，约 40 KB），详情按需从 `/detail` 加载并在前端缓存。

## 测试

```bash
# 需要一份真实的 player_progress.json（NIKKE_PROFILE_DIR 指向 profile 根目录）
NIKKE_PROFILE_DIR=<profile> python3 -m pytest tests -q
```

测试把内置 `references/targets.json` 与真实进度复制到临时目录；没有进度文件时相关用例会跳过。`plugin_api` 的加载测试需要 `fastapi`（面板运行环境自带）。

```bash
hermes plugins doctor ./nikke-tracker --ci
hermes plugins validate ./nikke-tracker
```
