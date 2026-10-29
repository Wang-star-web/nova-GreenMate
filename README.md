# GreenMate — 番茄大棚环境监测课程项目 (B01 / B02 / B03 / B04 / B05 / B06 / B07 / B08)

番茄大棚环境监测课程项目,当前包含 B01(智能交互模块)、B02(数据记忆模块)、
B03(天气证据接口模块)、B04(RAG 检索增强模块)、B05(工具智能体闭环模块)、
B06(传统机器学习闭环模块)、B07(主程序组装模块)与 B08(整体交付验证脚本):

- **B01** 命令行交互入口:采集 4 项环境参数并校验合法性,每次采集自动生成唯一 `run_id`,
  原始数据保存至 `evidence/raw/{run_id}.json`;只负责交互输入与原始数据保存。
- **B02** 数据记忆模块:将环境状态(timestamp + source_type)持久化到 SQLite
  (`data/environment.db`),固定 30 分钟网格、连续 7 天 336 个时序点(正常基线 + 扰动片段 + 噪声);
  程序重启后历史数据保留,提供 `get_current_environment()` / `get_environment_history(hours)`
  公开接口(供 B07 复用;新 B06 以只读连接直读该库)、时间窗口查询统计与趋势绘图
  (Chart.js 本地文件,离线可用)、10 项质量校验;不实现机器学习、RAG 与真实传感器。
- **B03** 天气证据接口模块:接入真实天气 Web API(Open-Meteo 主 / QWeather 可选),
  真实 HTTP 请求 + JSON 解析,统一返回 `weather_evidence` 结构(provider/来源记录/
  analysis_time/forecast_time/温度/湿度/降水概率 + 云量/辐射扩展字段),证据落盘
  `evidence/b03/`;提供 `get_weather_forecast(location)` 供 B07 在 run_id 流程中按需调用;
  支持 A/B 验证与失败验证(超时/错误参数/鉴权失败),服务出错如实返回失败,禁止伪造成功;
  不实现机器学习与 RAG。
- **B04** RAG 检索增强模块:番茄专业知识库(5 份自编模拟资料 + source 登记 + 可重建
  chunk/索引),零依赖字符向量 Top-K 检索,`search_tomato_knowledge` 供 B07 复用;
  检索片段送 LLM 回答并输出可溯源引用;资料不足输出【资料不足/无法确认】,
  禁止编造答案与引用;不使用 LangChain/LangGraph。
- **B05** 工具智能体闭环模块:LLM 原生工具调用(tool_calls)闭环——LLM 只决定调用
  哪个工具与参数,由 Python 真实执行 B02/B03/B04 的 4 个工具函数并把结果回传 LLM
  继续判断,直到输出最终回答;单/多工具任务 + 分支逻辑(证据充分→回答 / 不足→继续
  调用 / 失败→标注缺失降级)+ 每次调用强制日志(tool/arguments/tool_result/sequence);
  `run_agent`/`dispatch_tool`/`TOOL_DEFINITIONS` 供 B07 复用。
- **B06** 传统机器学习闭环模块:复用 B02 时序数据(只读)构建时间窗口预测任务——
  输入窗口最近 3 小时(6 点),预测未来 1~2 小时 `next_window_attention`
  (NORMAL/WATCH);26 个基础特征(current/mean/delta 等,sim_scenario/天气/标签源
  禁用);时间边界切分(前 5 天训练/后 2 天测试,防泄露断言);朴素基线对比 +
  混淆矩阵/F1/Recall + 假阳性/假阴性错误样本解释;模型/特征/配置保存重载,
  重启后可推理;`predict_next_attention`(MODEL_OUTPUT=B06_ML)供 B07 复用;
  不使用深度学习。
- **B07** 主程序组装模块:统一入口,把 B01~B06 串联成完整流水线
  (采集 → 存储 → 解析 → 规则 → ML → 融合 → 报告),全程使用同一个 run_id,
  任一阶段失败按降级策略继续。新 B06 独立运行,`predict_next_attention`
  (MODEL_OUTPUT=B06_ML)预留 B07 重写时复用。
- **B08** 整体交付验证脚本:输入 run_id 校验全部交付产物(raw/parsed/rule/ml/final
  证据 + HTML 报告)的存在性与内容有效性,输出三态校验清单(OK/缺失/异常)与
  确定性结果指纹,重复运行指纹一致即可确认结果可复现。report HTML 当前恒缺失
  (见 B08 一节注)。

