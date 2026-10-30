# B07 系统组装模块 — 四遍法 / 结构化判断 / 四视图报告说明

实现:`b07_main.py`(编排 + 结构化判断引擎 + LLM 解释 + 日志追踪 + CLI)+
`b07_report.py`(4 视图 HTML 报告)。B07 只组装 B01~B06 已完成资产,**不新增
基础能力**;场景配置位于 `测试配置/scenarios/`。

## 四遍法映射

| 遍 | 内容 | 落点 |
|---|---|---|
| ① 场景组装 | 沿 S1~S5 串联完整运行链路,复用 B01~B06 全部公开接口 | 8 次工具调用(见下) |
| ② 机制理解 | 连续状态认知(历史窗口 + 趋势双条款)、多源证据组织(4 类标记)、可信降级 | `judge_environment` 引擎 |
| ③ 产品化呈现 | 状态总览 / 状态演化 / 证据空间 / 场景回放对比 4 视图 | `b07_report.py` |
| ④ 边界判断 | 能力边界说明 + 无法处理场景显式降级 | `capability_boundary_notes` + 降级原因 |

## 业务执行顺序(硬性约束)

```
工具结果 → 证据记录 → 结构化判断 → LLM 自然语言解释
```

- **工具结果**(seq 1~8):B02 当前环境(仅上下文)→ B02 锚点样本(判断依据)→
  B01 原始存档(save_record,collect_time=锚点时间)→ B02 历史窗口 → B02 趋势统计 →
  B03 天气(真实 API,PUBLIC)→ B04 知识检索 → B06 模型预测。
- **证据记录**(EV01~EV08):与工具结果**分离**的可引用形式,每条含
  seq / evidence_id / source / source_marker / 摘要(≤200 字)/ status
  (ok / error / insufficient)/ detail,落盘 evidence_log.json。
- **结构化判断**:确定性引擎,落盘 structured_judgment.json,**可保存、可对比**;
  LLM 解释永远在判断落盘之后、且只存 trace——**LLM 仅解释,绝不覆盖判断**。

## 证据来源标记

| 标记 | 来源 | 说明 |
|---|---|---|
| SIMULATED | B01 原始记录、B02 模拟数据、B04 自编知识库 | 环境数据均为课程模拟,系统不声称真实监测 |
| PUBLIC | B03 真实天气 API | 标记由 B07 附加(b03 自身不输出标记串) |
| DERIVED_LABEL | 规则 R1~R8 与趋势派生判定 | 来源串与 B06 一致:阈值表 b03_rules.json,源=environment_samples 原始参数值 |
| MODEL_OUTPUT | B06 预测(B06_ML) | 其 label_source 字段在 trace 中携带 DERIVED_LABEL 串 |

## 场景配置 schema 与 5 场景

```json
{"scenario_id":"S1","name":"稳态基线","description":"...",
 "anchor_sample_time":"2026-10-28 12:00:00","history_hours":24,"trend_hours":24,
 "location":"北京","knowledge_query":"番茄适宜生长温度是多少",
 "degrade":{"weather":null,"knowledge":null},"llm_explain":true,
 "experiment_type":null,
 "sources":{"history":true,"weather":"real","knowledge":true,"prediction":true},
 "expected":{"conclusion":"正常","attention_level":"normal","trust":"高","note":"..."}}
```

`degrade.weather="timeout"` 强制 timeout=(0.0001,0.0001) → 真实 network_timeout
证据;`degrade.knowledge="uncovered"` 配合未覆盖问题 → 真实【资料不足/无法确认】。
`expected` 为信息字段,供 B08 下一轮逐场景对比。
`experiment_type/sources` 为案例A 第二遍实验字段:数据源开关(weather 可取
"real"/"injected_cooling"/"off"),disabled 来源计入边界说明与证据完整度、
不计入降级;实验预设位于 `测试配置/experiments/`(见 文档/b07_pass23.md)。

| 场景 | 锚点(实测) | 关键证据 | 预期结论/关注级/信任 |
|---|---|---|---|
| S1 稳态基线 | 10-28 12:00(27.6/54.0/923/59910) | 规则无违规 + 模型 NORMAL + 趋势平稳 | 正常/normal/高 |
| S2 渐进漂移 | 10-27 12:30(全适宜) | 2.5h 趋势四参数超幅值阈值(温度 +0.97/h 等)+ 模型 WATCH 0.58 | 需要关注/attention/中 |
| S3 外部扰动 | 10-27 13:30(32.9/87.0/8254) | R1+R3+R7 与模型 WATCH 一致 | 需要预警/warning/高 |
| S4 多源证据分歧 | 10-28 09:00(全适宜) | 规则 NORMAL vs 模型 WATCH 0.84(黎明弱光误报) | 需要关注/attention/中 |
| S5 证据退化 | 10-28 12:00(同 S1) | 天气超时 + 知识未覆盖,两条【证据缺失】 | 正常/normal/低 |

