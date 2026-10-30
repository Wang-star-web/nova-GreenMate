#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 GreenMate B01 — 智能交互模块(命令行交互入口)
=====================================================================

【任务目标】
  采集番茄大棚 4 项环境参数(温度、湿度、CO₂浓度、光照),逐项校验合法性,
  每次采集自动生成唯一 run_id,原始数据保存到 evidence/raw/{run_id}.json,
  供后续模块(如 B02 分析)读取。
  B01 只负责交互输入与原始数据保存,不实现任何数据分析。

【运行方式】
  py b01_cli.py
  运行后进入主菜单:
  ① 新建一组采集数据   ② 查看历史 run_id 列表   ③ 退出程序
  采集过程中输入 q 可取消本次采集;Ctrl+C 与输入流结束(EOF)均安全退出,程序不崩溃。
  py b01_cli.py web     # 启动环境监测 Web 页面(A/B 切换 + LLM 问答 + 新建采集 + 历史记录)
  py b01_cli.py abtest  # 运行 A/B 验证(两套环境状态 + 固定提问)

【合法范围(番茄大棚正常监测范围)】
  温度      5 ~ 40 ℃
  湿度      30 ~ 90 %RH
  CO₂浓度   300 ~ 2000 ppm
  光照      0 ~ 100000 lux
=====================================================================
"""

import html
import json
import os
import secrets
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

# requests 不在此处顶层导入:B01 补全在 ask_llm 内惰性导入,
# 未安装 requests 时 CLI 采集与 Web 页面仍可正常运行。

# ---------------- 配置 ----------------
BASE_DIR = Path(__file__).resolve().parent   # 脚本所在目录,数据路径不受启动位置影响
RAW_DIR = BASE_DIR / "evidence" / "raw"      # 原始数据保存目录(运行时自动创建)

# 4 项环境参数的采集规格:一次定义,驱动输入提示、范围校验与 JSON 输出
PARAM_SPECS = [
    {"key": "temperature", "name": "温度",    "unit": "℃",   "min": 5.0,     "max": 40.0},
    {"key": "humidity",    "name": "湿度",    "unit": "%RH", "min": 30.0,    "max": 90.0},
    {"key": "co2",         "name": "CO₂浓度", "unit": "ppm", "min": 300.0,   "max": 2000.0},
    {"key": "light",       "name": "光照",    "unit": "lux", "min": 0.0,     "max": 100000.0},
]

# ---------------- B01 补全: Web 展示 / LLM 问答 / A-B 验证 配置 ----------------
# 全部可被环境变量覆盖,代码中零硬编码 API Key。
B01_LLM_BASE_URL = os.environ.get("B01_LLM_BASE_URL", "https://api.deepseek.com/chat/completions")
B01_LLM_MODEL = os.environ.get("B01_LLM_MODEL", "deepseek-chat")
try:
    B01_LLM_TIMEOUT = float(os.environ.get("B01_LLM_TIMEOUT", "30"))
except ValueError:
    B01_LLM_TIMEOUT = 30.0  # 环境变量非法时退回默认值,不崩溃
B01_WEB_HOST = "127.0.0.1"  # 只绑本机回环,不触发防火墙弹窗
try:
    B01_WEB_PORT = int(os.environ.get("B01_WEB_PORT", "8123"))
except ValueError:
    B01_WEB_PORT = 8123
LOG_DIR = BASE_DIR / "logs"                      # B01 补全运行日志目录(运行时自动创建)
LLM_LOG = LOG_DIR / "b01_llm.log"

# A/B 验证: 两套环境状态,只切换状态、固定提问(全项目唯一,永不改动)。
STATE_A = {"temperature": 25.0, "humidity": 60.0, "co2": 850.0, "light": 30000.0}  # 适宜环境(对齐 S1)
STATE_B = {"temperature": 33.0, "humidity": 88.0, "co2": 900.0, "light": 8000.0}   # 异常环境(对齐 S3)
STATE_DEFS = {"A": STATE_A, "B": STATE_B}
STATE_NAMES = {"A": "适宜环境", "B": "异常环境"}
FIXED_QUESTION = "当前环境怎么样?"
CURRENT_STATE = "A"                              # 全局当前状态,由 Web 页面 /api/switch 切换
HISTORY_WEB_LIMIT = 50                           # Web 历史结果列表最多展示的最近采集记录条数


def setup_console():
    """强制标准输出为 UTF-8:输出被管道/重定向时避免 Windows 默认 GBK 导致乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass  # 环境不支持时保持默认,不影响程序运行


def range_text(spec):
    """拼接范围描述文字,如 "5~40 ℃"。"""
    return f"{spec['min']:.0f}~{spec['max']:.0f} {spec['unit']}"


def gen_run_id():
    """生成唯一 run_id:run_YYYYmmdd_HHMMSS_<6位随机hex>;同名文件已存在则重新生成。"""
    while True:
        run_id = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}"
        if not (RAW_DIR / f"{run_id}.json").exists():
            return run_id


def input_number(spec):
    """采集单个参数:非法输入针对性报错并重新提示,直到合法。

    返回 float;按 q 取消或 Ctrl+C 时返回 None(表示放弃本次采集)。
    空输入、非数字("abc"/"12abc")、越界均不会导致程序崩溃。
    """
    while True:
        try:
            raw = input(
                f"请输入{spec['name']} (单位 {spec['unit']},合法范围 {range_text(spec)},输入 q 取消): "
            ).strip()
        except KeyboardInterrupt:
            print("\n已取消本次采集,返回主菜单。")
            return None
        if not raw:
            print(f"输入不能为空,请输入一个数字 ({spec['name']}范围 {range_text(spec)})。")
            continue
        if raw.lower() == "q":
            print("已取消本次采集,返回主菜单。")
            return None
        try:
            value = float(raw)
        except ValueError:
            print(f'"{raw}" 不是有效的数字,请重新输入 ({spec["name"]}范围 {range_text(spec)})。')
            continue
        if not spec["min"] <= value <= spec["max"]:
            print(f"{value:g} 超出{spec['name']}的正常监测范围 ({range_text(spec)}),请重新输入。")
            continue
        return value


def collect_one_run():
    """新建一组采集数据:依次采集 4 项参数,全部合法后生成 run_id 并回显;
    任一项取消则返回 None(不保存任何数据)。"""
    print("\n---------------- 新建一组采集数据 ----------------")
    data = {}
    for spec in PARAM_SPECS:
        value = input_number(spec)
        if value is None:
            return None
        data[spec["key"]] = {"value": value, "unit": spec["unit"]}

    # 注意: 此记录结构与 Web 采集路径 build_record_from_values 保持逐键一致,改动须同步
    record = {
        "run_id": gen_run_id(),
        "collect_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_type": "SIMULATED",  # 环境数据来源标记: 模拟采集
        "data": data,
        # 记录本次校验采用的合法范围:证据文件自带校验证明,便于后续模块读取
        "valid_ranges": {
            spec["key"]: {"min": spec["min"], "max": spec["max"], "unit": spec["unit"]}
            for spec in PARAM_SPECS
        },
    }

    print("\n采集完成,本次数据如下:")
    print(f"  run_id: {record['run_id']}")
    print(f"  采集时间: {record['collect_time']}")
    for spec in PARAM_SPECS:
        value = data[spec["key"]]["value"]
        print(f"  {spec['name']}: {value:g} {spec['unit']}")
    return record


def save_record(record):
    """保存原始数据到 evidence/raw/{run_id}.json(自动创建目录,UTF-8);
    保存失败仅友好提示,程序不崩溃。"""
    try:
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        file_path = RAW_DIR / f"{record['run_id']}.json"
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"⚠ 保存失败: {e}。本组数据未保存,已返回主菜单。")
        return False
    print(f"✓ 原始数据已保存: {file_path}")
    return True


# ---------------- B01 补全: Web 新建采集(服务端权威校验,规则与 CLI 完全一致) ----------------
def validate_web_input(spec, raw_text):
    """校验 Web 提交的单个参数:规则与错误文案逐条对齐 CLI input_number。
    返回 (float 值, None) 或 (None, 错误文案)。"""
    if raw_text is None or isinstance(raw_text, bool) or isinstance(raw_text, (list, dict)):
        return None, "%s 参数格式不正确,请输入一个数字 (范围 %s)" % (spec["name"], range_text(spec))
    text = str(raw_text).strip()
    if not text:
        return None, "输入不能为空,请输入一个数字 (%s范围 %s)" % (spec["name"], range_text(spec))
    try:
        value = float(text)
    except ValueError:
        return None, '"%s" 不是有效的数字,请重新输入 (%s范围 %s)' % (text[:50], spec["name"], range_text(spec))
    if not spec["min"] <= value <= spec["max"]:
        return None, "%g 超出%s的正常监测范围 (%s),请重新输入。" % (value, spec["name"], range_text(spec))
    return value, None