## 运行方式

```bash
py b01_cli.py
```

主菜单:

| 选项 | 功能 |
|---|---|
| ① | 新建一组采集数据 |
| ② | 查看历史 run_id 列表 |
| ③ | 退出程序 |

交互说明:

- 采集过程中输入 `q` 可取消本次采集(不保存任何数据);
- 空输入、非数字、超出范围等非法输入均给出针对性中文提示并要求重新输入,程序不会崩溃;
- 按 `Ctrl+C` 或输入流结束(EOF)时程序安全退出,无异常堆栈。

## 参数合法范围(番茄大棚正常监测范围)

| 参数 | 单位 | 合法范围 |
|---|---|---|
| 温度 | ℃ | 5 ~ 40 |
| 湿度 | %RH | 30 ~ 90 |
| CO₂浓度 | ppm | 300 ~ 2000 |
| 光照 | lux | 0 ~ 100000 |

## 数据文件

- 保存位置:`evidence/raw/run_<YYYYmmdd>_<HHMMSS>_<6位hex>.json`(目录不存在时自动创建)
- 文件结构:

```json
{
  "run_id": "run_20261022_153045_a1b2c3",
  "collect_time": "2026-10-22T15:30:45+08:00",
  "data": {
    "temperature": {"value": 25.5, "unit": "℃"},
    "humidity":    {"value": 60.0, "unit": "%RH"},
    "co2":         {"value": 800.0, "unit": "ppm"},
    "light":       {"value": 30000.0, "unit": "lux"}
  },
  "valid_ranges": {
    "temperature": {"min": 5.0, "max": 40.0, "unit": "℃"},
    "humidity":    {"min": 30.0, "max": 90.0, "unit": "%RH"},
    "co2":         {"min": 300.0, "max": 2000.0, "unit": "ppm"},
    "light":       {"min": 0.0, "max": 100000.0, "unit": "lux"}
  }
}
```

## B01 补全(Web 展示 / LLM 问答 / A-B 验证 / 失败验证)

B01 在采集功能之外补全了以下能力(全部并入 `b01_cli.py`,B02~B08 未改动):

| 子命令 | 功能 |
|---|---|
| `py b01_cli.py` | 采集主菜单(原始功能,不变) |
| `py b01_cli.py web` | 启动本地 Web 页面(127.0.0.1:8123):环境状态 A/B 切换、环境数据展示(每项指标标记 `source_type=SIMULATED`)、固定提问"当前环境怎么样?"、LLM 实时回答、新建采集(手动输入 4 项参数,服务端按 CLI 同规则校验后保存到 `evidence/raw/`)、历史采集结果列表(最近 50 条) |
| `py b01_cli.py abtest` | 自动 A/B 验证:两套环境状态(状态 A 适宜 / 状态 B 异常)、只切换状态、固定提问;LLM 两次回答内容不同即 PASS,相同即 FAIL;失败路径如实上报 |

**环境数据展示**:采集的 raw 记录顶层新增 `"source_type": "SIMULATED"` 字段;Web 页面每张指标卡均显示 `source_type=SIMULATED` 徽章;发给 LLM 的提示词同样注明数据为模拟采集。

**LLM 调用**(DeepSeek OpenAI 兼容接口,全部可用环境变量覆盖):

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `B01_LLM_BASE_URL` | `https://api.deepseek.com/chat/completions` | 接口地址 |
| `B01_LLM_MODEL` | `deepseek-chat` | 模型名 |
| `B01_LLM_TIMEOUT` | `30`(秒) | 连接与读取超时 |
| `B01_WEB_PORT` | `8123` | Web 页面端口 |

API Key 读取顺序:`DEEPSEEK_API_KEY` → `ANTHROPIC_AUTH_TOKEN`(均从环境变量读取,代码中零硬编码)。

**Web 新建采集与历史记录**:网页端可手动输入 4 项环境参数,服务端按与 CLI 完全一致的
合法范围逐字段校验(错误文案逐条对齐),成功后复用 `gen_run_id`/`save_record` 生成
`run_id` 并保存到 `evidence/raw/{run_id}.json`(结构与 CLI 采集完全同构);历史面板
读取 `evidence/raw` 展示最近 50 条记录。历史读取容忍早期记录缺少 `source_type` 字段
与损坏文件(跳过并计数),全部文本以 `textContent` 渲染,防 XSS。

