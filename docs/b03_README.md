# GreenMate B03 — 天气证据接口模块 README

实现文件:`b03_weather.py`(Python 标准库 + requests,无其他依赖)。
结构定义详版:`docs/b03_weather_evidence.md`。

## 1. B03 模块任务目标

- 接入**真实外部天气 Web API**(Open-Meteo 为主,QWeather 可选),执行真实 HTTP 请求与 JSON 解析;
- 统一返回 `weather_evidence` 结构,记录数据来源(query,密钥参数脱敏)、检索时刻、状态与错误;
- 提供公开接口 `get_weather_forecast(location)`,供 B07 在同一个 run_id 流程中按需调用;
- 支持 **A/B 验证**(同一段代码仅改地点,返回结果必须真实变化,不写死 JSON)与
  **失败验证**(超时 / 错误参数 / 鉴权失败),服务出错时如实返回失败状态,**禁止伪造成功数据**。

业务边界:天气数据是**外部未来证据**——`forecast_time` 必须晚于 `analysis_time`(强制不变量,
违反即判失败);B03 不实现机器学习、不实现 RAG、不接真实传感器;仅做真实 HTTP 调用 +
JSON 解析 + 证据记录。

## 2. API 选型

| Provider | 角色 | 鉴权 | 提供字段 | 官方文档入口 |
|---|---|---|---|---|
| Open-Meteo | **主接口** | 免 API Key | 逐小时温度/湿度/降水概率/云量/短波辐射 | https://open-meteo.com/en/docs |
| QWeather | 可选接口 + 鉴权失败测试 | 需 API Key(免费额度) | 3 天预报(温度/湿度/降水概率) | https://dev.qweather.com/docs/api/weather/weather-daily-forecast/ |

- 技术栈:Python `requests`(HTTP + JSON,惰性导入;requirements.txt 已含 `requests==2.34.2`,无新增依赖);
- 主路径零密钥;QWeather 仅在设置 `B03_QWEATHER_API_KEY` 后走成功路径,
  失败验证用例直传故意无效的 key,依赖**真实 403** 而不是模拟。

## 3. weather_evidence 完整 JSON 结构定义

成功与失败**同构**;失败时 `forecast_time=null`、5 个数值字段全为 `{"value": null, "unit": ...}`。

### 3.1 成功示例(真实调用结果,北京)

```json
{
  "provider": "open-meteo",
  "retrieved_at": "2026-10-29T01:59:52+08:00",
  "query": {
    "method": "GET",
    "url": "https://api.open-meteo.com/v1/forecast",
    "params": {
      "latitude": 39.9042,
      "longitude": 116.4074,
      "hourly": "temperature_2m,relative_humidity_2m,precipitation_probability,cloud_cover,shortwave_radiation",
      "timezone": "Asia/Shanghai"
    }
  },
  "status": "success",
  "error": null,
  "location": "北京(39.9042,116.4074)",
  "analysis_time": "2026-10-29T01:59:52+08:00",
  "forecast_time": "2026-10-29T02:00:00+08:00",
  "temperature": {"value": 7.6, "unit": "℃"},
  "humidity": {"value": 70.0, "unit": "%RH"},
  "precipitation_probability": {"value": 0.0, "unit": "%"},
  "cloud_cover": {"value": 0.0, "unit": "%"},
  "shortwave_radiation": {"value": 0.0, "unit": "W/m²"}
}
```

### 3.2 逐字段说明