def validate_web_collect(payload):
    """校验 Web 新建采集提交的全部参数(逐字段)。
    返回 (field_errors, values): field_errors 为 {key: 错误文案},非空时 values 为 {}。"""
    field_errors, values = {}, {}
    for spec in PARAM_SPECS:
        value, error = validate_web_input(spec, payload.get(spec["key"]))
        if error is not None:
            field_errors[spec["key"]] = error
        else:
            values[spec["key"]] = value
    if field_errors:
        return field_errors, {}
    return {}, values


def build_record_from_values(values):
    """由已校验的 {key: float} 构建采集记录,结构与 collect_one_run 完全同构
    (run_id / collect_time / source_type=SIMULATED / data / valid_ranges),供 Web 新建采集使用。
    注意: 与 collect_one_run 内的记录构造保持逐键一致,B02~B08 依赖该结构。"""
    data = {spec["key"]: {"value": values[spec["key"]], "unit": spec["unit"]} for spec in PARAM_SPECS}
    return {
        "run_id": gen_run_id(),
        "collect_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_type": "SIMULATED",  # 环境数据来源标记: 模拟采集
        "data": data,
        # 记录本次校验采用的合法范围:证据文件自带校验证明,便于后续模块读取
        "valid_ranges": {
            spec["key"]: {"min": spec["min"], "max": spec["max"], "unit": spec["unit"]}
            for spec in PARAM_SPECS
        },
    }


# ---------------- B01 补全: LLM 问答(DeepSeek OpenAI 兼容接口) ----------------
def get_api_key():
    """读取 LLM API Key: DEEPSEEK_API_KEY 优先,回退 ANTHROPIC_AUTH_TOKEN。
    返回 (key|None, 来源名|None);绝不硬编码、绝不打印 key 值。"""
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        return key, "env:DEEPSEEK_API_KEY"
    key = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if key:
        return key, "env:ANTHROPIC_AUTH_TOKEN"
    return None, None


def format_state(state):
    """把环境状态格式化为可读行列表(名称与单位取自 PARAM_SPECS)。"""
    lines = []
    for spec in PARAM_SPECS:
        value = state.get(spec["key"])
        if value is None:
            continue
        lines.append("%s: %g %s" % (spec["name"], value, spec["unit"]))
    return lines


def build_messages(state, question):
    """构造 LLM 请求消息: system 说明助手身份与数据模拟来源,user 为固定提问 + 环境数据。"""
    lines = format_state(state)
    return [
        {"role": "system", "content":
            "你是 GreenMate 番茄大棚环境监测系统的智能助手,根据用户提供的环境监测数据回答大棚环境状况。"
            "数据来自模拟采集 (source_type=SIMULATED)。请直接、简洁地作答,不要编造数据,"
            "不要声称数据来自真实传感器。"},
        {"role": "user", "content": "%s\n\n当前环境监测数据 (source_type=SIMULATED):\n- %s"
            % (question, "\n- ".join(lines))},
    ]


def ask_llm(state, question):
    """调用 LLM 回答提问。返回 {"status":"success","answer":…} 或
    {"status":"error","error_type":…,"message":…}。
    成功路径只有一条: 解析真实 API 返回的内容。API Key 错误、网络超时等
    一律如实返回失败,绝不伪造静态成功结果。"""
    key, _key_source = get_api_key()
    if key is None:
        return {"status": "error", "error_type": "no_api_key",
                "message": "未配置 API Key(需设置环境变量 DEEPSEEK_API_KEY 或 ANTHROPIC_AUTH_TOKEN)"}
    try:
        import requests  # B01 补全: 惰性导入,未安装 requests 时仅 LLM 问答不可用
    except ImportError:
        return {"status": "error", "error_type": "missing_dependency",
                "message": "未安装 requests 库,无法调用 LLM(可执行 pip install requests)"}
    headers = {"Authorization": "Bearer %s" % key, "Content-Type": "application/json"}
    payload = {"model": B01_LLM_MODEL, "messages": build_messages(state, question), "stream": False}
    try:
        resp = requests.post(B01_LLM_BASE_URL, json=payload, headers=headers,
                             timeout=(B01_LLM_TIMEOUT, B01_LLM_TIMEOUT))
    except requests.exceptions.ConnectTimeout:
        return {"status": "error", "error_type": "network_timeout",
                "message": "连接超时(超过 %g 秒未建立连接),请检查 B01_LLM_BASE_URL 与网络" % B01_LLM_TIMEOUT}
    except requests.exceptions.ReadTimeout:
        return {"status": "error", "error_type": "network_timeout",
                "message": "读取超时(服务器 %g 秒内未返回结果)" % B01_LLM_TIMEOUT}
    except requests.exceptions.ConnectionError:
        return {"status": "error", "error_type": "network_timeout",
                "message": "网络连接失败,无法访问 %s" % B01_LLM_BASE_URL}
    except Exception as e:  # 其他网络异常如实上报
        return {"status": "error", "error_type": "network_timeout", "message": "网络异常: %s" % e}
    if resp.status_code in (401, 403):
        return {"status": "error", "error_type": "api_key_error",
                "message": "HTTP %d 认证失败,API Key 无效或已过期" % resp.status_code}
    if resp.status_code != 200:
        return {"status": "error", "error_type": "http_error",
                "message": "HTTP %d: %s" % (resp.status_code, resp.text[:200])}
    try:
        answer = resp.json()["choices"][0]["message"]["content"]
    except Exception:
        return {"status": "error", "error_type": "bad_response",
                "message": "API 返回内容无法解析(缺少 choices[0].message.content)"}
    return {"status": "success", "answer": str(answer)}


def log_llm(text):
    """追加一行时间戳日志到 logs/b01_llm.log(自动建目录),并镜像到控制台。"""
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), text)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LLM_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        print("⚠ 日志写入失败: %s" % e)
    print(line)


# ---------------- B01 补全: Web 展示页面(纯标准库本地服务) ----------------
PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GreenMate B01 环境监测 — Web 展示</title>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --ink-3: #898781;
  --border: rgba(11, 11, 11, 0.10); --grid: #e1e0d9; --bar: #2a78d6;
  --good: #0ca30c; --warn: #fab219; --serious: #ec835a; --crit: #d03b3b; --ok-text: #006300;
  --warn-text: #8a5b00;   /* 浅色主题下 warn 直接做文字对比度不足(1.79:1),用深琥珀 */
  /* 数字孪生变量 */
  --sky-a: #eaf4fb; --sky-b: #f2f7ec;
  --glass-a: rgba(165, 214, 255, 0.30); --glass-b: rgba(210, 235, 255, 0.14);
  --glass-edge: rgba(120, 170, 220, 0.35);
  --roof-a: rgba(140, 200, 240, 0.55); --roof-b: rgba(200, 232, 252, 0.30);
  --ground-a: #d8e4d0; --ground-b: #b9c9ae;
  --sun: #fab219; --fruit: #d8402e; --cloud: rgba(190, 205, 215, 0.9); --cloud-warn: rgba(150, 165, 160, 0.95);
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --ink-3: #898781;
    --border: rgba(255, 255, 255, 0.10); --grid: #2c2c2a; --bar: #3987e5; --ok-text: #0ca30c;
    --warn-text: #fab219;
    --sky-a: #12171c; --sky-b: #0f1512;
    --glass-a: rgba(90, 145, 200, 0.20); --glass-b: rgba(70, 110, 160, 0.10);
    --glass-edge: rgba(140, 180, 220, 0.22);
    --roof-a: rgba(70, 120, 170, 0.45); --roof-b: rgba(50, 90, 140, 0.25);
    --ground-a: #232a24; --ground-b: #161c17;
    --sun: #ffb347; --fruit: #e05a45; --cloud: rgba(120, 135, 150, 0.75); --cloud-warn: rgba(130, 140, 135, 0.85);
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif; line-height: 1.6; }
.wrap { max-width: 980px; margin: 0 auto; padding: 24px 20px 48px; }
h1 { font-size: 24px; margin: 0 0 4px; }
.sub { color: var(--ink-3); font-size: 12px; }
.panel { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 16px 18px; margin: 16px 0; }
.panel h2 { font-size: 15px; margin: 0 0 10px; }
.hero { display: grid; grid-template-columns: 1fr 460px; gap: 20px; align-items: center; margin-bottom: 8px; }
@media (max-width: 860px) { .hero { grid-template-columns: 1fr; } }
/* ---------- TWIN:CSS-START(三页同源复制块)---------- */
.twin { position: relative; perspective: 900px; perspective-origin: 50% 20%; height: 300px;
        overflow: hidden; border-radius: 16px; border: 1px solid var(--border);
        background: linear-gradient(180deg, var(--sky-a), var(--sky-b)); transition: background .6s; }
