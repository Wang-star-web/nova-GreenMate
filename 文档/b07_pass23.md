# B07+ 案例A 第二遍·第三遍 — 可复现实验框架 + 4 产品视图层

实现:`b07_main.py`(实验框架/证据模型/校验/溯源/CLI 扩展)+ `b07_report.py`(四视图
扩展 + eventLines 插件)。**不新增硬件/大模型能力**,只在 B01~B08 已有资产上增加
可复现实验框架与视图层。实验预设:`测试配置/experiments/exp_*.json` ×6。

## ① 顶层规则(全程遵守)

1. **唯一 run_id 全量存档**:每次运行绑定唯一 run_id,持久保存 run_id、timestamp、
   输入数据源配置(历史/天气/知识/预测 是否启用)、原始传感器时序、B05 工具调用
   记录(输入/返回/成功与否)、B06 预测、证据列表、最终状态判断、关注等级、LLM
   完整输出——全部在 `证据/b07/{run_id}/trace.json` 与配套产物中。
2. **只读存档**:任何页面/对比查看只读取已保存 run_id 的存档数据,**禁止重新调用
   API、禁止重新跑 LLM 推理**(报告为静态 HTML,渲染零网络请求,实测断言通过)。
3. **数据源开关**:通过配置拿掉/恢复数据源生成不同 run(见 §②)。
4. **四类证据不可互相替代**:历史=已发生事实 / 天气=未来外部条件 / 知识=静态规则 /
   预测=短时推演;每份证据自报身份(类型/来源/获取时间/可用性/结果,可选 error/
   citation/model_version);LLM 禁止编造未调用工具的结果,缺失来源必须标注
   【证据缺失】并收窄判断边界。
5. **趋势≠预测**:趋势(已发生)存 trend_findings,预测(未来窗口)存 B06 证据,
   分开存储、分句表述、禁止合并。

## ② 实验预设(6 组)与 run_id 命名

| 实验 | 锚点(实测) | sources | 期望结论/关注级/信任 | 用途 |
|---|---|---|---|---|
| agree | S1 10-28 12:00(全适宜) | 全开,24/24h | 正常/normal/高 | 创新点3 对照组:证据全一致 |
| conflict | S2 10-27 12:30(全适宜) | weather=injected_cooling,3/3h | 需要关注/attention/中 | 创新点3:历史升温 vs 注入天气降温,保留分歧 |
| missing_weather | S1 10-28 12:00 | weather=off | 正常/normal/高 | 创新点3:天气缺失,边界收窄 |
| current_only | S2 10-27 12:30 | history=false,prediction=false | 正常/normal/高 | 创新点1 组1:仅当前值 |
| with_history | S2 10-27 12:30 | prediction=false,3/3h | 需要关注/attention/高 | 创新点1 组2:+3h 历史识别升温 |
| with_history_prediction | S2 10-27 12:30 | 全开,3/3h | 需要关注/attention/中 | 创新点1 组3:+B06 短时预测 |

- run_id 可读命名 `run_{type}_{seq:03d}`(如 run_agree_001),同类型重跑序号自动
  递增(`_next_exp_seq` 扫描 证据/b07/);`_valid_run_id` 双格式放行时间戳格式。
- **agree 必须用 24h 窗口**(实测:S1 锚点 3h 趋势四参数超阈值会翻车变「需要关注」);
  3h 窗口只用于 S2 系实验(S2 3h 实测 +3.1℃ 升温,构成"历史持续升温"叙事)。
- 注入天气数值:温度 18℃/湿度 88%/降水概率 80%/云量 90%/辐射 250 W/m²,
  forecast_time=锚点+2h(强制 > analysis_time)。

## ③ 数据源开关语义

- `sources.weather`: `real`(真实 API,PUBLIC)/ `injected_cooling`(实验注入,SIMULATED,
  自报 provider=experiment_injection,query.method=INJECTION,**绝不冒充 PUBLIC**,
  不写 证据/b03/)/ `off`(不调用)。
- history/知识库/prediction: `false` → 对应工具替换为 status="disabled" 占位记录
  (与 b07_tool_call 同构,消费方零特判)。
- **disabled ≠ 降级**:配置关闭的来源不计入 degraded_reasons(不拉低信任),改记
  capability_boundary_notes 边界说明 + 证据记录 status="disabled" + 证据完整度
  evidence_completeness(N/4,disabled 列表)。
- run_result.json:history 关闭时 parsed.history 退用锚点单行,b08 契约不破坏。

## ④ 证据模型与关系规则