**降级提示横幅**:页面加载时未配置 API Key,或提问失败(密钥无效/网络超时等)时,
页面顶部展示降级提示横幅,并明确说明"本地新建采集与历史查看功能不受影响";
`/api/state` 与 `/api/switch` 响应仅返回 `llm.key_configured` 布尔标记,绝不返回密钥。

**失败验证(禁止伪造静态成功结果)**:成功路径只有一条——解析真实 API 返回的内容。
API Key 错误(HTTP 401/403)、网络超时/连接失败、返回内容无法解析等一律在
**页面错误框与 `logs/b01_llm.log` 日志**中明确输出失败(`error_type` + 中文说明),
绝不返回伪造的成功答案。注意:`/api/ask` 恒返回 HTTP 200,内层 JSON 的
`status` 字段才是成败信号(页面据此渲染成功/失败框)。

**日志**:`logs/b01_llm.log`(自动创建,UTF-8 追加):每次提问记录 `[请求]`(状态/问句/
环境值/Key 来源名,绝不记录 Key 值)、`[成功]`(回答)或 `[失败]`(error_type+message)、
状态切换记录 `[切换]`。

## B02 数据记忆模块

B02 已重构:由原"指标解析模块"重定为**数据记忆模块**(SQLite 时序持久化 + 查询统计 +
质量校验 + Web 可视化)。原 `b02_parse.py` 及 `evidence/parsed/` 旧产物已删除
(旧链路的弃用说明见下方 B07 一节)。

运行方式:

```bash
py b02_memory.py generate          # 生成 7 天模拟数据(幂等:已有数据不覆盖)
py b02_memory.py generate --force  # 强制重建(沿用旧锚点,数据一致)
py b02_memory.py current           # 查看当前环境状态(库内最新一条样本)
py b02_memory.py history --hours N # 查看最近 N 小时历史时序数据(默认 24)
py b02_memory.py stats [--hours N] # 窗口统计(默认全窗口,落盘样本统计)
py b02_memory.py check             # 质量校验(空值/重复/异常值/连续性/扰动分布)
py b02_memory.py web [--port N]    # 启动 Web 时间序列可视化页面(默认 8223)
py b02_memory.py --help            # 查看用法说明
```

**数据与 Schema**:`data/environment.db`(SQLite,重启后历史数据保留)。
表 `environment_samples`(30 分钟网格,7 天 336 点,4 项参数 + `sample_time` +
`source_type` + `sim_scenario` + `created_at`)与表 `b02_meta`(生成器配置追溯)。
Schema 完整说明见 `docs/b02_schema.md`。

**7 天模拟器**:昼夜基线公式 + 均匀噪声(种子 42,确定性、可复现)+ 5 段扰动片段
(4~6 段要求内,各占一个完整日历日,配方对齐 B03 规则 R1/R3/R6/R7 与 S3/STATE_B),
扰动片段均匀分布(相邻起始间隔 ≤40h、首末跨度 ≥96h)。生成规则详见
`docs/b02_simulator_rules.md`,扰动片段清单见 `docs/b02_disturbances.md` 与
`evidence/b02/disturbance_manifest.json`。

> **`sim_scenario` 仅为模拟器元数据,不能当作标签、不能作为基础特征。** 公开 API
> 使用显式列清单结构性隔离该字段,Web 页面亦不展示;仅质量检查内部用它做数据审计。

**公开接口(供 B06、B07 复用)**:

```python
get_current_environment()      # -> dict|None:{sample_time, source_type, temperature, humidity, co2, light}
get_environment_history(hours) # -> list[dict]:最近 N 小时(锚点=库内最新样本),升序
```

**质量校验**(`check`,10 项,退出码恒 0):表结构、行数==336、空值、重复、30 分钟网格
连续性、合法范围、离群点(相对生成模型的残差做 MAD 修正 z 分数,阈值 3.5)、扰动段
数量 4~6、扰动段均匀分布、元数据可追溯;报告落盘 `evidence/b02/quality_report.json`。
样本统计(全窗口/7天/3天/24小时)落盘 `evidence/b02/sample_stats.json`。

