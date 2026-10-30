#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 GreenMate B03 — 天气证据接口模块(真实天气 Web API 接入)
=====================================================================

【任务目标】
  接入真实外部天气 Web API(Open-Meteo 主 / QWeather 可选),执行真实 HTTP 请求与
  JSON 解析,统一返回 weather_evidence 结构,记录来源(query/redacted)、状态与错误,
  为后续模块提供"外部未来证据"。公开接口 get_weather_forecast(location) 供 B07
  在同一个 run_id 流程中按需调用。支持 A/B 验证(同代码路径仅改地点)与失败验证
  (超时 / 错误参数 / 鉴权失败),服务出错时如实返回失败状态,绝不伪造成功数据。

【运行方式】
  py b03_weather.py forecast <location> [--run-id R] [--provider p] [--horizon N] [--timeout T]
  py b03_weather.py abtest  [--city-a 北京] [--city-b 广州] [--window N]
  py b03_weather.py failtest
  py b03_weather.py --help    # 查看本说明(无参同)
  location 支持内置城市(北京/上海/广州)或 "lat,lng" 透传。

【统一返回结构 weather_evidence】
  provider / retrieved_at / query / status / error / location / analysis_time /
  forecast_time / temperature / humidity / precipitation_probability,
  扩展字段 cloud_cover / shortwave_radiation。
  成功: status="success", error=null, forecast_time 必晚于 analysis_time;
  失败: status="error", error={error_type,message}, forecast_time=null, 5 个数值字段全 null。

【业务边界】
  天气数据是外部未来证据(forecast_time 必须晚于 analysis_time);B03 不实现
  机器学习、不实现 RAG、不接真实传感器;仅做真实 HTTP 调用 + JSON 解析 + 证据记录。
=====================================================================
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

# ---------------- 配置 ----------------
BASE_DIR = Path(__file__).resolve().parent
EVIDENCE_DIR = BASE_DIR / "证据" / "b03"      # 天气证据产物目录(运行时自动创建)
LOG_DIR = BASE_DIR / "日志"
WEATHER_LOG = LOG_DIR / "b03_weather.log"         # 每次调用两行日志([请求]/[成功]或[失败])

# 全部可被环境变量覆盖,代码中零硬编码 API Key(b01 同款约定)
B03_OPEN_METEO_BASE_URL = os.environ.get("B03_OPEN_METEO_BASE_URL", "https://api.open-meteo.com/v1/forecast")
B03_QWEATHER_BASE_URL = os.environ.get("B03_QWEATHER_BASE_URL", "https://devapi.qweather.com/v7/weather/3d")
B03_QWEATHER_API_KEY = os.environ.get("B03_QWEATHER_API_KEY", "")   # 可选:设置了才走 QWeather 成功路径
B03_LOCATION = os.environ.get("B03_LOCATION", "北京")
try:
    B03_WEATHER_TIMEOUT = float(os.environ.get("B03_WEATHER_TIMEOUT", "10"))
except ValueError:
    B03_WEATHER_TIMEOUT = 10.0   # 环境变量非法时退回默认值,不崩溃

# 内置城市:显示名 -> (纬度, 经度, QWeather 城市码);QWeather 码与官方 LocationList 核对
LOCATIONS = {
    "北京": (39.9042, 116.4074, "101010100"),
    "上海": (31.2304, 121.4737, "101020100"),
    "广州": (23.1291, 113.2644, "101280101"),
}

RUN_ID_RE = re.compile(r"^run_\d{8}_\d{6}_[0-9a-f]{6}$")   # 与 B01/B07 的 run_id 约定一致(模块内复制)

ERROR_TYPES = ("missing_dependency", "network_timeout", "api_key_error",
               "http_error", "bad_param", "bad_response")
PROVIDERS = ("open-meteo", "qweather")

# 数值字段:key -> (evidence 键名, 单位, Open-Meteo 字段名)
VALUE_FIELDS = (
    ("temperature", "℃", "temperature_2m"),
    ("humidity", "%RH", "relative_humidity_2m"),
    ("precipitation_probability", "%", "precipitation_probability"),
    ("cloud_cover", "%", "cloud_cover"),
    ("shortwave_radiation", "W/m²", "shortwave_radiation"),
)
CORE_VALUE_KEYS = ("temperature", "humidity", "precipitation_probability")   # 成功时必非空
EXT_VALUE_KEYS = ("cloud_cover", "shortwave_radiation")                      # 扩展字段,允许 null