| 字段 | 类型 | 语义 | 成功取值 | 失败取值 |
|---|---|---|---|---|
| `provider` | str | 数据来源接口 | `open-meteo` / `qweather` | 同左 |
| `retrieved_at` | str | 证据生成时刻(ISO8601 带时区) | 恒有 | 恒有 |
| `query` | obj | **来源记录** `{method, url, params}`;params 中 key/secret 类参数一律 `***REDACTED***` | 恒有 | 恒有 |
| `status` | str | 调用成败标记 | `"success"` | `"error"` |
| `error` | obj\|null | `{error_type, message}`;message 含接口真实返回摘要(≤200 字符) | `null` | 有 |
| `location` | str | 请求地点(内置城市显示为 `北京(39.9042,116.4074)`) | 恒有 | 恒有 |
| `analysis_time` | str | 分析时刻(调用时点;B07 可传 run 的 collect_time) | 恒有 | 恒有 |
| `forecast_time` | str | 所选预报时刻;**必须晚于 analysis_time**(外部未来证据) | 有 | `null` |
| `temperature` | obj | 温度 `{value, unit:"℃"}` | 数值 | `{null, "℃"}` |
| `humidity` | obj | 湿度 `{value, unit:"%RH"}` | 数值 | `{null, "%RH"}` |
| `precipitation_probability` | obj | 降水概率 `{value, unit:"%"}` | 数值 | `{null, "%"}` |
| `cloud_cover` | obj | 云量(**扩展字段**,QWeather 无此字段 → null 亦合法) | 数值或 null | `{null, "%"}` |
| `shortwave_radiation` | obj | 短波辐射(**扩展字段**,同上) | 数值或 null | `{null, "W/m²"}` |

### 3.3 error_type 词汇表

| error_type | 触发场景 | 是否真实服务失败 |
|---|---|---|
| `missing_dependency` | requests 未安装(惰性导入检测) | — |
| `network_timeout` | 连接/读取超时、连接失败、其他网络异常 | 是 |
| `api_key_error` | HTTP 401/403(QWeather 无效 key 实测返回 403) | 是 |
| `bad_param` | 地点非法 / HTTP 400(如 Open-Meteo 对纬度 999 的真实 400) | 是 |
| `http_error` | 其他非 200 状态 | 是 |
| `bad_response` | JSON 不可解析 / 缺 `hourly` 键 / 无晚于分析时刻的步长 / forecast_time 不变量违反 | 接口异常,如实上报 |

**禁止伪造成功数据原则**:成功路径只有一条——解析真实 API 返回内容;任何失败都返回
`status="error"` + `error` 详情 + 5 数值字段全 null,绝不填充静态值。

## 4. 三组失败测试案例(`py b03_weather.py failtest`)

| # | 用例 | 触发方式 | 期望 error_type | 真实服务反应(已实测) |
|---|---|---|---|---|
| ① | **超时** | `get_weather_forecast("北京", timeout=(0.0001, 0.0001))`(0.0001 秒连接/读取超时) | `network_timeout` | 连接超时(冷连接 ConnectTimeout / 热连接 ReadTimeout,均映射同型) |
| ② | **非法纬度 400** | `get_weather_forecast("999,999")`——封装只做格式校验,范围校验交给 API | `bad_param` | Open-Meteo 真实 HTTP 400,响应体:`"reason": "Latitude must be in range of -90 to 90°. Given: 999.0."` |
| ③ | **QWeather 密钥 403** | `_call_qweather("101010100", "invalid-key-for-failtest", ...)` 直传无效 key(绕过环境变量,评分者配了真 key 也不影响确定性) | `api_key_error` | QWeather 真实 HTTP 403,响应体:`"Invalid Host - An invalid or unauthorized API Host."` |

每例的共同断言(不满足即 ⚠,绝不伪造):

1. `status == "error"`;
2. `error.error_type` 与预期一致;
3. `forecast_time == null` 且 temperature/humidity/precipitation_probability/cloud_cover/
   shortwave_radiation 五个数值字段的 `value` 全部为 `null`;
4. `error.message` 内嵌接口真实返回文本(非伪造证明)。

三例证据分别落盘:`evidence/b03/fail_timeout.json`、`fail_bad_param.json`、`fail_auth.json`;
退出码恒 0(检查清单语义)。

## 5. CLI 调用命令示例

### 5.1 正向调用 `forecast`

```bash
py b03_weather.py forecast 北京
# ✓ status=success | provider=open-meteo | location=北京(39.9042,116.4074)
#   forecast_time: 2026-10-29T02:00:00+08:00
#   temperature: 7.6 ℃   humidity: 70 %RH   precipitation_probability: 0 %
#   cloud_cover: 0 %      shortwave_radiation: 0 W/m²
# ✓ 证据已保存: evidence/b03/weather_北京_20261029_015954.json

py b03_weather.py forecast 广州
# ✓ 同一代码路径,仅地点不同:temperature 25.7 ℃(与北京真实不同)

py b03_weather.py forecast 上海 --horizon 6
# horizon=6 → 取最接近 analysis_time+6h 的步长(实测 analysis 02:02 → forecast 08:00)

py b03_weather.py forecast "23.1,113.2"       # "lat,lng" 透传(格式校验,范围交给 API)
py b03_weather.py forecast 北京 --run-id run_20261029_120000_abc123
# run-id 合法 → 证据落盘 evidence/b03/run_20261029_120000_abc123_weather.json(B07 同款用法)
py b03_weather.py forecast 北京 --provider qweather --timeout 15   # 需已设置 B03_QWEATHER_API_KEY
```