**Web 可视化**(`web`):时间范围筛选行(预设 6h/24h/3d/7d/全部 + 自定义起止时间)置顶,
4 张单轴折线图(温度/湿度/CO₂/光照)+ 最小二乘趋势线 + 当前状态卡 + 窗口统计表;
修改时间范围即触发 `/api` **真实 SQLite 时间窗查询**,曲线与统计表动态刷新。
Chart.js 4.4.9 为本地文件 `b02_assets/chart.umd.js`(离线可用;缺失时页面横幅降级,
统计表不受影响)。API:`/api/current`、`/api/history?hours=`、`/api/history_range?from=&to=`、
`/api/stats`、`/api/check`。

## B03 天气证据接口模块

B03 已重构:由原"规则判断模块"重定为**天气证据接口模块**(真实天气 Web API 接入 +
统一证据结构 + 来源记录 + 失败验证)。原 `b03_rule.py` 与 `evidence/rule/` 旧产物已删除;
专家规则库 `b03_rules.json` 保留(docs/b02_simulator_rules.md 引用其 R1~R8 阈值,
供后续模块复用)。

运行方式:

```bash
py b03_weather.py forecast 北京                    # 真实调用天气接口,打印并落盘证据
py b03_weather.py forecast 广州 --horizon 6        # 可选 --run-id/--provider/--horizon/--timeout
py b03_weather.py abtest                           # A/B 验证:同代码路径仅改地点(北京 vs 广州)
py b03_weather.py failtest                         # 失败验证:超时/错误参数/鉴权失败 三例
py b03_weather.py --help                           # 查看用法说明
```

接入的真实天气 API(**API 文档入口**):Open-Meteo https://open-meteo.com/en/docs(主,
免 key)、QWeather https://dev.qweather.com/docs/api/weather/weather-daily-forecast/
(可选,需 key;同时承担鉴权失败测试)。技术栈 requests(HTTP + JSON),无新增依赖。

**统一返回结构 weather_evidence**(成功与失败同构;失败时 forecast_time=null、
5 个数值字段全 null,**禁止伪造成功数据**):

| 字段 | 说明 |
|---|---|
| provider / retrieved_at / query | 数据来源、证据生成时刻、来源记录(密钥参数一律脱敏) |
| status / error | 成败标记;失败含 error_type + 接口真实错误摘要 |
| location / analysis_time / forecast_time | 地点、分析时刻、预报时刻(必晚于分析时刻) |
| temperature / humidity / precipitation_probability | 温度(℃)/ 湿度(%RH)/ 降水概率(%) |
| cloud_cover / shortwave_radiation | 扩展字段:云量(%)/ 短波辐射(W/m²) |

**验证规则**:
- A/B 测试(`abtest`):同一段代码、同一 provider/horizon/timeout,**仅 location 不同**,
  PASS 条件为两侧均成功且温度/湿度/降水概率至少一项真实不同(实测北京 8.8℃ vs 广州 25.4℃);
  天气恰好相同则如实 FAIL 附原因,绝不伪造差异。
- 失败测试(`failtest`):① 超时(timeout=0.0001s → network_timeout)② 错误参数
  (纬度 999 → Open-Meteo 真实 HTTP 400 → bad_param)③ 鉴权失败(QWeather 无效 key →
  真实 HTTP 403 → api_key_error);三例均断言 status=error、5 数值字段全 null、证据内嵌
  接口真实错误文本。

**公开接口(供 B07 复用)**:`get_weather_forecast(location, run_id=..., analysis_time=collect_time)`
→ weather_evidence;run_id 合法时落盘 `evidence/b03/{run_id}_weather.json`。

环境变量:`B03_WEATHER_TIMEOUT`(默认 10)、`B03_OPEN_METEO_BASE_URL`、
`B03_QWEATHER_BASE_URL`、`B03_QWEATHER_API_KEY`(可选)、`B03_LOCATION`(默认 北京)。

交付资产:`b03_weather.py`(可复用调用封装)、`docs/b03_weather_evidence.md`
(结构定义 + API 文档入口)、`evidence/b03/`(forecast/abtest/fail_* 证据)、
`logs/b03_weather.log`(每次调用两行日志)。

## B04 RAG 检索增强模块

