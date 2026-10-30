# B05 工具智能体闭环模块 — 机制说明与测试用例

实现:`b05_agent.py`(原生 LLM function calling + B02/B03/B04 四工具真实执行)。

## 闭环流程

```
用户任务 → LLM 决策(tool_calls:调哪个工具、参数是什么)
        → dispatch_tool 白名单调度 → Python 真实执行 B02/B03/B04 工具函数
        → tool_result 回传 LLM(role=tool 消息)
        → LLM 继续判断:证据充足 → 最终回答 / 不足 → 继续调用 / 失败 → 标注【证据缺失】降级
        → 循环直到 LLM 输出纯文本最终回答(或 max_steps 耗尽 / LLM 出错)
```

LLM **只负责决策**,真实查询、API 请求、知识库检索全部由 Python 代码执行;
LLM 不得编造工具数据,只能阅读工具返回结果。

## TOOL_DEFINITIONS(4 个标准工具)

| 工具 | 来源模块 | 真实函数 | 参数 | 返回证据 |
|---|---|---|---|---|
| get_current_environment | B02 | b02_memory.get_current_environment | 无 | 6 键状态 dict 或 null(空库) |
| get_environment_history | B02 | b02_memory.get_environment_history | hours(可选,默认 24,>0 ≤8760) | 升序时序 list |
| get_weather_forecast | B03 | b03_weather.get_weather_forecast | location 必填(城市或 "lat,lng");horizon_hours 可选 | weather_evidence(status success/error) |
| search_tomato_knowledge | B04 | b04_rag.search_tomato_knowledge | query 必填;k 可选 1~20 | retrieval_evidence(status ok/insufficient) |

完整 JSON Schema 见 `b05_agent.py` 的 `TOOL_DEFINITIONS` 常量或 `py b05_agent.py tools`。
调度白名单 `TOOL_DISPATCHERS` 映射到真实函数——不在白名单的调用名一律拒绝(unknown_tool)。

## dispatch_tool 错误分类

| error_type | 触发 | 例 |
|---|---|---|
| unknown_tool | 工具名不在白名单 | LLM 幻觉出的函数名 |
| bad_arguments | arguments 非 dict / TypeError | hours 传字符串 |
| value_error | 工具域校验 ValueError | hours=-5(b02 真实抛出) |
| tool_exception | 其他任何异常 | SQLite/OS 异常 |

调度层成功 = `{"status":"ok","result":<原始返回值>}`——weather/knowledge 的**嵌套 status**
(如 error/insufficient)如实穿透,LLM 读嵌套状态做分支判断;只有异常逃出工具函数才产生
调度层 error。dispatch_tool **永不抛异常**,每次调度(成功或失败)必写日志。

## 日志(强制保留,日志/b05_agent.log)

```
[工具调用] seq=%d step=%s tool=%s arguments=%s result_status=%s [nested=%s]
[工具失败] seq=%d tool=%s error_type=%s message=%s
[请求] step=%d messages=%d tools=%d   [失败] step=%d error_type=%s message=%s
[成功] step=%d final_answer_len=%d    [停止] step=%d 达到 max_steps=%d ...
[最终] status=%s steps=%d tool_calls=%d tool_failures=%d evidence=证据/b05/agent_run_*.json
```

每次调用强制记录 tool 名称、arguments、tool_result、sequence——没有真实执行、没有日志,
不算完成 Tool Agent。完整调用链(含 tool_result 全文)落盘 `证据/b05/agent_run_*.json`,
LLM 中途失败或步数耗尽时审计链同样完整保留。failtest 直调路径也自动留痕
(日志行增量可验证)。

## 分支逻辑

收到工具结果后由 LLM 判断:
- **证据充足** → 直接输出最终回答,不再调用工具;
- **证据不足** → 继续调用合适的其他工具;
- **工具失败 / error / insufficient 证据** → 最终回答中明确标注【证据缺失】降级,不编造数据。

## 测试用例

### 单/多工具闭环(`py b05_agent.py test`,需 API Key)

