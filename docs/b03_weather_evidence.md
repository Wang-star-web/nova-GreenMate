# B03 天气证据接口 — weather_evidence 结构定义与 API 文档入口

实现:`b03_weather.py`。本文档定义统一返回结构 `weather_evidence` 的逐字段语义,
并记录所接入真实天气 API 的官方文档入口。

## API 文档入口

| Provider | 用途 | 官方文档 |
|---|---|---|
| Open-Meteo | 主接口(免 API Key):逐小时温度/湿度/降水概率/云量/短波辐射 | https://open-meteo.com/en/docs |
| QWeather | 可选接口(需 API Key)与鉴权失败测试:3 天预报 | https://dev.qweather.com/docs/api/weather/weather-daily-forecast/ |

## weather_evidence 统一结构

成功与失败**同构**——失败时 `forecast_time=null`、5 个数值字段全为 `{"value": null, "unit": ...}`,
绝不伪造任何数值;成功路径只有一条:解析真实 API 返回内容。

```json
{
  "provider": "open-meteo",
  "retrieved_at": "2026-10-29T01:59:52+08:00",
  "query": {
    "method": "GET",
    "url": "https://api.open-meteo.com/v1/forecast",
    "params": {"latitude": 39.9042, "longitude": 116.4074,
               "hourly": "temperature_2m,relative_humidity_2m,precipitation_probability,cloud_cover,shortwave_radiation",
               "timezone": "Asia/Shanghai"}
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

## 字段语义

| 字段 | 类型 | 语义 | 成功 | 失败 |
|---|---|---|---|---|
| `provider` | str | 数据来源接口 | `open-meteo` / `qweather` | 同左 |
| `retrieved_at` | str | 证据生成时刻(ISO8601 带时区) | 恒有 | 恒有 |
| `query` | obj | **来源记录**:`{method, url, params}`;params 中 key/secret 类参数一律 `***REDACTED***` | 恒有 | 恒有 |
| `status` | str | 调用成败 | `"success"` | `"error"` |
| `error` | obj\|null | `{error_type, message}`;message 含接口真实返回摘要(≤200 字符) | `null` | 有 |
| `location` | str | 请求地点(内置城市显示为 `北京(39.9042,116.4074)`) | 恒有 | 恒有 |
| `analysis_time` | str | 分析时刻(调用时点;B07 可传 run 的 collect_time) | 恒有 | 恒有 |
| `forecast_time` | str | 所选预报时刻;**必须晚于 analysis_time**(外部未来证据,强制不变量) | 有 | `null` |
| `temperature` | obj | 温度 `{value, unit:"℃"}` | 数值 | `{null,"℃"}` |
| `humidity` | obj | 湿度 `{value, unit:"%RH"}` | 数值 | `{null,"%RH"}` |
| `precipitation_probability` | obj | 降水概率 `{value, unit:"%"}` | 数值 | `{null,"%"}` |
| `cloud_cover` | obj | 云量(扩展字段) | 数值(QWeather 无此字段 → null 亦合法) | `{null,"%"}` |
| `shortwave_radiation` | obj | 短波辐射(扩展字段) | 数值(同上) | `{null,"W/m²"}` |

## error_type 词汇表(与 B01 ask_llm 一致)

| error_type | 触发场景 | 是否真实服务失败 |
|---|---|---|
| `missing_dependency` | requests 未安装(惰性导入) | — |
| `network_timeout` | 连接/读取超时、连接失败、其他网络异常 | 是 |
| `api_key_error` | HTTP 401/403(QWeather 无效 key 实测返回 403) | 是 |
| `bad_param` | 地点非法 / HTTP 400(如 Open-Meteo 对纬度 999 的真实 400) | 是(400 为接口真实返回) |
| `http_error` | 其他非 200 状态 | 是 |
| `bad_response` | JSON 不可解析 / 缺 `hourly` 键 / 无晚于分析时刻的步长 / forecast_time 不变量违反 | 接口异常,如实上报 |

## 关键语义

- **forecast_time 时区**:Open-Meteo 的 `hourly.time` 为整点 naive 本地字符串
  (`YYYY-MM-DDTHH:MM`,timezone=Asia/Shanghai,+08:00 无夏令时),直接按 naive 字符串比较
  选出首个严格晚于 `analysis_time` 的步长,输出时附本地时区。QWeather 取 `fxDate` 的
  当日 12:00 本地时间,同规则选日。候选耗尽 → `bad_response`(诚实失败)。
- **强制不变量**:`get_weather_forecast` 在成功路径上校验 `forecast_time > analysis_time`,
  违反即降级为 `bad_response` 且数值清空——天气数据是外部**未来**证据,拒绝伪造。
- **脱敏**:`query.params` 中 key/secret/token 类参数一律 `***REDACTED***`;错误 message
  只用基础 URL 拼接(requests 异常串可能内嵌完整 URL/参数,禁止直接使用)。
  验证:`grep -rn "invalid-key-for-failtest" evidence/b03/ logs/b03_weather.log` 无匹配。
- **日志**:每次调用写两行到 `logs/b03_weather.log`(`[请求]` + `[成功]`/`[失败]`),
  控制台镜像;密钥永不落日志。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `B03_WEATHER_TIMEOUT` | `10`(秒) | 连接与读取超时 |
| `B03_OPEN_METEO_BASE_URL` | `https://api.open-meteo.com/v1/forecast` | Open-Meteo 接口地址 |
| `B03_QWEATHER_BASE_URL` | `https://devapi.qweather.com/v7/weather/3d` | QWeather 接口地址 |
| `B03_QWEATHER_API_KEY` | 空 | 可选;设置了 QWeather 成功路径才可用(无效 key 得真实 403) |
| `B03_LOCATION` | `北京` | `get_weather_forecast()` 未传 location 时的默认地点 |