B04 已重构:由原"机器学习异常检测模块"重定为**RAG 检索增强模块**(番茄专业知识库 +
稀疏向量检索 + LLM 引用回答)。原 `b04_ml.py` 与 `evidence/ml/` 旧产物已删除。

运行方式:

```bash
py b04_rag.py build                # 重建 chunk/索引(产出 source_register.json + index.json)
py b04_rag.py sources              # 查看 source 登记表
py b04_rag.py search <query>       # 纯检索(先验检索人工验证工具,不接 LLM)
py b04_rag.py ask <query>          # 完整 RAG:检索 → LLM 回答 + 引用
py b04_rag.py calibrate            # 阈值校准(检索分数分布 → 推荐阈值)
py b04_rag.py test                 # 先验检索测试(资料存在/缺失两类用例,无 LLM)
```

**知识库**:5 份自编模拟番茄种植资料(`knowledge/sources/`,阈值对齐 b03_rules.json 的
R1~R8 适宜带;登记中如实声明为课程项目自编模拟资料,不冒用出版机构)+
`knowledge/source_register.json`(source 登记,含 sha256)+ `knowledge/index.json`
(可重建 chunk/index:每 chunk 绑定 source_id,char_range 可追溯回原文;重建指纹恒定)。

**检索**(零依赖纯标准库):NFKC 归一化 + 字符 bigram/单字 + TF-IDF 稀疏向量 + 余弦
相似度;充分性只看 top1 分数 ≥ 阈值(实测覆盖类 0.21~0.44,未覆盖类 0.06~0.12,
校准阈值 0.17,env `B04_SCORE_THRESHOLD` 可覆盖)。

**资料不足边界**:检索不充分时输出【资料不足/无法确认】且**不调用 LLM**,禁止编造
答案与引用;citations 服务端从真实检索片段构建,每条可溯源回原文。

**LLM 引用回答**(`ask`):检索片段送入 DeepSeek 上下文,回答句末标注 [n];环境变量
`B04_LLM_BASE_URL` / `B04_LLM_MODEL` / `B04_LLM_TIMEOUT`,API Key 读取顺序
`DEEPSEEK_API_KEY` → `ANTHROPIC_AUTH_TOKEN`;无 key/调用失败如实上报,绝不伪造。

**公开接口(供 B07 复用)**:`search_tomato_knowledge(query) -> retrieval_evidence`
(Top-K 片段 + citation 来源),库调用无 key 依赖、只记日志不落盘。

交付资产:knowledge/source_register.json、knowledge/index.json(可重建)、
b04_rag.py(检索函数)、evidence/b04/(test_report.json 等测试证据)、
docs/b04_rag.md(结构定义与重建方法)。

## B05 工具智能体闭环模块

B05 已重构:由原"证据融合与可信降级模块"重定为**工具智能体闭环模块**(LLM 原生
function calling + B02/B03/B04 四工具真实执行)。原 `b05_fusion.py` 与 `evidence/final/`
旧融合产物已删除。

运行方式:

```bash
py b05_agent.py run <任务文本>      # 完整闭环:LLM 决策 → 工具执行 → 最终回答
py b05_agent.py tools              # 查看工具定义(TOOL_DEFINITIONS)
py b05_agent.py test               # 测试用例(2 单工具 + 2 多工具,需 API Key)
py b05_agent.py failtest           # 执行层失败验证(参数错误/空结果/超时,零 LLM)
```

**闭环机制**:LLM 输出 tool_call(**只决定调用哪个工具、参数是什么**)→ `dispatch_tool`
白名单调度 → Python 真实执行 B02/B03/B04 的工具函数 → tool_result 回传 LLM 继续判断,
直到输出最终回答(调用→执行→返回→继续)。

**四个标准工具**:

| 工具 | 来源模块 | 真实函数 |
|---|---|---|
| get_current_environment | B02 | b02_memory.get_current_environment |
| get_environment_history(hours=24) | B02 | b02_memory.get_environment_history |
| get_weather_forecast(location) | B03 | b03_weather.get_weather_forecast |
| search_tomato_knowledge(query) | B04 | b04_rag.search_tomato_knowledge |

**分支逻辑**(LLM 收到工具结果后判断):证据充足 → 直接输出最终回答;证据不足 →
继续调用其他工具;工具失败或 error/insufficient 证据 → 最终回答标注【证据缺失】降级,
不编造数据。

