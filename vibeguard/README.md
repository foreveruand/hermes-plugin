# VibeGuard

模型边界上的本地脱敏层。只做两件事：

| 对象 | 处理 | 模型看到 |
| --- | --- | --- |
| 站点、设备等敏感实体名 | **固定映射**，代号存 `data/vibeguard/mapping.json` | `[[S12]]`、`[[K3]]` |
| 密钥、身份证号、手机号、邮箱 | **随机替换**，假值只存在当前进程 | 形如真的随机值 |

两者都能还原：本地工具执行前、最终答复发给用户前，代号和假值都会换回真值。
检测全部在本地完成，没有模型参与，也没有数据库和密钥文件。

## 工作方式

    user text   --[llm_request]---------->  [[S12]] / 假身份证号   -> provider
    tool_call   --[tool_request]---------->  真名 / 真号            -> 工具执行
    tool result --[transform_tool_result]->  [[S12]] / 假号         -> 模型
    answer      --[transform_llm_output]-->  真名 / 真号            -> 用户

- 代号固定：同一个名称永远同一个 `__VG_HIDDEN_S\d+__`，跨轮、跨重启、跨重建都不变。
- 假值随手生成：只为骗过模型，重启即失效，不落盘。
- 幂等：已脱敏的历史再扫一遍不会二次替换。

## 数据文件

`data/vibeguard/mapping.json`（0600，主机私有，不入 git）

```json
{"version": 1, "source": "站点清单.doc",
 "stamp": {"mtime": 1790000000, "size": 550503},
 "sites": {"测试甲": "S1"}, "devices": {"测101": "K1"}}
```

代号只增不复用：重建时同名沿用旧代号，新名称取 `max+1`。
网关运行中改这个文件会被自动重载，不需要重启。

## 配置

`vibeguard.json`（profile 根目录，gitignored）：

```json
{
  "enabled": true,
  "mapping": "",
  "patterns": {
    "keywords": [],
    "regex": [{"pattern": "sk-[A-Za-z0-9]{48}", "category": "CREDENTIAL"}],
    "builtin": ["china_id", "china_phone", "email"],
    "exclude": ["example.com", "localhost"]
  },
  "topology": {
    "enabled": true,
    "glob": ["/path/to/docs/*站点清单*.doc*"],
    "archive": "notes/站点清单.md"
  }
}
```

- `mapping`：留空表示 `<profile>/data/vibeguard/mapping.json`。
- `patterns.builtin`：`china_id`、`china_phone`、`email`、`ipv4`、`uuid`、`mac` 可选。
- `patterns.regex` 的 `category` 决定假值形态：`CREDENTIAL` 保留 `sk-`/`ghp_`/`AKIA` 前缀，其余按字符类别逐个随机。
- `patterns.keywords` 是 v1 的兼容路径：命中后换成 `__VG_<类别>_<hash>__` 占位符，一般不用。

## 定期同步

```bash
python3 plugins/vibeguard/cli.py inventory seed      # 一次性：从现有笔记接管代号
python3 plugins/vibeguard/cli.py inventory sync      # 读清单文档 -> 更新 mapping.json + 笔记
python3 plugins/vibeguard/cli.py inventory sync --dry-run --out /tmp/note.md
```

清单文档更新后由 cron 定期调用 `inventory sync` 重建。
需要 `catdoc`（`.doc`）；`.docx` 走 zip 解析，不需要外部工具。

## 性能

数千条实体名与别名构建一次 Aho-Corasick 自动机（约 40 ms），之后每 20k 字符一遍扫描约 4 ms，
正则层约 3 ms；重复文本命中会话缓存，约 0.05 ms。

## 不做的部分

- 不做出口阻断：还原后的真名仍可被联网工具发出（与 v1 行为一致）。
- 不做语料扫描、候选审核、实体台账、人员/单位识别。
- 不映射人名、公司名、地名。

## 测试

```bash
python3 -m pytest plugins/vibeguard/tests -q
```