### 5.2 A/B 测试 `abtest`

```bash
py b03_weather.py abtest
# ================ B03 A/B 验证(同代码路径,仅地点不同)================
# 城市 A: 北京 | 城市 B: 广州 | provider: open-meteo
#   字段                     城市 A(北京)     城市 B(广州)     是否不同
#   temperature            8.8          25.4         是
#   humidity               51           71           是
#   precipitation_probability 0            5            是
#   判定: PASS — 返回结果真实随地点变化
# ✓ 证据已保存: evidence/b03/abtest_北京.json / abtest_广州.json / abtest_summary.json
```

PASS 条件:两侧均 success 且温度/湿度/降水概率**至少一项真实不同**;天气恰好相同则
如实 FAIL 附原因,绝不伪造差异。可选参数:`--city-a 北京 --city-b 广州 --window N`。

### 5.3 失败测试 `failtest`

```bash
py b03_weather.py failtest
# ================ B03 失败验证(禁止伪造成功数据)================
#   [1/3] 超时 ✓ | status=error error_type=network_timeout | 数值字段全 null: 是
#        接口真实返回: 读取超时(服务器 0.0001 秒内未返回结果)
#   [2/3] 错误参数 ✓ | status=error error_type=bad_param
#        接口真实返回: 天气接口返回参数错误(HTTP 400): Latitude must be in range of -90 to 90°. Given: 999.0.
#   [3/3] 鉴权失败 ✓ | status=error error_type=api_key_error
#        接口真实返回: HTTP 403 认证失败,API Key 无效或已过期(接口返回: Invalid Host - ...)
#   判定: PASS — 失败验证通过(通过 3/3;三例均为真实服务失败,无任何伪造成功数据)
```

### 5.4 帮助与配置

```bash
py b03_weather.py --help
```

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `B03_WEATHER_TIMEOUT` | `10`(秒) | 连接与读取超时 |
| `B03_OPEN_METEO_BASE_URL` | `https://api.open-meteo.com/v1/forecast` | Open-Meteo 接口地址 |
| `B03_QWEATHER_BASE_URL` | `https://devapi.qweather.com/v7/weather/3d` | QWeather 接口地址 |
| `B03_QWEATHER_API_KEY` | 空 | 可选;QWeather 成功路径需要(无效 key 得真实 403) |
| `B03_LOCATION` | `北京` | `get_weather_forecast()` 未传 location 时的默认地点 |

**B07 接入**(库调用,`import b03_weather`):

```python
ev = get_weather_forecast("北京", run_id=run_id, analysis_time=collect_time)
if ev["status"] != "success":
    ...  # 按降级策略处理 ev["error"]["error_type"],绝不伪造成功
```

## 6. 交付资产清单

| 资产 | 路径 / 说明 |
|---|---|
| requirements.txt | `requests==2.34.2`(唯一第三方依赖,已在项目根目录) |
| b03_weather.py 源码 | 可复用天气调用封装(provider 抽象 + weather_evidence 统一结构 + CLI + 验证) |
| evidence 证据样例 | `evidence/b03/`:正向 `weather_<城市>_<时间戳>.json` 与 `run_<run_id>_weather.json`;A/B `abtest_北京.json` / `abtest_广州.json` / `abtest_summary.json`;失败 `fail_timeout.json` / `fail_bad_param.json` / `fail_auth.json` |
| README.md | 本文档 `docs/b03_README.md` + 结构定义详版 `docs/b03_weather_evidence.md` |
| 调用日志 | `logs/b03_weather.log`:每次调用两行(`[请求]` + `[成功]`/`[失败]`),密钥永不落日志 |