**日志强制保留**:每次调用记录 tool 名称、arguments、tool_result、sequence
(logs/b05_agent.log 每调用一行 [工具调用];完整调用链落盘 evidence/b05/agent_run_*.json);
没有真实执行、没有日志,不算完成 Tool Agent。

环境变量:`B05_LLM_BASE_URL` / `B05_LLM_MODEL`(deepseek-chat)/ `B05_LLM_TIMEOUT` /
`B05_MAX_STEPS`(默认 6);API Key 读取顺序 `DEEPSEEK_API_KEY` → `ANTHROPIC_AUTH_TOKEN`。

公开接口(供 B07 复用):`run_agent(task)` / `dispatch_tool(name, arguments)` /
`TOOL_DEFINITIONS`。交付资产与测试用例详见 docs/b05_agent.md。

> 注:MCP 外部工具连接为可选扩展,本模块基础实现不包含(见 docs/b05_agent.md 留白说明);
> 旧 b05_fusion.py 及 evidence/final/ 旧融合产物已删除。

## B06 传统机器学习闭环模块

B06 已重构:由原"多视图报告生成模块"重定为**传统机器学习闭环模块**(时间窗口预测 +
基线对比 + 错误样本分析 + 模型保存重载)。原 `b06_report.py` 与 `report/` 旧 HTML 已删除。

依赖安装(若未安装;requirements.txt 未改动,仅含 requests):

```bash
py -m pip install pandas scikit-learn
```

运行方式:

```bash
py b06_ml.py train                # 训练:窗口化 → 基线 → 随机森林 → 指标 → 错误分析 → 保存
py b06_ml.py eval                 # 重载模型重算测试集指标(不重训练,确定性证明)
py b06_ml.py predict [--sample-time "YYYY-MM-DD HH:MM:SS"]   # 对新样本推理(默认最新)
py b06_ml.py info                 # 查看模型版本/配置/指标
```

**数据与标签**:只读复用 B02 `data/environment.db`(mode=ro 连接,不改动原始数据);
输入窗口 = 最近 3 小时(6 个 30 分钟采样点),预测窗口 = 未来 1~2 小时(4 个采样点)。
标签 `next_window_attention` 为 **DERIVED_LABEL**(来源标注):预测窗口内任一采样点
违反适宜带(温度 15~30 / 湿度 45~85 / CO₂ 800~1500;光照仅白天 6≤h<18 检查
10000~70000,夜间 0 正常)→ WATCH,否则 NORMAL。**sim_scenario、原始天气、标签源
不作为基础特征**(SQL 列清单结构性排除 sim_scenario)。

**特征(26 个)**:4 参数 × current/mean/std/min/max/delta + hour_sin/hour_cos,
特征列表与模型一起保存。

**切分与防泄露**:时间边界 `2026-10-27 01:30`(前 5 天训练 235 窗 / 后 2 天测试 92 窗);
运行时 3 项断言(训练锚点全部早于测试锚点、锚点集合不相交、特征行哈希无交集——
测试集绝不参与训练)。

**指标对比(实测)**:

| 模型 | accuracy | macro-F1 | WATCH recall |
|---|---|---|---|
| 基线(多数类) | 0.6739 | 0.4026 | 0.0000 |
| 随机森林(seed=42, balanced) | 0.8696 | 0.8600 | 0.9333 |

混淆矩阵 `[[52,10],[2,28]]`;判定 = 模型 macro-F1 超基线 ✓。

**错误样本分析**:FP(10-28 09:00 黎明弱光误报)与 FN(10-27 11:00 扰动起始突变漏报)
各 1 例,程序化中文解释,落盘 evidence/b06/error_analysis.json。

**模型保存重载**:models/b06/model.joblib + feature_list.json + config.json(版本
自动递增 B06-ML-vN);完全重启后 `eval`/`predict` 仅加载不训练(已实测指标逐位一致)。

**公开接口(供 B07 复用)**:`predict_next_attention(sample_time=None)` →
{prediction, probability, model_version, **MODEL_OUTPUT="B06_ML"**, window, features,
label_source, status};错误变体(model_missing/model_corrupt/insufficient_history/
db_unavailable)如实返回,退出码恒 0。