## 结构化判断引擎规则

- **逐参数**:temp >30→R1 偏高 / <15→R2 偏低;hum >85→R3 / <45→R4;
  co2 >1500→R6 / <800→R5;light 仅白天 6≤h<18 检查(>70000→R8 / <10000→R7),
  夜间"夜间不检查"并记边界说明。阈值复用 `b06_ml.BANDS / DAYLIGHT_RANGE`。
- **趋势双条款**(b02_memory.stats_window 的 slope_per_hour s,锚点值 v):
  ① 外推:`v + 2·s` 超出适宜带(2 小时外推,对齐 B06 预测窗);
  ② 幅值:`|s| ≥` 温度 0.5 / 湿度 1.0 / CO₂ 25 / 光照 2000(每小时)。
  光照夜间跳过。任一参数命中即 trend_alert。
- **融合表**:规则命中 → 需要预警/warning;否则 模型 WATCH 或趋势命中 →
  需要关注/attention;否则 正常/normal。
- **信任**:任一来源退化 → 低 + 逐条【证据缺失】(天气失败/知识不足/模型错误/
  锚点缺失/统计空窗);健康来源分歧(规则 vs 模型)→ 中 + 保守升级;一致 → 高。

## 四视图报告(报告/{run_id}_report.html,自包含,file:// 可用)