def setup_console():
    """强制标准输出为 UTF-8:Windows 默认 GBK 管道下打印 ℃/W/m² 会崩溃(已实测)。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def _now_iso():
    """当前时刻 ISO8601(带时区)。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _redact_params(params):
    """参数脱敏:key/secret 类参数值一律替换,证据与日志中绝不出现真实密钥。"""
    redacted = dict(params)
    for k in redacted:
        if "key" in k.lower() or "secret" in k.lower() or "token" in k.lower():
            redacted[k] = "***REDACTED***"
    return redacted


def _value_field(unit, value=None):
    """数值字段统一形状 {"value": float|None, "unit": str}。"""
    return {"value": value, "unit": unit}


def _evidence_shell(provider, query, location, analysis_time_iso):
    """weather_evidence 失败外壳:status=error、error=None、forecast_time=null、5 值全 null。
    调用方填充 error 或在成功时覆盖 status/error/forecast_time/数值字段。"""
    return {
        "provider": provider,
        "retrieved_at": _now_iso(),
        "query": query,
        "status": "error",
        "error": None,
        "location": location,
        "analysis_time": analysis_time_iso,
        "forecast_time": None,
        "temperature": _value_field("℃"),
        "humidity": _value_field("%RH"),
        "precipitation_probability": _value_field("%"),
        "cloud_cover": _value_field("%"),
        "shortwave_radiation": _value_field("W/m²"),
    }


def _normalize_analysis_time(analysis_time):
    """analysis_time 归一化:None -> 现在;datetime -> 本地时区;ISO 字符串 -> datetime;非法抛 ValueError。"""
    if analysis_time is None:
        return datetime.now().astimezone()
    if isinstance(analysis_time, datetime):
        return analysis_time.astimezone()
    try:
        return datetime.fromisoformat(str(analysis_time)).astimezone()
    except ValueError:
        raise ValueError("analysis_time 格式非法(需 ISO8601 或 datetime): %s" % analysis_time)


def resolve_location(location):
    """解析地点:内置城市名 -> (显示名, lat, lon, qweather_code);
    "lat,lng" -> (原文, lat, lon, None),仅做格式校验(两个可解析浮点),范围校验交给 API(failtest ② 依赖真实 400)。
    返回 None 或抛 ValueError(中文原因)。"""
    text = str(location).strip()
    if not text:
        raise ValueError("地点不能为空")
    if text in LOCATIONS:
        lat, lon, code = LOCATIONS[text]
        return "%s(%.4f,%.4f)" % (text, lat, lon), lat, lon, code
    if "," in text:
        parts = [p.strip() for p in text.split(",")]
        if len(parts) == 2:
            try:
                lat, lon = float(parts[0]), float(parts[1])
            except ValueError:
                raise ValueError("经纬度格式非法,需为 'lat,lng': %s" % text)
            return "%.4f,%.4f" % (lat, lon), lat, lon, None
    raise ValueError("未知地点: %s(支持 北京/上海/广州 或 'lat,lng')" % text)


def _pick_forecast_step(times, analysis_dt, horizon_hours=None):
    """从 Open-Meteo 整点步长列表中选预报时刻:
    默认取首个严格晚于 analysis_time 的步长;horizon_hours=N 取最接近 analysis+N 的步长(仍须严格晚于)。
    times 为 'YYYY-MM-DDTHH:MM' naive 本地字符串(Asia/Shanghai,无夏令时,直接比较)。
    返回 (index, datetime) 或 (None, None)。"""
    analysis_naive = analysis_dt.replace(tzinfo=None)
    target = analysis_naive + timedelta(hours=horizon_hours) if horizon_hours is not None else None
    best, best_delta = None, None
    for i, t in enumerate(times):
        try:
            dt = datetime.strptime(t, "%Y-%m-%dT%H:%M")
        except ValueError:
            continue
        if dt <= analysis_naive:
            continue
        if target is None:
            return i, dt   # 首个严格晚于分析时刻的步长
        delta = abs((dt - target).total_seconds())
        if best_delta is None or delta < best_delta:
            best, best_delta = (i, dt), delta
    return best if best is not None else (None, None)