证据记录字段:`evidence_id / source / source_marker / tool / evidence_type
{current,raw,history,trend,weather,knowledge,prediction} / evidence_time / status
{ok,error,insufficient,disabled} / summary / detail(error/citation/model_version) /
relation{支持,限制,中性} / relation_reason`。

relation 判定(确定性,compute_evidence_relations):
- 结论 需要预警/需要关注:趋势 alert、预测 WATCH、历史/当前/原始 ok → **支持**;
  趋势无 alert、预测 NORMAL → **限制**;天气 ok → **中性**(注入降温 → **限制**+
  分歧理由);知识 ok → **中性**;error/insufficient/disabled → **限制**。
- 结论 正常:方向相反(alert/WATCH → 限制,平稳/NORMAL → 支持)。

## ⑤ 事件标注算法(视图2)

`find_persistent_drift_start(rows, key, delta)`:首个样本 i,满足 |v[i]-v[0]| ≥ delta
且其后所有样本保持同方向 ≥ delta(纯数值比较,确定性)。阈值与趋势条款同量纲:
温度 0.5℃/湿度 1.0%RH/CO₂ 25ppm/光照 2000lux;光照仅白天。
`compute_events` 产出 trend_start(四参数)/ model_watch / weather(或 weather_missing)/
attention_raised / rule_hit,按 (sample_time, 优先级) 排序存入 trace.steps.events;
history 关闭或锚点缺失 → 降级 note,视图2 空态说明。

## ⑥ LLM 提示词与输出校验

提示词硬性规则(6 条):只依据给定分块输入、不改变结构化判断、不编造;四类证据
分别标注类型与来源标记;趋势(已发生)与预测(未来)分句;缺失标注【证据缺失】
并收窄边界;冲突保留分歧、禁强确定词;输出结构 ≤400 字。输入分块:【数据源配置】
【锚点观测】【历史窗口(已发生)】【趋势统计(已发生)】【模型预测(未来1~2h)】
【天气证据(未来外部条件)或【证据缺失】】【知识证据】【证据清单】【结构化判断】。

`validate_llm_output`(纯函数,零 LLM 零网络):
| 检查项 | 判定 |
|---|---|
| 强确定词 | 8 词正则(负向后视排除"不一定/未必");证据不足(降级/分歧/来源关闭)→ fail;证据充分 → pass+warning |
| 天气不编造 | 文本有天气断言但 weather=off 或无 b03 成功调用 → fail |
| 工具调用校验 | 文本声称某来源结果但对应工具无成功记录 → fail |
| 关闭来源不编造 | prediction/history 关闭但文本出现预测/历史表述 → fail |
| LLM 缺失 | text=None → SKIP(诚实说明),结构类检查仍执行 |

## ⑦ 四视图(同一 run_id,只读存档,零重推理)