| 视图 | 内容 | 技术要点 |
|---|---|---|
| ① 状态总览 | 结论横幅(按关注级着色)+ 信任徽章 + 标记 chips 计数 + 4 参数卡(值/适宜带/判定/规则建议)+ LLM 解释 + 降级与边界清单 | 文本注入全部经 html.escape / textContent |
| ② 状态演化 | 4 参数 small-multiple 折线图 + 窗口统计表 | Chart.js 经 `../前端资源/chart.umd.js` 相对路径离线加载;锚点竖虚线为自注册插件 anchorLine(afterDatasetsDraw 按 x 像素绘制,不受 y 轴范围影响) |
| ③ 证据空间 | 证据记录表 + 工具结果表(含嵌套状态)+ structured_judgment.json pretty | 状态着色:ok/error/insufficient |
| ④ 场景回放对比 | 本场景配置回显 + 实际 vs 预期 + 重跑命令 + 全部历史运行对比表 | 对比表渲染时扫描 证据/b07/*/structured_judgment.json;每次重跑新 run_id |

Chart.js 缺失时降级横幅提示,表格与文本照常渲染;明暗主题沿用项目 CSS 变量调色板。

## 能力边界(judgment.capability_boundary_notes)

- 真实传感器不可用:环境数据均为课程模拟(SIMULATED),不声称真实监测;
- 不可处理:长期天气预报(>1 天)、跨域农业问题、真实设备控制——只输出判断与解释,
  不执行任何控制动作;
- 天气证据不可用 → 结论仅基于棚内模拟证据并标注【证据缺失】;
- 知识库未覆盖 → 【资料不足/无法确认】,不编造、不伪造引用;
- 夜间光照规则不适用;模型缺失 → 判断降级为仅规则+趋势。

## 统一 run_id 与产物

单次全流程一个 run_id(`b01_cli.gen_run_id()`,`--run-id` 须匹配
`run_YYYYmmdd_HHMMSS_<6位hex>`,非法换新、目录已存在覆盖重跑):

| 产物 | 路径 |
|---|---|
| 全序追踪(工具结果 + 证据 + 判断 + 解释) | `证据/b07/{run_id}/trace.json` |
| 结构化判断(可保存可对比) | `证据/b07/{run_id}/structured_judgment.json` |
| 证据记录 | `证据/b07/{run_id}/evidence_log.json` |
| 场景配置 + 实际 + 预期 | `证据/b07/{run_id}/scenario.json` |
| B08 契约输入(run_result.json:raw/parsed 内容 + rule/ml/final null 占位) | `证据/b07/{run_id}/run_result.json` |
| HTML 报告(展示;新 b08 契约不再校验 HTML) | `报告/{run_id}_report.html` |
| B01 原始采集记录(经 b01.save_record;内容嵌入 run_result.json.raw) | `证据/raw/{run_id}.json` |
| 天气证据(b03 附带产物) | `证据/b03/{run_id}_weather.json` |
| 流程日志(每行镜像控制台) | `日志/b07_main.log` |

日志行格式:`[场景]` / `[工具] seq= tool= marker= status=[nested=]` / `[证据]` /
`[判断] conclusion= attention_level= trust= matched_rules= degraded=` / `[解释]` /
`[完成]`。B07 绝不写入 日志/b05_agent.log(B05 failtest 按行增量计数)。

## LLM 解释(B07 自持)

B05 的 `_call_llm_with_tools` 恒带 tools、B04 的 `_call_llm` 为私有函数,均不适配
——B07 自持简单 chat 调用(无 tools 载荷)。系统提示词要求:只能解释给定结构化判断
与证据,不得改变/否定/补充结论、不得编造数据。失败分类与 b01/b05 同源
(missing_dependency / no_api_key / network_timeout / api_key_error / http_error /
bad_response)→ `{status:"omitted"}`;`llm_explain=false` → skipped。环境变量:
`B07_LLM_BASE_URL`(默认 api.deepseek.com/chat/completions)、`B07_LLM_MODEL`
(deepseek-chat)、`B07_LLM_TIMEOUT`(30);Key 读 `DEEPSEEK_API_KEY` →
`ANTHROPIC_AUTH_TOKEN`,绝不打印 key 值。

## B08 验证说明

B07 步骤⑥ 落盘 `证据/b07/{run_id}/run_result.json`(B08 新契约唯一输入):
raw(嵌入 证据/raw 完整记录)/ parsed(锚点行 + 历史窗口行)/ rule / ml / final
(本轮显式 null 占位)。校验命令 `py b08_verify.py --json-path <路径>`:
raw/parsed 必须存在且形状有效;rule/ml/final 缺失(键不存在/null/空对象)标记
「本轮预期缺失」不判错误;输出五项清单 + 文件 sha256 + 校验指纹,重复运行一致
即确认稳定可复现。场景配置的 `expected` 字段 + `scenario.json` 的 actuals 供 B08
逐场景对比。完整契约与校验细则见 文档/b08_verify.md。

## 运行与复现

```bash
py b07_main.py run S1 [--run-id run_x]   # 单场景
py b07_main.py run-all                   # S1→S5 顺序执行 + 对比表
py b07_main.py report <run_id>           # 重新生成 HTML(同输入字节一致,已实测 sha256 相同)
py b07_main.py list                      # 历史运行列表
py b08_verify.py --json-path 证据/b07/<run_id>/run_result.json   # 契约校验(raw/parsed OK,rule/ml/final 预期缺失)

# 案例A 第二遍·第三遍(详见 文档/b07_pass23.md)
py b07_main.py exp <type>                # 一键生成实验 run(run_<type>_<seq>)
py b07_main.py exp-all                   # 6 组实验 + 两张对比表
py b07_main.py trace <run_id> "<结论>"   # 创新点2 溯源 + B05 工具调用校验
py b07_main.py check <run_id>...         # 创新点3 校验(分歧保留/天气不编造/视图一致/溯源自检)
py b07_main.py check-all                 # 全部实验 run + 汇总表
py b07_main.py selftest                  # 校验函数负向自测(11 项)
```

run-all 含 4 次真实天气 HTTP 调用(约 1~3 秒/次);断网时如实降级为【证据缺失】,
绝不伪造成功。实测锚点结论:S1 正常/高、S2 需要关注/中、S3 需要预警/高(R1,R3,R7)、
S4 需要关注/中(规则 vs 模型分歧)、S5 正常/低(天气 timeout + 知识未覆盖两条降级)。

## 交付资产

| 资产 | 路径 |
|---|---|
| 完整串联调度主程序 | b07_main.py(编排 + 判断引擎 + LLM 解释 + 日志追踪 + CLI + 实验框架/校验/溯源) |
| S1~S5 场景配置 | 测试配置/scenarios/s1_baseline.json ~ s5_degraded.json |
| 实验预设(案例A 第二遍) | 测试配置/experiments/exp_agree.json ~ exp_with_history_prediction.json |
| 可视化视图代码 | b07_report.py(4 视图 HTML 模板 + 渲染 + anchorLine/eventLines 插件) |
| 日志追踪代码 | b07_main.py `_log_b07` + 日志/b07_main.log |
| 运行时证据 | 证据/b07/{run_id}/(trace / structured_judgment / evidence_log / scenario / run_result)+ 报告/{run_id}_report.html |
| 第二三遍文档(命令清单 + 截图采集清单) | 文档/b07_pass23.md |
| 本文档 | 文档/b07_main.md |
