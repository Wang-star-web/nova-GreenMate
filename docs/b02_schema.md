# B02 数据表 Schema 说明

数据库文件:`data/environment.db`(SQLite 3,Python 标准库 `sqlite3` 创建,无任何扩展)。

## 表 1:environment_samples(环境样本时序表)

```sql
CREATE TABLE IF NOT EXISTS environment_samples (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_time  TEXT NOT NULL UNIQUE,          -- 采样时间 'YYYY-MM-DD HH:MM:SS'(本地时间,固定 30 分钟网格)
    temperature  REAL NOT NULL,                 -- 温度 ℃,合法 5~40
    humidity     REAL NOT NULL,                 -- 湿度 %RH,合法 30~90
    co2          REAL NOT NULL,                 -- CO₂浓度 ppm,合法 300~2000
    light        REAL NOT NULL,                 -- 光照 lux,合法 0~100000
    source_type  TEXT NOT NULL DEFAULT 'SIMULATED',  -- 数据来源标记(与 B01 约定一致)
    sim_scenario TEXT NOT NULL,                 -- 模拟器场景元数据:仅生成追溯用,严禁作为标签/特征
    created_at   TEXT NOT NULL                  -- 行写入时间,ISO8601 带时区,如 2026-10-29T01:00:00+08:00
);
```

| 字段 | 类型 | 约束 | 语义 |
|---|---|---|---|
| `id` | INTEGER | 主键自增 | 行号,无业务含义 |
| `sample_time` | TEXT | NOT NULL UNIQUE | 采样时刻,固定 30 分钟网格点(`分` 只能为 00/30,`秒` 恒为 00) |
| `temperature` | REAL | NOT NULL | 温度(℃),合法范围 5~40 |
| `humidity` | REAL | NOT NULL | 湿度(%RH),合法范围 30~90 |
| `co2` | REAL | NOT NULL | CO₂浓度(ppm),合法范围 300~2000 |
| `light` | REAL | NOT NULL | 光照(lux),合法范围 0~100000 |
| `source_type` | TEXT | NOT NULL | 数据来源标记,当前恒为 `SIMULATED`(与 B01 顶层字段约定一致) |
| `sim_scenario` | TEXT | NOT NULL | 模拟器场景名(见下方警告),当前取值 `baseline` 或 `disturbance_*` |
| `created_at` | TEXT | NOT NULL | 行写入库的时刻(带时区 ISO8601),仅审计用,不参与查询 |

- `UNIQUE(sample_time)` 的隐式唯一索引即服务所有时间窗 `WHERE sample_time >= ? AND sample_time <= ?` 查询,无需额外索引。
- 时间语义:库内一律 **naive 本地时间字符串**(中国无夏令时,30 分钟差分运算安全);仅 `created_at` 与报告时间戳使用带时区 ISO8601。
- 索引之外不启用 WAL(单写多读场景,避免 sidecar 文件混入交付物)。

## ⚠ sim_scenario:仅模拟器元数据

`sim_scenario` 是生成器写下的**内部追溯元数据**,用于说明每个采样点来自哪一段模拟场景
(基线或某段扰动)。**它不能当作标签、不能作为基础特征**——这不是建议,是 B02 的硬性约束,
并已在代码层结构性强制执行:

1. 公开 API `get_current_environment()` / `get_environment_history()` 使用**显式列清单**
   (`sample_time, source_type, temperature, humidity, co2, light`),不含 `sim_scenario`,
   即使将来误改 `SELECT *` 也不会泄漏;
2. Web 页面不展示、不用它着色或分段;
3. 仅 `b02_memory.py` 内部的质量检查(C7 残差期望、C8/C9 扰动段校验)读取该列做数据质量审计。

## 表 2:b02_meta(生成器元数据表)

```sql
CREATE TABLE IF NOT EXISTS b02_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

键值表,由 `generate` 写入,记录本次生成的全部可追溯信息:

| key | 示例值 | 说明 |
|---|---|---|
| `schema_version` | `1` | Schema 版本 |
| `generator_version` | `1.0` | 生成器版本 |
| `seed` | `42` | 随机种子(生成确定性) |
| `anchor` | `2026-10-29 01:00:00` | 7 天窗口右端点(最近 30 分钟边界) |
| `start_time` / `end_time` | `2026-10-22 01:30:00` / `2026-10-29 01:00:00` | 网格起止 |
| `expected_points` | `336` | 期望采样点数 |
| `disturbance_count` | `5` | 扰动片段数 |
| `noise_temperature` / `noise_humidity` / `noise_co2` / `noise_light` | `0.4` / `1.5` / `15.0` / `300.0` | 各参数噪声幅度 |
| `disturbances` | JSON 数组 | 扰动片段清单(与 evidence/b02/disturbance_manifest.json 同构) |
| `generated_at` | `2026-10-29T01:08:02+08:00` | 本次生成时刻 |
| `generation_mode` | `create` / `fill` | 强制重建 / 幂等补插 |

## 查询接口契约(供 B06、B07 复用)

```python
get_current_environment()      # -> dict|None:{sample_time, source_type, temperature, humidity, co2, light}
get_environment_history(hours) # -> list[dict]:最近 N 小时(锚点=库内 MAX(sample_time)),升序,同形状
```

- 返回形状**不含** `sim_scenario`(结构性隔离,见上);
- 空库:`get_current_environment()` 返回 `None`,`get_environment_history()` 返回 `[]`;
- `hours <= 0` 抛 `ValueError`;
- 导入模块或启动 Web 服务**不会**生成/改写数据,数据只由 `generate` 子命令显式写入。