.twin-scene { position: absolute; left: 50%; top: 0; width: 460px; height: 320px; margin-left: -230px;
              transform-style: preserve-3d; }
.twin-sun { position: absolute; top: 26px; right: 36px; width: 64px; height: 64px;
            animation: sun-pulse 6s ease-in-out infinite; transition: transform .6s, filter .6s; }
/* SVG 呈现属性不支持 var(),颜色一律经 CSS 规则注入 */
.twin-sun circle { fill: var(--sun); }
.twin-sun g { stroke: var(--sun); }
.twin-clouds svg path { fill: var(--cloud); }
.crops circle { fill: var(--fruit); }
.twin-clouds { position: absolute; top: 8px; left: 0; right: 0; height: 46px; opacity: .25; transition: opacity .6s; }
.twin-clouds svg { position: absolute; width: 96px; height: 40px; animation: cloud-drift 14s ease-in-out infinite alternate; }
.twin-clouds .cloud2 { left: 300px; top: 10px; opacity: 0; animation-delay: -7s; transition: opacity .6s; }
.gh { position: absolute; left: 100px; bottom: 40px; width: 260px; height: 150px;
      transform-style: preserve-3d; transform: rotateX(-16deg) rotateY(-26deg);
      animation: twin-in 1.1s ease-out both; }
.gh-wall { position: absolute; border: 1px solid var(--glass-edge);
           background-image: repeating-linear-gradient(90deg, var(--glass-edge) 0 2px, transparent 2px 44px),
                             linear-gradient(180deg, var(--glass-a), var(--glass-b)); }
.gh-back { width: 260px; height: 150px; transform: rotateY(180deg) translateZ(90px); }
.gh-front { width: 260px; height: 150px; transform: translateZ(90px); }
.gh-left, .gh-right { width: 180px; height: 150px; left: 40px;
                      background-image: repeating-linear-gradient(90deg, var(--glass-edge) 0 2px, transparent 2px 36px),
                                        linear-gradient(180deg, var(--glass-b), var(--glass-a)); }
.gh-left { transform: rotateY(-90deg) translateZ(130px); }
.gh-right { transform: rotateY(90deg) translateZ(130px); }
.gh-roof { position: absolute; left: 0; width: 260px; height: 150px; top: -123px;
           transform-origin: 50% 0; border: 1px solid var(--glass-edge);
           background: linear-gradient(180deg, var(--roof-a), var(--roof-b)); transition: background .6s; }
.gh-roof-a { transform: rotateX(-35deg); }
.gh-roof-b { transform: rotateX(35deg); }
.crops { position: absolute; left: 30px; bottom: 0; width: 200px; height: 96px; transform-style: preserve-3d;
         animation: crop-sway 8s ease-in-out infinite alternate; }
.crop-row { position: absolute; bottom: 0; width: 200px; height: 96px; }
.crop-row.r1 { transform: translateZ(-60px); }
.crop-row.r2 { transform: translateZ(0); }
.crop-row.r3 { transform: translateZ(60px); }
.mist { position: absolute; left: 0; right: 0; top: 12px; height: 120px; opacity: .15;
        animation: mist-drift 7s ease-in-out infinite alternate; transition: opacity .6s; }
.mist-drop { position: absolute; width: 26px; height: 14px; border-radius: 50%;
             background: radial-gradient(ellipse at center, rgba(255,255,255,.85), rgba(255,255,255,0));
             animation: drop-rise 3.2s linear infinite; animation-play-state: paused; }
.mist-drop:nth-child(1) { left: 12%; top: 78%; animation-delay: 0s; }
.mist-drop:nth-child(2) { left: 30%; top: 60%; animation-delay: .45s; }
.mist-drop:nth-child(3) { left: 48%; top: 84%; animation-delay: .9s; }
.mist-drop:nth-child(4) { left: 64%; top: 56%; animation-delay: 1.35s; }
.mist-drop:nth-child(5) { left: 80%; top: 80%; animation-delay: 1.8s; }
.mist-drop:nth-child(6) { left: 88%; top: 48%; animation-delay: 2.25s; }
.fan { position: absolute; right: 0; top: 52px; width: 44px; height: 44px;
       transform: rotateY(90deg) translateZ(130px); }
.fan svg { animation: fan-spin 2.4s linear infinite; }
.rays { position: absolute; left: 40px; top: 0; width: 180px; height: 150px; opacity: .85;
        transform: translateZ(60px); transition: opacity .6s; }
.ray { position: absolute; top: -10px; width: 34px; height: 170px; border-radius: 40%;
       background: linear-gradient(180deg, rgba(255,240,190,.75), rgba(255,240,190,0));
       animation: ray-shimmer 4s ease-in-out infinite; }
.ray.r1 { left: 20px; transform: rotate(14deg); }
.ray.r2 { left: 76px; animation-delay: .9s; }
.ray.r3 { left: 132px; transform: rotate(-14deg); animation-delay: 1.8s; }
.gh-platform { position: absolute; left: 50%; bottom: 6px; width: 400px; height: 44px; margin-left: -200px;
               border-radius: 50%; background: radial-gradient(ellipse at center, var(--ground-a), var(--ground-b)); }
.twin-badge { position: absolute; left: 14px; bottom: 12px; padding: 3px 12px; border-radius: 999px;
              font-size: 12px; background: var(--surface); border: 1px solid var(--border); color: var(--ink-2); }
.twin.watch::after { content: ""; position: absolute; inset: 0; border-radius: 16px; pointer-events: none;
                     animation: watch-pulse 1.8s ease-out infinite; }
/* 状态 class:阈值对齐 B03 规则 R1/R3/R6/R7 */
.twin.temp-hot .twin-sun { fill: var(--serious); transform: translateY(-8px) scale(1.16); }
.twin.temp-hot .twin-sun circle { fill: var(--serious); }
.twin.temp-hot .twin-sun g { stroke: var(--serious); }
.twin.temp-hot .gh-roof { background: linear-gradient(180deg, var(--roof-a), var(--roof-b)),
                          rgba(236, 131, 90, 0.25); }