| 视图 | 新增内容 |
|---|---|
| ① 状态总览 | 数据源配置 chips(开/关/注入)、证据完整度 N/4、原始数值 vs 系统理解对照块(趋势标【已发生】、预测标【未来1~2h】、关闭来源显示"—(数据源关闭)") |
| ② 状态演化 | 3h 曲线 + 关键事件竖线标注(eventLines 插件,按 parameter 过滤到对应参数图,顶部堆叠标签)+ 事件清单表;history=false 空态说明 |
| ③ 证据空间 | 证据表加 类型/自报时间/关系(支持·限制·中性)三列;disabled 状态"关闭";工具结果表 |
| ④ 场景回放对比 | 创新点3 三列并排对比 + 创新点1 三列并排对比(渲染时扫 证据/b07/*/scenario.json,只读存档)、B05 工具调用回放表、LLM 校验卡片 |

## ⑧ 命令清单(终端执行)

```bash
# 一键生成(每个 run 独立 run_id,全程日志 日志/b07_main.log)
py b07_main.py exp agree|conflict|missing_weather|current_only|with_history|with_history_prediction
py b07_main.py exp-all                        # 6 组全生成 + 创新点3/创新点1 两张对比表

# 查询存档(只读,不重推理)
py b07_main.py list                           # 全部历史运行(含实验类型)
py b07_main.py report <run_id>                # 重新生成 HTML 报告(同输入字节一致)
py b07_main.py trace <run_id> "<结论文本>"    # 创新点2 溯源:证据清单(支持/限制/中性)+ B05 工具调用校验

# 校验检查(零 LLM 零网络;LLM 未生成时文本类检查 SKIP)
py b07_main.py check <run_id>...              # 四项:分歧保留/天气不编造/视图一致/溯源自检
py b07_main.py check-all                      # 全部实验 run + 汇总表
py b07_main.py selftest                       # 校验函数负向自测(11 项)

# 兼容原有命令
py b07_main.py run S1..S5 / run-all / --help
py b08_verify.py --json-path 证据/b07/<run_id>/run_result.json

# 整合门户(b01~s5 全部网页统一首页)
py b09_portal.py              # 生成/刷新 门户页面/index.html(报告列表来自存档,只读)
py b09_portal.py serve        # 本地门户 http://127.0.0.1:8300(报告链接一体可达)
```

注:LLM 解释读 `B07_LLM_BASE_URL / B07_LLM_MODEL / B07_LLM_TIMEOUT`,API Key 读
`DEEPSEEK_API_KEY → ANTHROPIC_AUTH_TOKEN`;无 Key 时 omitted,结构化判断与校验
不受影响。exp-all 含 5 次真实天气 HTTP 调用,断网时如实降级。

## ⑨ 截图采集清单(13 张)

终端(5 张):
1. `py b07_main.py exp-all` 完整输出(6 组生成日志 + 两张对比表);
2. `py b07_main.py trace run_conflict_001 "需要关注"`(证据清单 支持6/限制1/中性1,
   含 EV06 注入天气「限制」行);
3. `py b07_main.py check-all`(四项检查输出 + 汇总表);
4. `py b07_main.py check run_missing_weather_001`(天气不编造 PASS;无 Key 时 SKIP
   诚实说明,两张都截);
5. `py b07_main.py list`(可读命名 run_id 列表)。

HTML 报告(8 张,浏览器打开 报告/ 下文件,file:// 可用):
6. run_agree_001 视图①:结论横幅 正常/高 + 完整度 4/4 + 数据源 chips + 原始数值 vs
   系统理解对照;
7. run_conflict_001 视图②:3h 曲线 + 事件竖线标注(升温起点 10:00/B06 WATCH/注入
   天气/关注级提升)+ 事件表;
8. run_conflict_001 视图③:证据表(类型/时间/关系列,EV06 实验注入 SIMULATED 行);
9. run_missing_weather_001 视图①:完整度 3/4 + 天气「关闭」chip + 边界说明;
10. run_current_only_001 视图②:history=false 空态说明;
11. run_with_history_001 视图②:3h 7 点曲线 + 升温起点事件(无 B06 事件);
12. run_with_history_prediction_001 视图④:创新点3/创新点1 并排对比表 + B05 工具
    调用回放 + LLM 校验卡片;
13. run_agree_001 视图④:并排对比(agree/conflict/missing_weather 三列)。

## ⑩ 验证矩阵与实测记录

| 检查 | 结果 |
|---|---|
| selftest 11 项(强词 fail/负向后视/天气编造/漂移起点/夜间光照跳过/关系规则/溯源) | 11/11 ✓ |
| 6 组实验结论/关注级/信任 | agree 正常/高、conflict 需要关注/中(分歧×3)、missing_weather 正常/高、current_only 正常/高、with_history 需要关注/高、with_history_prediction 需要关注/中 ✓ |
| check-all | 6 run 全部 0 FAIL(SKIP=LLM 文本类检查,无 Key 诚实降级) ✓ |
| 视图一致性 | 报告两次重生成 sha256 一致;HTML 内嵌 LLM 文本==存档;无运行时网络加载构造 ✓ |
| b08 契约 | current_only(parsed.history=1 行)/with_history(7 行)均 2 OK / 3 预期缺失 ✓ |
| 回归 | S1~S5 结论与改造前一致;旧 trace 报告重生成兼容;DB sha256=916fd654…;b05_agent.log 行数不变 ✓ |
| GBK 管道 | check-all/list 重定向无崩溃 ✓ |

## ⑪ 风险与边界

- **注入天气 vs B03"绝不伪造成功"原则**:注入路径完全不经过 b03_weather 的成功
  路径(真实 API 解析 + forecast>analysis 强校验未触碰);注入证据自报
  provider=experiment_injection、source_marker=SIMULATED、query.method=INJECTION
  指向 exp JSON;不写 证据/b03/;报告/视图/LLM 输入均显式标注"实验注入,
  非真实天气"。B03 模块与实证目录零污染。
- **可读 run_id**:b07 双格式放行;b08 无格式限制;b03 不落盘实验 run 天气证据
  (其内部 RUN_ID_RE 不匹配,证据已在 trace 归档)。
- **信任语义**:disabled 来源不计入 degraded(配置关闭 ≠ 证据失败),信任只由真实
  失败/分歧决定;边界收窄由 boundary notes + 证据完整度表达。
- **LLM 缺失**:check 的文本类检查输出 SKIP(设计内诚实降级);用户带真实 Key 复跑
  即可见 LLM 成功路径与校验全绿。