业务边界:不使用深度学习/YOLO 大模型;DERIVED_LABEL 标签来源必须标注;
不改动 B02 原始数据。交付资产详见 docs/b06_ml.md。

## B07 主程序(统一入口流水线)

运行方式:

```bash
py b07_main.py              # 完整流水线:交互采集后自动执行 B02~B06
py b07_main.py run_xxx      # 对已有 run_id 从解析阶段开始重跑(跳过采集,幂等覆盖)
py b07_main.py --help       # 查看用法说明
```

> **注意:B02、B03、B04、B05、B06 已重构。** `b02_parse.py`、`b03_rule.py`、`b04_ml.py`、
> `b05_fusion.py`、`b06_report.py` 已删除(连带其 `__pycache__` 字节码与
> `evidence/parsed/`、`evidence/rule/`、`evidence/ml/`、`evidence/final/`、
> `report/` 旧产物),B07 旧链路(B01 采集 → B02 解析 → B03 规则 → B04 ML →
> B05 融合 → B06 报告)随之弃用,当前 `py b07_main.py` 会因 `import b02_parse`
> 失败而无法运行。
> 新 B02(数据记忆)、新 B03(天气证据)、新 B04(RAG 检索增强)、新 B05(工具智能体,
> 整合四工具闭环)与新 B06(传统机器学习闭环,只读复用 B02 时序数据)均独立运行、
> 不参与旧链路,其公开 API(`get_current_environment` / `get_environment_history` /
> `get_weather_forecast` / `search_tomato_knowledge` / `run_agent` / `dispatch_tool` /
> `predict_next_attention`)预留给后续 B07 重写时复用。

流水线六个阶段(全程使用同一个 run_id):

| 阶段 | 内容 | 模块 | 产物 |
|---|---|---|---|
| 1/6 | 采集输入 | B01 | `evidence/raw/{run_id}.json` |
| 2/6 | 解析提取证据 | B02 | `evidence/parsed/{run_id}_parsed.json` |
| 3/6 | 专家规则判断 | B03 | (已移除:b03_rule.py 删除,见上方警告) |
| 4/6 | ML 异常检测 | B04 | (已移除:b04_ml.py 删除,见上方警告) |
| 5/6 | 证据融合可信降级 | B05 | (已移除:b05_fusion.py 删除;新 B05 工具智能体独立运行,不产出 final) |
| 6/6 | 生成 HTML 报告 | B06 | (已移除:b06_report.py 删除;新 B06 传统机器学习闭环独立运行,见 B06 一节) |

**降级继续策略**:阶段 2~6 任一失败(如解析/规则/ML/融合证据已随模块移除而恒缺失)
打印警告并继续——B05 自动把缺失证据标注【证据缺失】并降级可信度,阶段 6 恒缺失、
B08 报缺失;阶段 1 取消或保存失败则终止。

## B08 整体交付验证脚本

运行方式:

```bash
py b08_verify.py run_20261022_220400_485a96   # 直接校验指定 run_id
py b08_verify.py                              # 不带参数:列出可校验的 run_id 交互选择
```

校验内容:对指定 run_id 逐一检查 6 个交付产物 —— `evidence/raw/{run_id}.json`、
`evidence/parsed/{run_id}_parsed.json`、`evidence/rule/{run_id}_rule.json`、
`evidence/ml/{run_id}_ml.json`、`evidence/final/{run_id}_final.json`、
`report/{run_id}_report.html`,输出三态清单:

| 状态 | 含义 |
|---|---|
| ✓ OK | 文件存在且内容有效(JSON 可解析且 run_id 字段一致;HTML 非空) |
| ✗ 缺失 | 文件不存在 |
| ⚠ 异常 | 文件存在但内容无效(JSON 损坏 / run_id 不一致 / HTML 为空) |

每个文件附带 SHA-256 内容摘要,并汇总出确定性「结果指纹」(不含校验时间):
**重复运行同一 run_id,结果指纹一致即确认结果可复现、证据未被改动**。

> 注:`evidence/parsed/`、`evidence/rule/`、`evidence/ml/`、`evidence/final/` 旧证据与
> `report/` HTML 已随模块重构删除,b08 对这些项当前恒报"缺失"属预期;b08 代码本轮未改。