.twin.temp-hot .fan svg { animation-duration: 1.1s; }
.twin.hum-high .mist { opacity: .82; }
.twin.hum-high .mist-drop { animation-play-state: running; }
.twin.co2-high .twin-clouds { opacity: .9; }
.twin.co2-high .twin-clouds svg path { fill: var(--cloud-warn); }
.twin.co2-high .cloud2 { opacity: 1; }
.twin.co2-high .twin-clouds svg { animation-duration: 8s; }
.twin.light-low .rays { opacity: .12; }
.twin.light-low .ray { animation-play-state: paused; }
.twin.light-low .twin-sun { filter: brightness(.75); }
.twin.light-low { background: linear-gradient(180deg, #c7ccd4, #dfe3da); }
@keyframes twin-in { from { transform: rotateX(-4deg) rotateY(-26deg) scale(.92) translateY(26px); opacity: 0; }
                     to { transform: rotateX(-16deg) rotateY(-26deg) scale(1) translateY(0); opacity: 1; } }
@keyframes sun-pulse { 0%,100% { transform: scale(1); filter: brightness(1); } 50% { transform: scale(1.06); filter: brightness(1.08); } }
@keyframes ray-shimmer { 0%,100% { opacity: .5; } 50% { opacity: .9; } }
@keyframes mist-drift { from { transform: translateX(-5%); } to { transform: translateX(5%); } }
@keyframes drop-rise { 0% { transform: translateY(6px) scale(.7); opacity: 0; }
                       30% { opacity: .85; } 100% { transform: translateY(-10px) scale(1); opacity: 0; } }
@keyframes fan-spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
@keyframes cloud-drift { from { transform: translateX(-10%); } to { transform: translateX(10%); } }
@keyframes watch-pulse { 0% { box-shadow: 0 0 0 3px rgba(250,178,25,.55); } 100% { box-shadow: 0 0 0 16px rgba(250,178,25,0); } }
@keyframes crop-sway { from { transform: rotateX(1.5deg); } to { transform: rotateX(-1.5deg); } }
@media (max-width: 760px) {
  .twin { height: 230px; }
  .twin-scene { transform: scale(.72); transform-origin: 50% 0; }
  .mist-drop:nth-child(n+4) { display: none; }
  .crops { animation: none; }
}
@media (prefers-reduced-motion: reduce) {
  .twin * { animation: none !important; }
}
/* ---------- TWIN:CSS-END ---------- */
.state-row { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
.state-row label { font-size: 14px; }
.switch-seg { display: inline-flex; border: 1px solid var(--border); border-radius: 999px; overflow: hidden; background: var(--surface); }
.switch-seg label { position: relative; padding: 6px 18px; cursor: pointer; font-size: 13px; color: var(--ink-2); }
.switch-seg input { position: absolute; opacity: 0; width: 0; }
.switch-seg label:has(input:checked) { background: var(--bar); color: #fff; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; }
.metric-card { background: var(--surface); border: 1px solid var(--border); border-left: 4px solid var(--good);
               border-radius: 10px; padding: 12px 14px; display: flex; flex-direction: column; gap: 4px; }
.metric-card.bad { border-left-color: var(--serious); }
.metric-head { display: flex; align-items: center; gap: 8px; color: var(--ink-3); font-size: 12px; }
.metric-head svg { width: 18px; height: 18px; }
.metric-value { font-size: 24px; font-weight: 600; font-variant-numeric: tabular-nums; }
.metric-unit { font-size: 12px; color: var(--ink-2); }
.metric-tip { font-size: 12px; }
.metric-tip.ok { color: var(--ok-text); }
.metric-tip.bad { color: var(--warn-text); }
.sim-badge { display: inline-block; margin-top: 2px; padding: 1px 8px; border: 1px solid var(--bar); color: var(--bar); border-radius: 4px; font-size: 11px; }
button { padding: 7px 18px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); color: var(--ink); cursor: pointer; font-size: 14px; }
button:hover { border-color: var(--bar); }
button.primary { background: var(--bar); border-color: var(--bar); color: #fff; }
#answer-box { margin-top: 10px; padding: 10px 14px; border-radius: 8px; white-space: pre-wrap; font-size: 14px; }
#answer-box.ok { border: 1px solid var(--good); color: var(--ok-text); }
#answer-box.err { border: 1px solid var(--crit); color: var(--crit); }
#answer-box.empty { border: 1px dashed var(--grid); color: var(--ink-3); }
.hidden { display: none; }
#degrade-banner { margin: 14px 0; padding: 10px 14px; border: 1px solid var(--warn); color: var(--warn-text); border-radius: 8px; font-size: 13px; background: var(--surface); }
.collect-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 8px 0; }
.collect-label { width: 86px; font-size: 14px; display: flex; align-items: center; gap: 6px; }
.collect-label svg { width: 16px; height: 16px; }
.collect-row input[type=text] { flex: 1; min-width: 150px; padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface); color: var(--ink); font-size: 14px; }
.collect-row input[type=text].input-err { border-color: var(--crit); }
.collect-unit { width: 44px; color: var(--ink-3); font-size: 12px; }
.collect-err { flex-basis: 100%; color: var(--crit); font-size: 12px; margin-left: 96px; }
#collect-box { margin-top: 10px; padding: 10px 14px; border-radius: 8px; white-space: pre-wrap; font-size: 14px; }
#collect-box.ok { border: 1px solid var(--good); color: var(--ok-text); }
#collect-box.err { border: 1px solid var(--crit); color: var(--crit); }
#collect-box.empty { border: 1px dashed var(--grid); color: var(--ink-3); }
table.history { width: 100%; border-collapse: collapse; font-size: 13px; }
.history th, .history td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border); }
.history th { color: var(--ink-3); font-weight: 500; white-space: nowrap; }
.history .run-id { font-family: ui-monospace, Consolas, monospace; font-size: 12px; }
.history .src { color: var(--ink-2); font-size: 12px; white-space: nowrap; }
#history-empty { color: var(--ink-3); font-size: 13px; padding: 8px 0; }
</style>
</head>
<body>
<div class="wrap">
  <div class="hero">
    <div>
      <h1>🌱 GreenMate B01 环境监测</h1>
      <p class="sub">数字孪生 · 实时联动环境状态(A/B 切换即见孪生变化)· 数据标记 source_type=SIMULATED</p>
    </div>
    <!-- TWIN:HTML-START(三页同源复制块)-->
    <div class="twin" id="twin">
      <div class="twin-scene">
        <svg class="twin-sun" viewBox="0 0 64 64" aria-hidden="true">
          <circle cx="32" cy="32" r="14"/>
          <g stroke="var(--sun)" stroke-width="3" stroke-linecap="round">
            <line x1="32" y1="4" x2="32" y2="12"/><line x1="32" y1="52" x2="32" y2="60"/>
            <line x1="4" y1="32" x2="12" y2="32"/><line x1="52" y1="32" x2="60" y2="32"/>
            <line x1="12" y1="12" x2="18" y2="18"/><line x1="46" y1="46" x2="52" y2="52"/>
            <line x1="12" y1="52" x2="18" y2="46"/><line x1="46" y1="18" x2="52" y2="12"/>
          </g>
        </svg>
        <div class="twin-clouds" aria-hidden="true">
          <svg viewBox="0 0 96 40"><path d="M22 30 a10 10 0 0 1 0 -20 a13 13 0 0 1 24 -2 a9 9 0 0 1 8 14 a10 10 0 0 1 -4 8 z" fill="var(--cloud)"/></svg>
          <svg class="cloud2" viewBox="0 0 96 40"><path d="M22 30 a10 10 0 0 1 0 -20 a13 13 0 0 1 24 -2 a9 9 0 0 1 8 14 a10 10 0 0 1 -4 8 z" fill="var(--cloud)"/></svg>
        </div>
        <div class="gh">
          <div class="gh-wall gh-back"></div>
          <div class="gh-wall gh-left"></div>
          <div class="gh-wall gh-right"></div>
          <div class="gh-wall gh-front"></div>
          <div class="gh-roof gh-roof-a"></div>
          <div class="gh-roof gh-roof-b"></div>
          <div class="crops" aria-hidden="true">
            <svg class="crop-row r1" viewBox="0 0 200 96">
              <g transform="translate(20,0)"><path d="M30 92 C30 62 30 40 30 22" stroke="#3f7d3a" stroke-width="3" fill="none"/><path d="M30 40 C22 35 14 32 8 27 C14 23 22 20 30 26 Z" fill="#4f9c46"/><path d="M30 50 C38 45 46 42 52 38 C46 34 38 31 30 36 Z" fill="#4f9c46"/><circle cx="24" cy="32" r="4.5" fill="var(--fruit)"/><circle cx="37" cy="26" r="4.5" fill="var(--fruit)"/><circle cx="30" cy="18" r="4.5" fill="var(--fruit)"/></g>
              <g transform="translate(90,0)"><path d="M30 92 C30 62 30 40 30 22" stroke="#3f7d3a" stroke-width="3" fill="none"/><path d="M30 40 C22 35 14 32 8 27 C14 23 22 20 30 26 Z" fill="#4f9c46"/><path d="M30 50 C38 45 46 42 52 38 C46 34 38 31 30 36 Z" fill="#4f9c46"/><circle cx="24" cy="32" r="4.5" fill="var(--fruit)"/><circle cx="37" cy="26" r="4.5" fill="var(--fruit)"/><circle cx="30" cy="18" r="4.5" fill="var(--fruit)"/></g>
              <g transform="translate(160,0)"><path d="M30 92 C30 62 30 40 30 22" stroke="#3f7d3a" stroke-width="3" fill="none"/><path d="M30 40 C22 35 14 32 8 27 C14 23 22 20 30 26 Z" fill="#4f9c46"/><path d="M30 50 C38 45 46 42 52 38 C46 34 38 31 30 36 Z" fill="#4f9c46"/><circle cx="24" cy="32" r="4.5" fill="var(--fruit)"/><circle cx="37" cy="26" r="4.5" fill="var(--fruit)"/><circle cx="30" cy="18" r="4.5" fill="var(--fruit)"/></g>
            </svg>
            <svg class="crop-row r2" viewBox="0 0 200 96">
              <g transform="translate(20,0)"><path d="M30 92 C30 62 30 40 30 22" stroke="#3f7d3a" stroke-width="3" fill="none"/><path d="M30 40 C22 35 14 32 8 27 C14 23 22 20 30 26 Z" fill="#4f9c46"/><path d="M30 50 C38 45 46 42 52 38 C46 34 38 31 30 36 Z" fill="#4f9c46"/><circle cx="24" cy="32" r="4.5" fill="var(--fruit)"/><circle cx="37" cy="26" r="4.5" fill="var(--fruit)"/><circle cx="30" cy="18" r="4.5" fill="var(--fruit)"/></g>
              <g transform="translate(90,0)"><path d="M30 92 C30 62 30 40 30 22" stroke="#3f7d3a" stroke-width="3" fill="none"/><path d="M30 40 C22 35 14 32 8 27 C14 23 22 20 30 26 Z" fill="#4f9c46"/><path d="M30 50 C38 45 46 42 52 38 C46 34 38 31 30 36 Z" fill="#4f9c46"/><circle cx="24" cy="32" r="4.5" fill="var(--fruit)"/><circle cx="37" cy="26" r="4.5" fill="var(--fruit)"/><circle cx="30" cy="18" r="4.5" fill="var(--fruit)"/></g>
              <g transform="translate(160,0)"><path d="M30 92 C30 62 30 40 30 22" stroke="#3f7d3a" stroke-width="3" fill="none"/><path d="M30 40 C22 35 14 32 8 27 C14 23 22 20 30 26 Z" fill="#4f9c46"/><path d="M30 50 C38 45 46 42 52 38 C46 34 38 31 30 36 Z" fill="#4f9c46"/><circle cx="24" cy="32" r="4.5" fill="var(--fruit)"/><circle cx="37" cy="26" r="4.5" fill="var(--fruit)"/><circle cx="30" cy="18" r="4.5" fill="var(--fruit)"/></g>
            </svg>
            <svg class="crop-row r3" viewBox="0 0 200 96">
              <g transform="translate(20,0)"><path d="M30 92 C30 62 30 40 30 22" stroke="#3f7d3a" stroke-width="3" fill="none"/><path d="M30 40 C22 35 14 32 8 27 C14 23 22 20 30 26 Z" fill="#4f9c46"/><path d="M30 50 C38 45 46 42 52 38 C46 34 38 31 30 36 Z" fill="#4f9c46"/><circle cx="24" cy="32" r="4.5" fill="var(--fruit)"/><circle cx="37" cy="26" r="4.5" fill="var(--fruit)"/><circle cx="30" cy="18" r="4.5" fill="var(--fruit)"/></g>
              <g transform="translate(90,0)"><path d="M30 92 C30 62 30 40 30 22" stroke="#3f7d3a" stroke-width="3" fill="none"/><path d="M30 40 C22 35 14 32 8 27 C14 23 22 20 30 26 Z" fill="#4f9c46"/><path d="M30 50 C38 45 46 42 52 38 C46 34 38 31 30 36 Z" fill="#4f9c46"/><circle cx="24" cy="32" r="4.5" fill="var(--fruit)"/><circle cx="37" cy="26" r="4.5" fill="var(--fruit)"/><circle cx="30" cy="18" r="4.5" fill="var(--fruit)"/></g>
              <g transform="translate(160,0)"><path d="M30 92 C30 62 30 40 30 22" stroke="#3f7d3a" stroke-width="3" fill="none"/><path d="M30 40 C22 35 14 32 8 27 C14 23 22 20 30 26 Z" fill="#4f9c46"/><path d="M30 50 C38 45 46 42 52 38 C46 34 38 31 30 36 Z" fill="#4f9c46"/><circle cx="24" cy="32" r="4.5" fill="var(--fruit)"/><circle cx="37" cy="26" r="4.5" fill="var(--fruit)"/><circle cx="30" cy="18" r="4.5" fill="var(--fruit)"/></g>
            </svg>
          </div>
          <div class="mist" aria-hidden="true">
            <span class="mist-drop"></span><span class="mist-drop"></span><span class="mist-drop"></span>
            <span class="mist-drop"></span><span class="mist-drop"></span><span class="mist-drop"></span>
          </div>
          <div class="fan" aria-hidden="true">
            <svg viewBox="0 0 44 44"><circle cx="22" cy="22" r="5" fill="#8a929b"/><g fill="#aab3bc">
              <path d="M22 17 C27 17 29 12 29 7 C24 7 19 12 19 17 Z"/><path d="M22 27 C27 27 29 32 29 37 C24 37 19 32 19 27 Z"/>
              <path d="M17 22 C17 17 12 15 7 15 C7 20 12 25 17 25 Z"/><path d="M27 22 C27 17 32 15 37 15 C37 20 32 25 27 25 Z"/></g></svg>
          </div>
          <div class="rays" aria-hidden="true">
            <span class="ray r1"></span><span class="ray r2"></span><span class="ray r3"></span>
          </div>
        </div>
        <div class="gh-platform" aria-hidden="true"></div>
      </div>
      <div class="twin-badge" id="twin-badge">数字孪生 · 实时状态</div>
    </div>
    <!-- TWIN:HTML-END -->
  </div>
  <div class="panel">
    <h2>环境状态(A/B 切换 · 孪生实时联动)</h2>
    <div class="state-row">
      <span>当前状态: <b id="state-label">__STATE__</b></span>
      <span class="switch-seg">
        <label><input type="radio" name="state" value="A"> 状态 A · 适宜环境</label>
        <label><input type="radio" name="state" value="B"> 状态 B · 异常环境</label>
      </span>
    </div>
  </div>
  <div class="panel">
    <h2>环境数据展示</h2>
    <div class="cards" id="metrics">__METRICS__</div>
  </div>
  <div id="degrade-banner" class="hidden"></div>
  <div class="panel" id="collect-panel">
    <h2>新建采集(与命令行同规则,服务端校验)</h2>
    <p class="sub">合法范围与 CLI 采集一致;提交成功后自动保存到 evidence/raw/ 并刷新历史列表。</p>
    __COLLECT_FORM__
    <button type="button" id="btn-collect" class="primary">提交采集</button>
    <div id="collect-box" class="empty hidden"></div>
  </div>
  <div class="panel">
    <h2>历史采集结果(最近 __HISTORY_LIMIT__ 条)</h2>
    <table class="history">
      <thead><tr><th>run_id</th><th>采集时间</th><th>温度(℃)</th><th>湿度(%RH)</th><th>CO₂(ppm)</th><th>光照(lux)</th><th>来源</th></tr></thead>
      <tbody id="history-body"></tbody>
    </table>
    <div id="history-empty" class="hidden">暂无历史采集记录</div>
  </div>
  <div class="panel">
    <h2>固定提问</h2>
    <p><b>当前环境怎么样?</b> <button id="btn-ask" class="primary">提问</button></p>
    <div id="answer-box" class="empty hidden"></div>
  </div>