| id | 类型 | 任务 | 断言 |
|---|---|---|---|
| C1 | 单工具 | 查询当前大棚环境状态,并总结温度、湿度、CO₂ 与光照情况 | success ∧ 调用 ⊇{get_current_environment} ∧ 结果含 sample_time(或空库 null) |
| C2 | 单工具 | 查询北京的天气预报并总结未来天气 | success ∧ 调用 ⊇{get_weather_forecast} ∧ 结果含 provider 键 |
| C3 | 多工具 | 分析当前大棚环境状态并给出管理建议,需要时查阅番茄种植知识库 | success ∧ ⊇{get_current_environment} ∧ distinct_tools ≥2 |
| C4 | 多工具 | 结合最近 24 小时环境历史与北京天气预报,评估大棚是否需要干预,不确定时查阅知识库 | success ∧ ⊇{get_environment_history, get_weather_forecast} ∧ distinct_tools ≥2 |

共同断言:每条记录 tool/arguments/tool_result 非空、sequence 严格递增;**多工具顺序由 LLM
决定,代码零硬编码顺序**(断言只查集合成员);真实结果形状证明(current→sample_time、
history→list、weather→provider、knowledge→top_chunks)。无 Key 时每例如实记录 no_api_key。

### 执行层失败验证(`py b05_agent.py failtest`,零 LLM、零 Key)

| # | 用例 | 直调 | 断言 |
|---|---|---|---|
| ① 参数错误 | get_environment_history {"hours":-5} | 调度层 error ∧ value_error(b02 真实 ValueError 被捕获) |
| ② 空结果 | search_tomato_knowledge "量子计算是什么" | 调度层 ok ∧ 嵌套 insufficient(诚实空结果如实穿透) |
| ③ 超时 | get_weather_forecast 北京 timeout=(0.0001,0.0001) | 调度层 ok ∧ 嵌套 error/network_timeout(真实失败证据) |

附加断言:3 份 fail_*.json 落盘;日志 [工具调用] 行增量 ==3(强制日志直接证明)。

## 环境变量

| 变量 | 默认值 |
|---|---|
| `B05_LLM_BASE_URL` | `https://api.deepseek.com/chat/completions` |
| `B05_LLM_MODEL` | `deepseek-chat`(原生支持 function calling) |
| `B05_LLM_TIMEOUT` | `30`(秒) |
| `B05_MAX_STEPS` | `6`(闭环最大轮数,每轮 = 1 次 LLM 调用) |
| API Key 读取顺序 | `DEEPSEEK_API_KEY` → `ANTHROPIC_AUTH_TOKEN` |

## B07 复用说明

```python
import b05_agent
ev = b05_agent.run_agent("分析当前大棚环境状态并给出管理建议")   # 完整闭环
# ev["status"] in (success, error, stopped);ev["tool_calls"] 为完整调用链(含 tool_result)
# ev["final_answer"] 为最终回答;失败时 ev["error"]/ev["tool_failures"] 如实记录
result = b05_agent.dispatch_tool("get_environment_history", {"hours": 12})  # 单工具直调
# TOOL_DEFINITIONS / TOOL_DISPATCHERS 可直接复用
```

库级调用同样强制写日志(每工具调用一行),且自带控制台 UTF-8 兜底(GBK 教训)。

## MCP 外部工具连接(可选扩展)

本模块基础实现**不包含** MCP 连接(规格为非必需扩展)。预留扩展点:在 `TOOL_DISPATCHERS`
中注册包装函数即可接入新工具(白名单 + 统一 tool_result + 强制日志机制天然兼容);
MCP client 协议接入可在此结构上另行实现。

## 交付资产

| 资产 | 路径 |
|---|---|
| 工具定义 | b05_agent.py:`TOOL_DEFINITIONS`(4 工具 JSON Schema) |
| 调用调度代码 | b05_agent.py:`dispatch_tool` / `run_agent` |
| 调用日志记录逻辑 | 日志/b05_agent.log + 证据/b05/agent_run_*.json(强制日志) |
| 单/多工具测试用例 | 证据/b05/test_report.json(C1~C4) |
| 失败验证证据 | 证据/b05/fail_001_bad_param.json / fail_002_empty_result.json / fail_003_timeout.json + failtest_summary.json |
| 本文档 | 文档/b05_agent.md |