模块链路:B01 采集 → B02 数据记忆(SQLite 持久化时序数据,独立运行)→ B03 天气证据
(真实天气 API,独立运行,`get_weather_forecast` 供 B07 复用)→ B04 RAG 检索增强
(番茄知识库检索,独立运行,`search_tomato_knowledge` 供 B07 复用)→ B05 工具智能体
(整合 B02~B04 四工具闭环,`run_agent`/`dispatch_tool` 供 B07 复用,独立运行,不产出
final)→ 新 B06 传统机器学习闭环(只读复用 B02 时序数据训练/推理,独立运行,
`predict_next_attention` 供 B07 复用);旧链路 B02 解析 → B03 规则 / B04 ML → B05 融合
→ B06 报告(B07 组装)已弃用(详见 B07 一节警告),B06 报告生成当前不可用属预期;
B08 校验交付产物完整性,结果指纹保证可复现。旧解析/规则/ML/final 证据与 HTML 报告
现为缺失属预期(b05/b06 按【证据缺失】降级,B08 报"缺失",本轮不改这些模块的代码)。

## 测试场景

| 场景 | 说明 | 数据文件 | 预期 | 运行命令 |
|---|---|---|---|---|
| S1 | 标准适宜环境:四项指标全在适宜区间 | `testdata/s1_standard.json` | B02 全适宜、B03 正常、最终结论正常 | `printf '25\n63\n850\n31000\n' \| py b07_main.py` |
| S2 | 单项指标偏高(温度偏高) | `testdata/s2_high_temperature.json` | B02 温度偏高、B03 触发 R1 需要预警、最终结论需要预警 | `printf '32\n63\n850\n31000\n' \| py b07_main.py` |
| S3 | 多项指标异常(高温高湿弱光) | `testdata/s3_multi_abnormal.json` | B02 温度/湿度偏高+光照偏低、B03 触发 R1/R3/R7 需要预警、最终结论需要预警 | `printf '33\n88\n900\n8000\n' \| py b07_main.py` |
| S5 | 临界边界值(恰在分界线上) | `testdata/s5_boundary.json` | 闭区间语义:B02 四项全适宜、B03 正常、最终结论正常 | `printf '30\n45\n1500\n10000\n' \| py b07_main.py` |

> 注:S1~S5 预期中的 "B02 判定" 与 "B03 触发 R1/R3/R7" 均指旧版解析/规则模块(已移除)。
> 新 B02 的验证方式为 `py b02_memory.py check`(10 项质量校验)与 `py b02_memory.py stats`
> (样本统计);新 B03 的验证方式为 `py b03_weather.py abtest`(A/B 真实差异)与
> `py b03_weather.py failtest`(三例失败验证);新 B04 的验证方式为
> `py b04_rag.py test`(先验检索测试:资料存在/缺失两类用例)与 `py b04_rag.py ask`(引用回答);
> 新 B05 的验证方式为 `py b05_agent.py test`(单/多工具闭环用例,需 API Key)与
> `py b05_agent.py failtest`(执行层失败验证,无需 Key);新 B06 的验证方式为
> `py b06_ml.py train`(基线对比 + 错误样本分析)与 `py b06_ml.py eval`/`predict`
> (模型重载与重启推理)。

## 任务码映射

| 任务码 | 说明 | 文件 |
|---|---|---|
| B01 | 智能交互模块(交互输入 + 原始数据保存) | b01_cli.py |
| B02 | 数据记忆模块(SQLite 时序持久化 + 查询统计 + 质量校验 + 可视化) | b02_memory.py |
| B03 | 天气证据接口模块(真实天气 API + 统一证据结构 + A/B 与失败验证) | b03_weather.py |
| B04 | RAG 检索增强模块(番茄知识库 + 稀疏向量检索 + LLM 引用回答) | b04_rag.py |
| B05 | 工具智能体闭环模块(LLM 工具调用闭环 + 四工具真实执行 + 强制调用日志) | b05_agent.py |
| B06 | 传统机器学习闭环模块(时间窗预测 + 基线对比 + 错误样本分析 + 模型保存重载) | b06_ml.py |
| B07 | 主程序组装模块(统一入口流水线) | b07_main.py |
| B08 | 整体交付验证脚本(完整性校验 + 结果指纹) | b08_verify.py |