</div>
<script>
function stateLabel(s) { return s + (s === "A" ? "(适宜环境)" : "(异常环境)"); }
// 指标图标(内联 SVG,零外部资源)
var PARAM_ICONS = {
  "温度": '<svg viewBox="0 0 20 20" fill="none"><path d="M10 3a2 2 0 0 0-2 2v7.2a4 4 0 1 0 4 0V5a2 2 0 0 0-2-2Z" stroke="currentColor" stroke-width="1.5"/><circle cx="10" cy="14.5" r="1.8" fill="currentColor"/></svg>',
  "湿度": '<svg viewBox="0 0 20 20" fill="none"><path d="M10 3S4.5 9.2 4.5 13a5.5 5.5 0 0 0 11 0C15.5 9.2 10 3 10 3Z" stroke="currentColor" stroke-width="1.5"/></svg>',
  "CO₂浓度": '<svg viewBox="0 0 20 20" fill="none"><circle cx="7" cy="11" r="2.2" stroke="currentColor" stroke-width="1.5"/><circle cx="11.5" cy="8.5" r="1.5" stroke="currentColor" stroke-width="1.5"/><circle cx="11.5" cy="13.5" r="2.8" stroke="currentColor" stroke-width="1.5"/></svg>',
  "光照": '<svg viewBox="0 0 20 20" fill="none"><circle cx="10" cy="10" r="3.2" stroke="currentColor" stroke-width="1.5"/><path d="M10 1.5v2M10 16.5v2M1.5 10h2M16.5 10h2M4 4l1.4 1.4M14.6 14.6 16 16M16 4l-1.4 1.4M5.4 14.6 4 16" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>'
};
// 状态阈值:对齐 B03 规则 R1/R3/R6/R7(单一事实源仅此处;服务端不注入状态类)
function paramState(name, value) {
  var v = parseFloat(value);
  if (isNaN(v)) { return null; }
  if (name === "温度" && v > 30) { return { cls: "bad", tip: "偏高(>30℃,R1)" }; }
  if (name === "湿度" && v > 85) { return { cls: "bad", tip: "偏高(>85%RH,R3)" }; }
  if (name === "CO₂浓度" && v > 1500) { return { cls: "bad", tip: "偏高(>1500ppm,R6)" }; }
  if (name === "光照" && v < 10000) { return { cls: "bad", tip: "弱光(<10000lux,R7)" }; }
  return { cls: "ok", tip: "适宜" };
}
function setCls(el, name, on) { if (on) { el.classList.add(name); } else { el.classList.remove(name); } }
function updateTwin(items) {
  var t = document.getElementById("twin");
  if (!t) { return; }
  var map = {};
  for (var i = 0; i < items.length; i++) { map[items[i].name] = parseFloat(items[i].value); }
  setCls(t, "temp-hot", map["温度"] > 30);
  setCls(t, "hum-high", map["湿度"] > 85);
  setCls(t, "co2-high", map["CO₂浓度"] > 1500);
  setCls(t, "light-low", map["光照"] < 10000);
  var watch = t.classList.contains("temp-hot") || t.classList.contains("hum-high")
           || t.classList.contains("co2-high") || t.classList.contains("light-low");
  setCls(t, "watch", watch);
  var badge = document.getElementById("twin-badge");
  if (badge) { badge.textContent = watch ? "数字孪生 · 需要关注" : "数字孪生 · 适宜"; }
}
function render(data) {
  document.getElementById("state-label").innerText = stateLabel(data.state);
  var radios = document.querySelectorAll('input[name="state"]');
  for (var i = 0; i < radios.length; i++) { radios[i].checked = radios[i].value === data.state; }
  var cards = "";
  var items = data.metrics.items;
  for (var i = 0; i < items.length; i++) {
    var it = items[i];
    var st = paramState(it.name, it.value) || { cls: "ok", tip: "—" };
    cards += '<div class="metric-card ' + st.cls + '"><div class="metric-head">' + (PARAM_ICONS[it.name] || "")
      + '<span>' + it.name + '</span></div>'
      + '<div class="metric-value">' + it.value + '<span class="metric-unit"> ' + it.unit + '</span></div>'
      + '<div class="metric-tip ' + st.cls + '">' + st.tip + '</div>'
      + '<span class="sim-badge">source_type=' + it.source_type + '</span></div>';
  }
  document.getElementById("metrics").innerHTML = cards;
  updateTwin(items);
  if (data.llm) {
    if (data.llm.key_configured) { if (!llmFailed) { hideBanner(); } }
    else { showBanner("未配置 LLM API Key(需设置 DEEPSEEK_API_KEY 或 ANTHROPIC_AUTH_TOKEN),在线问答暂不可用;"); }
  }
}
function loadState() {
  fetch("/api/state").then(function (r) { return r.json(); }).then(render);
}
function postJson(url, body, cb) {
  fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) })
    .then(function (r) { return r.json(); }).then(cb);
}
function showAnswer(status, answer, errorType, message) {
  var box = document.getElementById("answer-box");
  box.classList.remove("hidden");
  if (status === "success") {
    box.className = "ok";
    box.innerText = "回答: " + answer;
  } else {
    box.className = "err";
    box.innerText = "失败 [" + errorType + "]: " + message;
  }
}
var llmFailed = false;
function showBanner(msg) {
  var b = document.getElementById("degrade-banner");
  b.textContent = "提示: " + msg + " 本地新建采集与历史查看功能不受影响,可继续使用。";
  b.classList.remove("hidden");
}
function hideBanner() { document.getElementById("degrade-banner").classList.add("hidden"); }
function clearCollectErrors() {
  var errs = document.querySelectorAll(".collect-err");
  for (var i = 0; i < errs.length; i++) { errs[i].textContent = ""; errs[i].classList.add("hidden"); }
  var ins = document.querySelectorAll("#collect-panel input[type=text]");
  for (var j = 0; j < ins.length; j++) { ins[j].classList.remove("input-err"); }
}
function showCollectResult(ok, message) {
  var box = document.getElementById("collect-box");
  box.classList.remove("hidden");
  box.className = ok ? "ok" : "err";
  box.innerText = message;
}
function fmtVal(v) { return (v === null || v === undefined) ? "—" : String(v); }
function loadHistory() {
  fetch("/api/history").then(function (r) { return r.json(); }).then(function (d) {
    var tbody = document.getElementById("history-body");
    var empty = document.getElementById("history-empty");
    tbody.textContent = "";
    if (!d.records || d.records.length === 0) { empty.classList.remove("hidden"); return; }
    empty.classList.add("hidden");
    for (var i = 0; i < d.records.length; i++) {
      var r = d.records[i];
      var tr = document.createElement("tr");
      var cells = [r.run_id, r.collect_time, fmtVal(r.temperature), fmtVal(r.humidity),
                   fmtVal(r.co2), fmtVal(r.light), r.source_type || "—"];
      for (var j = 0; j < cells.length; j++) {
        var td = document.createElement("td");
        td.textContent = cells[j];  // textContent 渲染,天然防 XSS
        if (j === 0) { td.className = "run-id"; }
        if (j === 6) { td.className = "src"; }
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
  });
}
document.addEventListener("DOMContentLoaded", function () {
  loadState();
  var radios = document.querySelectorAll('input[name="state"]');
  for (var i = 0; i < radios.length; i++) {
    radios[i].addEventListener("change", function () {
      postJson("/api/switch", { state: this.value }, function (d) {
        if (d.status === "ok") { render(d); }
        else { showAnswer("error", d.error_type || "server_error", d.message || "切换失败"); }
      });
    });
  }
  document.getElementById("btn-ask").addEventListener("click", function () {
    postJson("/api/ask", {}, function (d) {
      showAnswer(d.status, d.answer, d.error_type, d.message);
      if (d.status === "success") { llmFailed = false; hideBanner(); }
      else { llmFailed = true; showBanner("在线问答失败 [" + (d.error_type || "unknown") + "]: " + (d.message || "")); }
    });
  });
  loadHistory();
  document.getElementById("btn-collect").addEventListener("click", function () {
    clearCollectErrors();
    var payload = {};
    var ins = document.querySelectorAll("#collect-panel input[type=text]");
    for (var i = 0; i < ins.length; i++) { payload[ins[i].name] = ins[i].value; }
    postJson("/api/collect", payload, function (d) {
      if (d.status === "ok") {
        showCollectResult(true, "采集成功: run_id=" + d.record.run_id + ",已保存到 " + d.saved_path);
        loadHistory();
      } else if (d.error_type === "invalid_params" && d.field_errors) {
        var keys = Object.keys(d.field_errors);
        for (var k = 0; k < keys.length; k++) {
          var key = keys[k];
          var errEl = document.getElementById("err-" + key);
          if (errEl) { errEl.textContent = d.field_errors[key]; errEl.classList.remove("hidden"); }
          var inEl = document.getElementById("in-" + key);
          if (inEl) { inEl.classList.add("input-err"); }
        }
        showCollectResult(false, d.message || "参数不合法");
      } else {
        showCollectResult(false, d.message || "保存失败");
      }
    });
  });
});
</script>
</body>
</html>
"""


def build_metrics(state):
    """把环境状态转为指标展示列表(每项标记 source_type=SIMULATED)。"""
    items = []
    for spec in PARAM_SPECS:
        value = state.get(spec["key"])
        if value is None:
            continue
        items.append({"name": spec["name"], "value": "%g" % value,
                      "unit": spec["unit"], "source_type": "SIMULATED"})
    return items


def metrics_cards_html(state):
    """渲染指标卡片 HTML(服务端注入初始内容;页面 render() 首载会按同一结构重建,
    状态类由页面 JS 单一阈值事实源注入,服务端不注入避免双源漂移)。"""
    icons = {"温度": '<svg viewBox="0 0 20 20" fill="none"><path d="M10 3a2 2 0 0 0-2 2v7.2a4 4 0 1 0 4 0V5a2 2 0 0 0-2-2Z" stroke="currentColor" stroke-width="1.5"/><circle cx="10" cy="14.5" r="1.8" fill="currentColor"/></svg>',
             "湿度": '<svg viewBox="0 0 20 20" fill="none"><path d="M10 3S4.5 9.2 4.5 13a5.5 5.5 0 0 0 11 0C15.5 9.2 10 3 10 3Z" stroke="currentColor" stroke-width="1.5"/></svg>',
             "CO₂浓度": '<svg viewBox="0 0 20 20" fill="none"><circle cx="7" cy="11" r="2.2" stroke="currentColor" stroke-width="1.5"/><circle cx="11.5" cy="8.5" r="1.5" stroke="currentColor" stroke-width="1.5"/><circle cx="11.5" cy="13.5" r="2.8" stroke="currentColor" stroke-width="1.5"/></svg>',
             "光照": '<svg viewBox="0 0 20 20" fill="none"><circle cx="10" cy="10" r="3.2" stroke="currentColor" stroke-width="1.5"/><path d="M10 1.5v2M10 16.5v2M1.5 10h2M16.5 10h2M4 4l1.4 1.4M14.6 14.6 16 16M16 4l-1.4 1.4M5.4 14.6 4 16" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>'}
    cards = []
    for item in build_metrics(state):
        cards.append(
            '<div class="metric-card"><div class="metric-head">%s<span>%s</span></div>'
            '<div class="metric-value">%s<span class="metric-unit"> %s</span></div>'
            '<div class="metric-tip ok">适宜</div>'
            '<span class="sim-badge">source_type=%s</span></div>'
            % (icons.get(item["name"], ""), html.escape(item["name"]), html.escape(item["value"]),
               html.escape(item["unit"]), html.escape(item["source_type"])))
    return "".join(cards)


def collect_form_html():
    """按 PARAM_SPECS 生成新建采集表单字段(服务端注入,范围文案与 CLI 校验一致)。"""
    icons = {"temperature": '<svg viewBox="0 0 16 16" fill="none"><path d="M8 2a1.6 1.6 0 0 0-1.6 1.6v6a3.4 3.4 0 1 0 3.2 0v-6A1.6 1.6 0 0 0 8 2Z" stroke="currentColor" stroke-width="1.2"/><circle cx="8" cy="11.8" r="1.4" fill="currentColor"/></svg>',
             "humidity": '<svg viewBox="0 0 16 16" fill="none"><path d="M8 2S3.6 7 3.6 10.2a4.4 4.4 0 0 0 8.8 0C12.4 7 8 2 8 2Z" stroke="currentColor" stroke-width="1.2"/></svg>',
             "co2": '<svg viewBox="0 0 16 16" fill="none"><circle cx="5.5" cy="9" r="1.8" stroke="currentColor" stroke-width="1.2"/><circle cx="9" cy="7" r="1.2" stroke="currentColor" stroke-width="1.2"/><circle cx="9" cy="11" r="2.2" stroke="currentColor" stroke-width="1.2"/></svg>',
             "light": '<svg viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="2.6" stroke="currentColor" stroke-width="1.2"/><path d="M8 1v1.6M8 13.4V15M1 8h1.6M13.4 8H15M3.2 3.2l1.1 1.1M11.7 11.7l1.1 1.1M12.8 3.2l-1.1 1.1M4.3 11.7l-1.1 1.1" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>'}
    rows = []
    for spec in PARAM_SPECS:
        rows.append(
            '<div class="collect-row">'
            '<label class="collect-label" for="in-%s">%s%s</label>'
            '<input type="text" id="in-%s" name="%s" inputmode="decimal" placeholder="合法范围 %s">'
            '<span class="collect-unit">%s</span>'
            '<span class="collect-err hidden" id="err-%s"></span>'
            '</div>' % (spec["key"], icons.get(spec["key"], ""), html.escape(spec["name"]),
                        spec["key"], spec["key"], html.escape(range_text(spec)),
                        html.escape(spec["unit"]), spec["key"]))
    return "".join(rows)


def llm_status():
    """LLM 可用性摘要(只含布尔标记,绝不含密钥): key_configured=True 表示已配置任一 API Key。"""
    key, _source = get_api_key()
    return {"key_configured": key is not None}


def read_recent_history(limit=HISTORY_WEB_LIMIT):
    """读取 evidence/raw 下最近 limit 条 run_*.json(文件名倒序 = 时间倒序,与 show_history 一致)。
    返回 (records, skipped): 单条文件损坏/缺字段仅计数跳过,不崩溃;
    条目为 {run_id, collect_time, source_type, temperature, humidity, co2, light},
    数值缺失为 None(页面显示 "—"),早期记录可能无 source_type(显示 "—")。"""
    files = sorted(RAW_DIR.glob("run_*.json"), reverse=True)[:limit]  # 先排序再截断,取真正最新的 limit 条
    records, skipped = [], 0
    for path in files:
        try:
            with open(path, encoding="utf-8") as f:
                record = json.load(f)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            skipped += 1  # 并发写入读到半截文件或损坏文件:跳过不崩溃
            continue
        if not isinstance(record, dict) or not isinstance(record.get("data"), dict):
            skipped += 1
            continue
        data = record["data"]
        item = {
            "run_id": record.get("run_id") or path.stem,
            "collect_time": record.get("collect_time") or "",
            "source_type": record.get("source_type") or "",
        }
        for spec in PARAM_SPECS:
            entry = data.get(spec["key"])
            value = entry.get("value") if isinstance(entry, dict) else None
            item[spec["key"]] = value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        records.append(item)
    return records, skipped


def build_page():
    """生成当前状态对应的页面 HTML。"""
    return (PAGE_HTML
            .replace("__STATE__", "%s(%s)" % (CURRENT_STATE, STATE_NAMES[CURRENT_STATE]))
            .replace("__METRICS__", metrics_cards_html(STATE_DEFS[CURRENT_STATE]))
            .replace("__COLLECT_FORM__", collect_form_html())
            .replace("__HISTORY_LIMIT__", str(HISTORY_WEB_LIMIT)))


def _json_response(handler, code, obj):
    """写 JSON 响应(UTF-8,长度按字节计算)。"""
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class B01RequestHandler(BaseHTTPRequestHandler):
    """B01 补全 Web 服务: 页面 / 状态 / 切换 / 提问 / 新建采集 / 历史记录。任何异常均以 JSON 如实返回,零 traceback。"""
    server_version = "GreenMateB01/1.0"
    protocol_version = "HTTP/1.1"   # keep-alive:全部 200 响应路径均显式 Content-Length(已核实)

    def log_message(self, format, *args):  # 请求日志统一由 log_llm 记录,关闭默认 stderr 噪音
        pass

    def do_GET(self):
        try:
            path = urlparse(self.path).path
            if path == "/":
                page = build_page().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
                return
            if path == "/api/state":
                state = STATE_DEFS[CURRENT_STATE]
                _json_response(self, 200, {"status": "ok", "state": CURRENT_STATE,
                                           "metrics": {"source_type": "SIMULATED",
                                                       "items": build_metrics(state)},
                                           "llm": llm_status()})
                return
            if path == "/api/history":
                records, skipped = read_recent_history()
                _json_response(self, 200, {"status": "ok", "count": len(records),
                                           "skipped": skipped, "records": records})
                return
            if path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            _json_response(self, 404, {"status": "error", "error_type": "not_found",
                                       "message": "404 未找到: %s" % path})
        except Exception as e:
            log_llm("[失败] web error_type=server_error message=%s" % str(e)[:300])
            try:
                _json_response(self, 500, {"status": "error", "error_type": "server_error",
                                           "message": str(e)[:300]})
            except Exception:
                pass

    def do_POST(self):
        try:
            path = urlparse(self.path).path
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
            except ValueError:
                length = 0
            if length > 65536:
                _json_response(self, 413, {"status": "error", "error_type": "body_too_large",
                                           "message": "请求体过大(上限 65536 字节)"})
                return
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8")) if raw.strip() else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                _json_response(self, 400, {"status": "error", "error_type": "bad_json",
                                           "message": "请求体不是合法 JSON"})
                return
            if path == "/api/collect":
                if not isinstance(payload, dict):
                    _json_response(self, 400, {"status": "error", "error_type": "bad_json",
                                               "message": "请求体必须是 JSON 对象"})
                    return
                field_errors, values = validate_web_collect(payload)
                if field_errors:
                    _json_response(self, 400, {"status": "error", "error_type": "invalid_params",
                                               "message": "部分参数不合法,请按提示修正后重新提交",
                                               "field_errors": field_errors})
                    return
                record, saved_path = None, None
                for _attempt in range(3):  # 保存失败或撞名时换新 run_id 重试(gen_run_id 自带存在性循环)
                    record = build_record_from_values(values)
                    if save_record(record):
                        saved_path = str(RAW_DIR / ("%s.json" % record["run_id"]))
                        break
                if saved_path is None:
                    _json_response(self, 500, {"status": "error", "error_type": "save_failed",
                                               "message": "原始数据保存失败(evidence/raw 目录不可写?),本次采集未保存"})
                    return
                log_llm("[采集] run_id=%s 温度=%g 湿度=%g co2=%g 光照=%g"
                        % (record["run_id"], values["temperature"], values["humidity"],
                           values["co2"], values["light"]))
                _json_response(self, 200, {"status": "ok", "record": record,
                                           "saved_path": saved_path})
                return
            if path == "/api/switch":
                new_state = payload.get("state") if isinstance(payload, dict) else None
                if new_state not in STATE_DEFS:
                    _json_response(self, 400, {"status": "error", "error_type": "bad_state",
                                               "message": "state 必须为 A 或 B"})
                    return
                global CURRENT_STATE
                old = CURRENT_STATE
                CURRENT_STATE = new_state
                log_llm("[切换] state=%s -> %s" % (old, new_state))
                state = STATE_DEFS[CURRENT_STATE]
                _json_response(self, 200, {"status": "ok", "state": CURRENT_STATE,
                                           "metrics": {"source_type": "SIMULATED",
                                                       "items": build_metrics(state)},
                                           "llm": llm_status()})
                return
            if path == "/api/ask":
                state = STATE_DEFS[CURRENT_STATE]
                log_llm("[请求] state=%s 问句=%s 环境=%s"
                        % (CURRENT_STATE, FIXED_QUESTION, " / ".join(format_state(state))))
                result = ask_llm(state, FIXED_QUESTION)
                if result["status"] == "success":
                    log_llm("[成功] state=%s answer=%s" % (CURRENT_STATE, result["answer"][:200]))
                else:
                    log_llm("[失败] state=%s error_type=%s message=%s"
                            % (CURRENT_STATE, result["error_type"], result["message"]))
                _json_response(self, 200, result)  # 恒 200,内层 status 字段才是成败信号
                return
            _json_response(self, 404, {"status": "error", "error_type": "not_found",
                                       "message": "404 未找到: %s" % path})
        except Exception as e:
            log_llm("[失败] web error_type=server_error message=%s" % str(e)[:300])
            try:
                _json_response(self, 500, {"status": "error", "error_type": "server_error",
                                           "message": str(e)[:300]})
            except Exception:
                pass


def run_web_server(port=None):
    """启动 B01 补全 Web 页面服务(127.0.0.1,端口默认 B01_WEB_PORT 可被 port 形参覆盖)。
    port 形参供 b09_portal 线程同起复用;端口占用时打印提示并返回(不 sys.exit,
    避免子线程/重复启动静默退出无反馈)。"""
    listen_port = port if port is not None else B01_WEB_PORT
    setup_console()
    print("================ GreenMate B01 环境监测 Web 展示 ================")
    print("页面地址: http://%s:%d" % (B01_WEB_HOST, listen_port))
    print("当前状态: %s (%s)   固定提问: %s" % (CURRENT_STATE, STATE_NAMES[CURRENT_STATE], FIXED_QUESTION))
    print("环境数据标记: source_type=SIMULATED")
    print("Web 能力: A/B 切换 / LLM 问答 / 新建采集(服务端同规则校验) / 最近 %d 条历史" % HISTORY_WEB_LIMIT)
    print("按 Ctrl+C 停止服务。")
    try:
        server = ThreadingHTTPServer((B01_WEB_HOST, listen_port), B01RequestHandler)
        server.daemon_threads = True
    except OSError as e:
        print("⚠ 端口 %d 启动失败(可能已被占用): %s。请设置 B01_WEB_PORT 环境变量换一个端口。" % (listen_port, e))
        return
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
    finally:
        server.server_close()
        sys.exit(0)


# ---------------- B01 补全: A/B 验证 ----------------
def _normalize(text):
    """归一化回答文本(合并空白 + 去尾部标点),用于 A/B 对比。"""
    return " ".join(str(text).split()).rstrip("。.! !? ?")


def run_abtest():
    """A/B 验证: 两套环境状态、只切换状态、固定提问,校验 LLM 回答是否跟随状态变化。
    退出码恒为 0,判定结果与完整回答见控制台与 logs/b01_llm.log。"""
    setup_console()
    print("================ B01 A/B 验证 ================")
    print("固定提问: %s" % FIXED_QUESTION)
    print("状态 A(%s): %s" % (STATE_NAMES["A"], " / ".join(format_state(STATE_A))))
    print("状态 B(%s): %s" % (STATE_NAMES["B"], " / ".join(format_state(STATE_B))))
    print("环境数据标记: source_type=SIMULATED")
    results = {}
    for key in ("A", "B"):
        state = STATE_DEFS[key]
        log_llm("[请求] state=%s 问句=%s 环境=%s" % (key, FIXED_QUESTION, " / ".join(format_state(state))))
        result = ask_llm(state, FIXED_QUESTION)
        results[key] = result
        if result["status"] == "success":
            log_llm("[成功] state=%s answer=%s" % (key, result["answer"][:200]))
            print("\n[状态 %s · %s] 回答: %s" % (key, STATE_NAMES[key], result["answer"][:300]))
        else:
            log_llm("[失败] state=%s error_type=%s message=%s"
                    % (key, result["error_type"], result["message"]))
            print("\n[状态 %s · %s] 失败: [%s] %s"
                  % (key, STATE_NAMES[key], result["error_type"], result["message"]))
    print("\n---------------- A/B 判定 ----------------")
    ra, rb = results["A"], results["B"]
    failed = [k for k in ("A", "B") if results[k]["status"] != "success"]
    if failed:
        k = failed[0]
        print("FAIL — 状态 %s 调用失败 (error_type=%s, message=%s)"
              % (k, results[k]["error_type"], results[k]["message"]))
    elif _normalize(ra["answer"]) != _normalize(rb["answer"]):
        print("PASS — LLM 回答跟随状态变化(两次回答内容不同)")
        low_b = " ".join(rb["answer"].lower().split())
        keywords = [w for w in ("异常", "过高", "偏高", "预警", "警报", "危险", "告警") if w in low_b]
        if keywords:
            print("观察(仅供参考,不计入判定): 回答 B 出现异常相关关键词: %s" % "、".join(keywords))
        else:
            print("观察(仅供参考,不计入判定): 回答 B 未出现异常相关关键词")
    else:
        print("FAIL — 两状态回答内容一致,LLM 未跟随状态变化")
    print("完整回答见 logs/b01_llm.log")


def show_history():
    """查看历史 run_id 列表:读取 evidence/raw 下 run_*.json 文件名,按时间倒序展示。"""
    files = sorted(RAW_DIR.glob("run_*.json"), reverse=True)  # 时间戳定宽,字典序即时间序
    if not files:
        print("\n暂无历史采集记录,请先选择菜单 ① 新建采集。")
        return

    print("\n---------------- 历史 run_id 列表 ----------------")
    print(f"共 {len(files)} 条采集记录 (按时间倒序):")
    skipped = 0
    for i, path in enumerate(files, start=1):
        try:
            time_text = datetime.strptime(path.stem[4:19], "%Y%m%d_%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            skipped += 1
            continue
        print(f"  {i}. {path.stem}  ({time_text})")
    if skipped:
        print(f"(另有 {skipped} 个文件名格式不符的文件被忽略)")


def show_menu():
    """打印主菜单。"""
    print("\n================ GreenMate B01 主菜单 ================")
    print("  ① 新建一组采集数据")
    print("  ② 查看历史 run_id 列表")
    print("  ③ 退出程序")


def usage():
    """打印 B01 用法说明(采集菜单 / Web 展示 / A-B 验证)。"""
    print("用法:")
    print("  py b01_cli.py             # 进入采集主菜单(原始功能)")
    print("  py b01_cli.py web         # 启动 Web 页面(A/B 切换 + LLM 问答 + 新建采集 + 历史记录)")
    print("  py b01_cli.py abtest      # 运行 A/B 验证(两套环境状态 + 固定提问)")
    print("  py b01_cli.py --help      # 查看本说明")
    print("环境变量: B01_LLM_BASE_URL / B01_LLM_MODEL / B01_LLM_TIMEOUT / B01_WEB_PORT")
    print("          API Key 读取顺序: DEEPSEEK_API_KEY → ANTHROPIC_AUTH_TOKEN")


def main():
    """程序入口: 无参数进入采集主菜单;web 启动页面服务;abtest 运行 A/B 验证。
    主菜单循环中 Ctrl+C 与输入流结束(EOF)均友好退出,不抛 traceback。"""
    setup_console()
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help", "help"):
        usage()
        return
    if args and args[0] == "web":
        run_web_server()
        return
    if args and args[0] == "abtest":
        run_abtest()
        return
    if args:
        print("未知参数: %s" % " ".join(args))
        usage()
        sys.exit(1)
    print("欢迎使用 GreenMate B01 智能交互模块 (番茄大棚环境数据采集)")
    try:
        while True:
            show_menu()
            choice = input("请选择 (1/2/3): ").strip()
            if choice == "1":
                record = collect_one_run()
                if record is not None:
                    save_record(record)
            elif choice == "2":
                show_history()
            elif choice == "3":
                print("感谢使用 GreenMate B01 智能交互模块,再见!")
                break
            else:
                print("无效选项:请输入 1、2 或 3。")
    except KeyboardInterrupt:
        print("\n检测到 Ctrl+C,程序已安全退出。")
        sys.exit(0)
    except EOFError:
        print("\n输入已结束 (EOF),程序退出。")
        sys.exit(0)


if __name__ == "__main__":
    main()