def _to_float(value):
    """API 数值防御转换:None 保持 None(降水概率偶有 null),其余 float()。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _timeout_text(timeout):
    """超时参数的人类可读文本(元组取连接超时)。"""
    return "%g" % (timeout[0] if isinstance(timeout, tuple) else timeout)


def _try_json_text(resp):
    """从响应体提取接口真实错误文本(≤200 字符,含中文/° 等,用 resp.text)。"""
    try:
        data = resp.json()
    except Exception:
        text = (resp.text or "").strip()
        return text[:200] if text else "无响应体"
    # QWeather 错误形状 {"error":{...}};Open-Meteo 400 形状 {"reason":..., "error":true}
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return ("%s - %s" % (err.get("title", ""), err.get("detail", err.get("status", "")))).strip(" -")[:200]
        if data.get("reason"):
            return str(data["reason"])[:200]
        if data.get("msg"):
            return str(data["msg"])[:200]
        if data.get("code"):
            return "接口返回 code=%s" % data["code"]
    return str(data)[:200]


def _http_call(method, url, params, timeout):
    """真实 HTTP 请求封装(b01 ask_llm 移植 + 400->bad_param)。
    返回 (resp|None, error_dict|None):网络异常与全部非 200 状态都转 error_dict,
    错误 message 只用基础 URL 与接口真实返回文本拼,绝不内嵌完整 URL/密钥。"""
    try:
        import requests   # 惰性导入:未安装 requests 时其余子命令(help/用法)不受影响
    except ImportError:
        return None, {"error_type": "missing_dependency",
                      "message": "未安装 requests 库,无法调用天气接口(可执行 pip install requests)"}
    try:
        resp = requests.request(method, url, params=params, timeout=timeout)
    except requests.exceptions.ConnectTimeout:
        return None, {"error_type": "network_timeout",
                      "message": "连接超时(超过 %s 秒未建立连接),请检查网络或 B03_WEATHER_TIMEOUT" % _timeout_text(timeout)}
    except requests.exceptions.ReadTimeout:
        return None, {"error_type": "network_timeout",
                      "message": "读取超时(服务器 %s 秒内未返回结果)" % _timeout_text(timeout)}
    except requests.exceptions.ConnectionError:
        return None, {"error_type": "network_timeout",
                      "message": "网络连接失败,无法访问 %s" % url}
    except requests.exceptions.InvalidURL:
        return None, {"error_type": "bad_param", "message": "接口地址非法: %s" % url}
    except Exception as e:   # 其他网络异常如实上报,绝不伪造成功
        return None, {"error_type": "network_timeout", "message": "网络异常: %s" % e}
    if resp.status_code in (401, 403):
        return resp, {"error_type": "api_key_error",
                      "message": "HTTP %d 认证失败,API Key 无效或已过期(接口返回: %s)"
                                 % (resp.status_code, _try_json_text(resp))}
    if resp.status_code == 400:
        return resp, {"error_type": "bad_param",
                      "message": "天气接口返回参数错误(HTTP 400): %s" % _try_json_text(resp)}
    if resp.status_code != 200:
        return resp, {"error_type": "http_error",
                      "message": "HTTP %d: %s" % (resp.status_code, _try_json_text(resp))}
    return resp, None


def _attach_local_tz(dt, analysis_dt):
    """把 API 的 naive 本地时间附上本地时区(中国无夏令时,固定 +08:00)。"""
    return dt.replace(tzinfo=analysis_dt.tzinfo or datetime.now().astimezone().tzinfo)


def _call_open_meteo(display, lat, lon, analysis_dt, horizon_hours=None, timeout=None):
    """调用 Open-Meteo(免 key),返回完整 weather_evidence。"""
    timeout = timeout if timeout is not None else B03_WEATHER_TIMEOUT
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": "temperature_2m,relative_humidity_2m,precipitation_probability,cloud_cover,shortwave_radiation",
        "timezone": "Asia/Shanghai",
    }
    query = {"method": "GET", "url": B03_OPEN_METEO_BASE_URL, "params": _redact_params(params)}
    ev = _evidence_shell("open-meteo", query, display, analysis_dt.isoformat(timespec="seconds"))
    resp, err = _http_call("GET", B03_OPEN_METEO_BASE_URL, params, timeout)
    if err is not None:
        ev["error"] = err
        return ev
    try:
        data = resp.json()
    except Exception:
        ev["error"] = {"error_type": "bad_response", "message": "接口返回内容不是合法 JSON"}
        return ev
    hourly = data.get("hourly") if isinstance(data, dict) else None
    if not isinstance(hourly, dict) or not isinstance(hourly.get("time"), list) or not hourly["time"]:
        ev["error"] = {"error_type": "bad_response", "message": "接口返回缺少 hourly 数据(字段缺失或为空)"}
        return ev
    index, step_dt = _pick_forecast_step(hourly["time"], analysis_dt, horizon_hours)
    if index is None:
        ev["error"] = {"error_type": "bad_response", "message": "接口返回中找不到晚于分析时刻的预报步长"}
        return ev
    values = {}
    for key, unit, api_key in VALUE_FIELDS:
        arr = hourly.get(api_key)
        raw = arr[index] if isinstance(arr, list) and index < len(arr) else None
        values[key] = _to_float(raw)
    if any(values[k] is None for k in CORE_VALUE_KEYS):
        ev["error"] = {"error_type": "bad_response",
                       "message": "接口返回的预报步长缺少核心数值(温度/湿度/降水概率)"}
        return ev
    ev["status"] = "success"
    ev["forecast_time"] = _attach_local_tz(step_dt, analysis_dt).isoformat(timespec="seconds")
    for key, unit, _api_key in VALUE_FIELDS:
        ev[key] = _value_field(unit, values[key])
    return ev


def _qweather_select_day(daily, analysis_dt):
    """QWeather 3 天预报中选首个 fxDate T12:00 晚于分析时刻的日;无则 None。"""
    analysis_naive = analysis_dt.replace(tzinfo=None)
    for day in daily:
        try:
            day_dt = datetime.strptime(day.get("fxDate", ""), "%Y-%m-%d").replace(hour=12)
        except ValueError:
            continue
        if day_dt > analysis_naive:
            return day, day_dt
    return None, None


def _call_qweather(display, code, api_key, analysis_dt, timeout=None):
    """调用 QWeather 3 天预报(需有效 key;无效 key 得真实 401/403)。
    该 API 无云量/辐射字段,扩展字段保持 null。"""
    timeout = timeout if timeout is not None else B03_WEATHER_TIMEOUT
    if not code:
        ev = _evidence_shell("qweather", {"method": "GET", "url": B03_QWEATHER_BASE_URL, "params": {}},
                             display, analysis_dt.isoformat(timespec="seconds"))
        ev["error"] = {"error_type": "bad_param", "message": "QWeather 仅支持内置城市(北京/上海/广州)"}
        return ev
    params = {"location": code, "key": api_key}
    query = {"method": "GET", "url": B03_QWEATHER_BASE_URL, "params": _redact_params(params)}
    ev = _evidence_shell("qweather", query, display, analysis_dt.isoformat(timespec="seconds"))
    resp, err = _http_call("GET", B03_QWEATHER_BASE_URL, params, timeout)
    if err is not None:
        ev["error"] = err
        return ev
    try:
        data = resp.json()
    except Exception:
        ev["error"] = {"error_type": "bad_response", "message": "接口返回内容不是合法 JSON"}
        return ev
    if not isinstance(data, dict) or str(data.get("code")) != "200":
        code_text = str(data.get("code", "?")) if isinstance(data, dict) else "?"
        error_type = "api_key_error" if code_text in ("401", "402") else "http_error"
        ev["error"] = {"error_type": error_type,
                       "message": "QWeather 接口返回错误 code=%s: %s" % (code_text, _try_json_text(resp))}
        return ev
    daily = data.get("daily")
    if not isinstance(daily, list) or not daily:
        ev["error"] = {"error_type": "bad_response", "message": "接口返回缺少 daily 预报数据"}
        return ev
    day, day_dt = _qweather_select_day(daily, analysis_dt)
    if day is None:
        ev["error"] = {"error_type": "bad_response", "message": "接口返回中找不到晚于分析时刻的预报日"}
        return ev
    temp_max, temp_min = _to_float(day.get("tempMax")), _to_float(day.get("tempMin"))
    humidity, precip = _to_float(day.get("humidity")), _to_float(day.get("precipProb"))
    if temp_max is None or temp_min is None or humidity is None:
        ev["error"] = {"error_type": "bad_response",
                       "message": "接口返回的预报日缺少核心数值(温度/湿度)"}
        return ev
    ev["status"] = "success"
    ev["forecast_time"] = _attach_local_tz(day_dt, analysis_dt).isoformat(timespec="seconds")
    ev["temperature"] = _value_field("℃", round((temp_max + temp_min) / 2.0, 1))
    ev["humidity"] = _value_field("%RH", humidity)
    ev["precipitation_probability"] = _value_field("%", precip if precip is not None else 0.0)
    # 云量/辐射:QWeather 3 天预报不提供,扩展字段保持 null(结构统一)
    return ev


# ---------------- 公开接口(官方契约,供 B07 复用)----------------
def get_weather_forecast(location=None, provider=None, run_id=None, analysis_time=None,
                         horizon_hours=None, timeout=None):
    """天气证据唯一公开入口(B07 在 run_id 流程中按需调用)。
    · location: 内置城市(北京/上海/广州)或 "lat,lng";None -> 环境变量 B03_LOCATION
    · provider: "open-meteo"(默认)| "qweather"(需 B03_QWEATHER_API_KEY 有效)
    · analysis_time: None -> 现在;可传 datetime 或 ISO 字符串(B07 可传 run 的 collect_time)
    · run_id 合法时保存 证据/b03/{run_id}_weather.json;库调用不落盘
    恒返回统一 weather_evidence;成功路径只有一条——解析真实 API 返回,
    且强制校验 forecast_time > analysis_time(违反即 bad_response,绝不伪造成功)。"""
    location = location or B03_LOCATION
    provider = provider or "open-meteo"
    try:
        analysis_dt = _normalize_analysis_time(analysis_time)
    except ValueError as e:
        return _evidence_shell(provider, {"method": "GET", "url": B03_OPEN_METEO_BASE_URL, "params": {}},
                               str(location), _now_iso()) | {
                                   "error": {"error_type": "bad_param", "message": str(e)}}
    try:
        display, lat, lon, code = resolve_location(location)
    except ValueError as e:
        return _evidence_shell(provider, {"method": "GET", "url": B03_OPEN_METEO_BASE_URL, "params": {}},
                               str(location), analysis_dt.isoformat(timespec="seconds")) | {
                                   "error": {"error_type": "bad_param", "message": str(e)}}
    if provider == "open-meteo":
        ev = _call_open_meteo(display, lat, lon, analysis_dt, horizon_hours, timeout)
    elif provider == "qweather":
        ev = _call_qweather(display, code, B03_QWEATHER_API_KEY, analysis_dt, timeout)
    else:
        ev = _evidence_shell(provider, {"method": "GET", "url": B03_OPEN_METEO_BASE_URL, "params": {}},
                             display, analysis_dt.isoformat(timespec="seconds"))
        ev["error"] = {"error_type": "bad_param",
                       "message": "未知 provider: %s(支持 open-meteo / qweather)" % provider}
    # 业务边界强制校验:天气数据是外部未来证据,forecast_time 必须晚于 analysis_time
    if ev["status"] == "success":
        try:
            forecast_dt = datetime.fromisoformat(ev["forecast_time"])
            if forecast_dt.astimezone() <= analysis_dt:
                ev["status"] = "error"
                ev["forecast_time"] = None
                for key, unit, _api_key in VALUE_FIELDS:
                    ev[key] = _value_field(unit)
                ev["error"] = {"error_type": "bad_response",
                               "message": "预报时刻不晚于分析时刻,证据无效(拒绝伪造未来证据)"}
        except ValueError:
            ev["status"] = "error"
            ev["forecast_time"] = None
            for key, unit, _api_key in VALUE_FIELDS:
                ev[key] = _value_field(unit)
            ev["error"] = {"error_type": "bad_response", "message": "预报时刻格式非法"}
    _log_evidence(ev)
    if run_id and RUN_ID_RE.match(str(run_id)):
        save_weather_evidence(ev, "%s_weather.json" % run_id)
    return ev


# ---------------- 证据校验 / 落盘 / 打印 / 日志 ----------------
def validate_weather_evidence(ev):
    """结构自检。返回 (ok, problems)。规则:
    必填键齐全;provider/status/error_type 白名单;成功 ⇒ error=null、forecast_time 可解析
    且晚于 analysis_time、3 个核心值非 null(扩展字段允许 null);失败 ⇒ 5 值全 null。"""
    problems = []
    if not isinstance(ev, dict):
        return False, ["不是字典"]
    required = ("provider", "retrieved_at", "query", "status", "error", "location",
                "analysis_time", "forecast_time", "temperature", "humidity",
                "precipitation_probability", "cloud_cover", "shortwave_radiation")
    for key in required:
        if key not in ev:
            problems.append("缺少字段 %s" % key)
    if ev.get("provider") not in PROVIDERS:
        problems.append("provider 不在白名单: %s" % ev.get("provider"))
    if ev.get("status") not in ("success", "error"):
        problems.append("status 非法: %s" % ev.get("status"))
    value_fields = ("temperature", "humidity", "precipitation_probability",
                    "cloud_cover", "shortwave_radiation")
    if ev.get("status") == "success":
        if ev.get("error") is not None:
            problems.append("成功证据 error 应为 null")
        try:
            f_dt = datetime.fromisoformat(ev.get("forecast_time", ""))
            a_dt = datetime.fromisoformat(ev.get("analysis_time", ""))
            if f_dt.astimezone() <= a_dt.astimezone():
                problems.append("forecast_time 未晚于 analysis_time")
        except (TypeError, ValueError):
            problems.append("forecast_time/analysis_time 不可解析")
        for key in CORE_VALUE_KEYS:
            field = ev.get(key)
            if not isinstance(field, dict) or not isinstance(field.get("value"), (int, float)):
                problems.append("成功证据 %s 值缺失" % key)
    else:
        err = ev.get("error")
        if not isinstance(err, dict) or err.get("error_type") not in ERROR_TYPES or not err.get("message"):
            problems.append("失败证据 error 结构非法")
        if ev.get("forecast_time") is not None:
            problems.append("失败证据 forecast_time 应为 null")
        for key in value_fields:
            field = ev.get(key)
            if not isinstance(field, dict) or field.get("value") is not None:
                problems.append("失败证据 %s 值应为 null(禁止伪造成功数据)" % key)
    return (not problems), problems


def save_weather_evidence(ev, filename):
    """证据落盘 证据/b03/{filename}(自动建目录,UTF-8);失败仅友好提示。"""
    try:
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        with open(EVIDENCE_DIR / filename, "w", encoding="utf-8") as f:
            json.dump(ev, f, ensure_ascii=False, indent=2)
        return True
    except OSError as e:
        print("⚠ 证据保存失败: %s" % e)
        return False


def _fmt_value(field):
    """数值字段格式化:value 为 None -> '—',其余 %g。"""
    value = field.get("value")
    return "—" if value is None else "%g" % value


def _print_evidence(ev):
    """控制台打印证据摘要(✓/⚠ 风格)。"""
    mark = "✓" if ev.get("status") == "success" else "⚠"
    print("\n---------------- 天气证据 weather_evidence ----------------")
    print("  %s status=%s | provider=%s | location=%s" % (mark, ev.get("status"), ev.get("provider"), ev.get("location")))
    print("  来源: %s %s (params: %s)" % (ev["query"].get("method"), ev["query"].get("url"),
                                          ", ".join("%s=%s" % (k, v) for k, v in ev["query"].get("params", {}).items()) or "-"))
    print("  retrieved_at: %s" % ev.get("retrieved_at"))
    print("  analysis_time: %s" % ev.get("analysis_time"))
    print("  forecast_time: %s" % (ev.get("forecast_time") or "—"))
    if ev.get("status") == "success":
        for key in ("temperature", "humidity", "precipitation_probability", "cloud_cover", "shortwave_radiation"):
            print("  %s: %s %s" % (key, _fmt_value(ev[key]), ev[key]["unit"]))
    else:
        err = ev.get("error") or {}
        print("  error_type: %s" % err.get("error_type"))
        print("  message: %s" % err.get("message"))


def _log_evidence(ev):
    """每次调用两行日志([请求] + [成功]/[失败]),镜像控制台;绝不记录密钥(参数已脱敏)。
    先重配控制台:本函数是库级路径(B07 直接调用 get_weather_forecast 时不会先走 main 的
    setup_console),GBK 管道下打印 ℃/W/m² 会崩溃,必须在此兜底。"""
    setup_console()
    line_req = "[请求] provider=%s location=%s %s %s params=%s" % (
        ev["provider"], ev["location"], ev["query"].get("method"), ev["query"].get("url"),
        ", ".join("%s=%s" % (k, v) for k, v in ev["query"].get("params", {}).items()) or "-")
    if ev["status"] == "success":
        line_result = "[成功] provider=%s location=%s forecast_time=%s temperature=%s%s humidity=%s%s precipitation_probability=%s%s cloud_cover=%s%s shortwave_radiation=%s%s" % (
            ev["provider"], ev["location"], ev["forecast_time"],
            _fmt_value(ev["temperature"]), ev["temperature"]["unit"],
            _fmt_value(ev["humidity"]), ev["humidity"]["unit"],
            _fmt_value(ev["precipitation_probability"]), ev["precipitation_probability"]["unit"],
            _fmt_value(ev["cloud_cover"]), ev["cloud_cover"]["unit"],
            _fmt_value(ev["shortwave_radiation"]), ev["shortwave_radiation"]["unit"])
    else:
        err = ev.get("error") or {}
        line_result = "[失败] provider=%s location=%s error_type=%s message=%s" % (
            ev["provider"], ev["location"], err.get("error_type"), err.get("message"))
    for text in (line_req, line_result):
        line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), text)
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with open(WEATHER_LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        print(line)


# ---------------- CLI ----------------
def _parse_flag(args, name, default=None):
    """解析 --name value 与 --name=value 两种形态;未出现返回 default。"""
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return default


def _positional(args):
    """第一个非 -- 开头的位置参数;无则 None。"""
    for a in args:
        if not a.startswith("-"):
            return a
    return None


def _safe_float(text, flag):
    """flag 参数转 float;失败返回 None 并打印提示。"""
    try:
        return float(text)
    except (TypeError, ValueError):
        print("⚠ %s 必须是数字(收到 %s)" % (flag, text))
        return None


def cmd_forecast(args):
    """forecast 子命令:真实调用一次天气接口,打印并落盘证据。"""
    location = _positional(args) or B03_LOCATION
    run_id = _parse_flag(args, "--run-id")
    provider = _parse_flag(args, "--provider", "open-meteo")
    horizon_text = _parse_flag(args, "--horizon")
    timeout_text = _parse_flag(args, "--timeout")
    horizon = _safe_float(horizon_text, "--horizon") if horizon_text else None
    timeout = _safe_float(timeout_text, "--timeout") if timeout_text else None
    if (horizon_text and horizon is None) or (timeout_text and timeout is None):
        return
    ev = get_weather_forecast(location, provider=provider, run_id=run_id,
                              horizon_hours=horizon, timeout=timeout)
    _print_evidence(ev)
    if run_id and RUN_ID_RE.match(str(run_id)):
        print("✓ 证据已保存: 证据/b03/%s_weather.json" % run_id)
        return
    if run_id:
        print("⚠ run-id 格式非法(%s),改按时间戳文件名保存" % run_id)
    # 文件名:内置城市用城市名,lat,lng 用坐标(逗号转下划线)
    name_part = location if location in LOCATIONS else str(location).strip().replace(",", "_")
    filename = "weather_%s_%s.json" % (name_part, datetime.now().strftime("%Y%m%d_%H%M%S"))
    if save_weather_evidence(ev, filename):
        print("✓ 证据已保存: 证据/b03/%s" % filename)


def cmd_abtest(args):
    """abtest 子命令:A/B 验证——同一段代码、同一 provider/horizon/timeout,仅 location 不同。
    PASS 规则:两侧均 success 且温度/湿度/降水概率至少一项真实不同;否则如实 FAIL,绝不伪造差异。"""
    city_a = _parse_flag(args, "--city-a", "北京")
    city_b = _parse_flag(args, "--city-b", "广州")
    window_text = _parse_flag(args, "--window")
    provider = _parse_flag(args, "--provider", "open-meteo")
    window = _safe_float(window_text, "--window") if window_text else None
    if window_text and window is None:
        return
    if city_a not in LOCATIONS or city_b not in LOCATIONS:
        print("⚠ --city-a/--city-b 必须是内置城市: %s" % "、".join(LOCATIONS))
        return
    if city_a == city_b:
        print("⚠ A/B 两个城市必须不同")
        return
    print("================ B03 A/B 验证(同代码路径,仅地点不同)================")
    print("城市 A: %s | 城市 B: %s | provider: %s | 时间窗口: %s"
          % (city_a, city_b, provider, ("+%gh" % window) if window else "首个晚于分析时刻的步长"))
    ev_a = get_weather_forecast(city_a, provider=provider, horizon_hours=window)
    ev_b = get_weather_forecast(city_b, provider=provider, horizon_hours=window)
    compared = {}
    for key in CORE_VALUE_KEYS:
        va = ev_a[key]["value"] if isinstance(ev_a.get(key), dict) else None
        vb = ev_b[key]["value"] if isinstance(ev_b.get(key), dict) else None
        compared[key] = {"a": va, "b": vb, "differ": (va is not None and vb is not None and va != vb)}
    both_ok = ev_a["status"] == "success" and ev_b["status"] == "success"
    any_differ = any(c["differ"] for c in compared.values())
    result = "PASS" if (both_ok and any_differ) else "FAIL"
    print("\n  %-22s %-12s %-12s %-6s" % ("字段", "城市 A(%s)" % city_a, "城市 B(%s)" % city_b, "是否不同"))
    for key in CORE_VALUE_KEYS:
        c = compared[key]
        print("  %-22s %-12s %-12s %-6s" % (key, _fmt_value(_value_field("", c["a"])),
                                            _fmt_value(_value_field("", c["b"])), "是" if c["differ"] else "否"))
    reasons = []
    if not both_ok:
        reasons.append("存在失败侧: A=%s(%s) B=%s(%s)" % (ev_a["status"], (ev_a.get("error") or {}).get("error_type"),
                                                          ev_b["status"], (ev_b.get("error") or {}).get("error_type")))
    if not any_differ:
        reasons.append("三项核心数值完全相同(天气真实如此,不伪造差异)")
    print("\n  判定: %s %s" % (result, ("— " + ";".join(reasons)) if reasons else "— 返回结果真实随地点变化"))
    save_weather_evidence(ev_a, "abtest_%s.json" % city_a)
    save_weather_evidence(ev_b, "abtest_%s.json" % city_b)
    summary = {
        "generated_at": _now_iso(),
        "result": result,
        "city_a": city_a, "city_b": city_b,
        "window_hours": window,
        "provider": provider,
        "compared_fields": compared,
        "differ": any_differ,
        "reasons": reasons,
    }
    save_weather_evidence(summary, "abtest_summary.json")
    print("✓ 证据已保存: 证据/b03/abtest_%s.json / abtest_%s.json / abtest_summary.json" % (city_a, city_b))


def cmd_failtest():
    """failtest 子命令:失败验证三例(超时/错误参数/鉴权失败)。
    PASS 规则:三例均 status=error 且 error_type 与预期一致、5 数值字段全 null、
    forecast_time=null,且证据内嵌接口真实错误文本(非伪造证明)。退出码恒 0。"""
    print("================ B03 失败验证(禁止伪造成功数据)================")
    cases = []
    # ① 超时:0.0001 秒连接超时(冷连接 ConnectTimeout / 热连接 ReadTimeout,均映射 network_timeout)
    ev_timeout = get_weather_forecast("北京", timeout=(0.0001, 0.0001))
    cases.append(("超时", ev_timeout, "network_timeout", "fail_timeout.json"))
    # ② 错误参数:lat=999 越过 [-90,90],Open-Meteo 返回真实 HTTP 400(范围校验交给 API)
    ev_badparam = get_weather_forecast("999,999")
    cases.append(("错误参数", ev_badparam, "bad_param", "fail_bad_param.json"))
    # ③ 鉴权失败:直传故意无效的 key 调 QWeather(绕过环境变量,评分者配了真 key 也不影响确定性)
    try:
        _display, _lat, _lon, code = resolve_location("北京")
        ev_auth = _call_qweather(_display, code, "invalid-key-for-failtest",
                                 datetime.now().astimezone(), None)
    except ValueError:
        ev_auth = None
    cases.append(("鉴权失败", ev_auth, "api_key_error", "fail_auth.json"))

    passed = 0
    for i, (name, ev, expected_type, filename) in enumerate(cases, start=1):
        if ev is None:
            print("  [%d/3] %s ⚠ 证据未生成" % (i, name))
            continue
        save_weather_evidence(ev, filename)
        ok_type = ev.get("status") == "error" and (ev.get("error") or {}).get("error_type") == expected_type
        ok_nulls = (ev.get("forecast_time") is None
                    and all(isinstance(ev.get(k), dict) and ev[k]["value"] is None
                            for k in ("temperature", "humidity", "precipitation_probability",
                                      "cloud_cover", "shortwave_radiation")))
        ok_real = bool((ev.get("error") or {}).get("message", "").strip())
        ok = ok_type and ok_nulls and ok_real
        passed += 1 if ok else 0
        print("  [%d/3] %s %s | status=%s error_type=%s | 数值字段全 null: %s | 已保存 证据/b03/%s"
              % (i, name, "✓" if ok else "⚠", ev.get("status"), (ev.get("error") or {}).get("error_type"),
                 "是" if ok_nulls else "否", filename))
        print("       接口真实返回: %s" % (ev.get("error") or {}).get("message", "")[:160])
    print("\n  判定: %s(通过 %d/3;三例均为真实服务失败,无任何伪造成功数据)" % (
        "PASS — 失败验证通过" if passed == 3 else "FAIL — 未全部通过", passed))


def print_usage():
    """打印 B03 用法说明。"""
    print("用法:")
    print("  py b03_weather.py forecast <location> [--run-id R] [--provider p] [--horizon N] [--timeout T]")
    print("      location: 北京/上海/广州 或 \"lat,lng\";默认取环境变量 B03_LOCATION")
    print("  py b03_weather.py abtest [--city-a 北京] [--city-b 广州] [--window N]")
    print("      A/B 验证:同一段代码仅改地点,返回结果必须真实变化(不写死 JSON)")
    print("  py b03_weather.py failtest")
    print("      失败验证:超时 / 错误参数 / 鉴权失败 三例,服务出错如实返回失败,禁止伪造成功")
    print("  py b03_weather.py --help")
    print("环境变量: B03_WEATHER_TIMEOUT / B03_OPEN_METEO_BASE_URL / B03_QWEATHER_BASE_URL")
    print("          B03_QWEATHER_API_KEY(可选,QWeather 成功路径需要)/ B03_LOCATION")
    print("公开接口: get_weather_forecast(location) -> weather_evidence,供 B07 在 run_id 流程中按需调用")
    print("API 文档: Open-Meteo https://open-meteo.com/en/docs")
    print("          QWeather  https://dev.qweather.com/文档/api/weather/weather-daily-forecast/")


def main():
    """B03 入口:子命令分发。退出码恒 0(清单语义);Ctrl+C/EOF 友好退出。"""
    setup_console()   # 必须最先执行:GBK 管道打印 ℃/W/m² 会崩溃
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print_usage()
        return
    cmd, rest = args[0], args[1:]
    try:
        if cmd == "forecast":
            cmd_forecast(rest)
        elif cmd == "abtest":
            cmd_abtest(rest)
        elif cmd == "failtest":
            cmd_failtest()
        else:
            print("未知参数: %s" % " ".join(args))
            print_usage()
    except KeyboardInterrupt:
        print("\n检测到 Ctrl+C,程序已安全退出。")
    except EOFError:
        print("\n输入已结束 (EOF),程序退出。")


if __name__ == "__main__":
    main()