## 公开接口(B07 接入说明)

```python
from b03_weather import get_weather_forecast
# B07 在 run_id 流程中按需调用:analysis_time 传该 run 的 collect_time,证据落盘 evidence/b03/{run_id}_weather.json
ev = get_weather_forecast("北京", run_id=run_id, analysis_time=collect_time)
if ev["status"] != "success":
    # 按降级策略处理:ev["error"]["error_type"] + message,绝不伪造成功数据
    ...
```

- `get_weather_forecast(location=None, provider=None, run_id=None, analysis_time=None, horizon_hours=None, timeout=None) -> dict`
- `horizon_hours=N` 选择最接近 `analysis_time+N` 的预报步长(仍须严格晚于分析时刻)。
- `run_id` 合法(`run_\d{8}_\d{6}_[0-9a-f]{6}`)时自动落盘;库调用不落盘、不打印证据摘要(仅日志)。
- 库级路径已自带控制台 UTF-8 兜底(GBK 管道打印 `℃`/`W/m²` 会崩溃,已修复)。

## A/B 与失败验证(验证规则)

- **A/B**(`py b03_weather.py abtest`):同一段代码、同一 provider/horizon/timeout,仅 location
  不同(默认 北京 vs 广州)。PASS 条件:两侧均 success 且温度/湿度/降水概率至少一项**真实不同**
  (实测 8.8 vs 25.4℃)。若天气恰好相同 → 如实 FAIL 附原因,绝不伪造差异。
- **失败验证**(`py b03_weather.py failtest`):超时(`timeout=(0.0001,0.0001)`)/
  错误参数(`lat=999` 触发 Open-Meteo 真实 400)/ 鉴权失败(QWeather 无效 key 触发真实 403)。
  三例均断言 `status=error`、预期 `error_type`、5 数值字段全 null、`forecast_time=null`、
  证据内嵌接口真实错误文本——服务出错时系统明确返回失败,禁止伪造成功数据。
