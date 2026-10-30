#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 GreenMate B05 — 工具智能体闭环模块(LLM 工具调用 + 真实执行)
=====================================================================

【任务目标】
  实现工具智能体闭环:LLM 输出 tool_call(只负责决定调用哪个工具、参数是什么),
  Python 程序真实执行 B02/B03/B04 已写好的 4 个工具函数,拿到工具结果返回 LLM
  继续判断,形成 调用→执行→返回→继续 闭环,直到 LLM 输出最终回答。
  每次调用强制记录 tool 名称、arguments、tool_result、sequence(没有真实执行、
  没有日志,不算完成 Tool Agent)。

【四个标准工具(B02/B03/B04 真实函数)】
  get_current_environment()          # B02 当前环境状态(库内最新样本)
  get_environment_history(hours=24)  # B02 最近 N 小时历史时序
  get_weather_forecast(location)     # B03 真实天气 API 证据
  search_tomato_knowledge(query)     # B04 番茄知识库检索

【运行方式】
  py b05_agent.py run <任务文本>      # 完整闭环:LLM 决策 → 工具执行 → 最终回答
  py b05_agent.py tools              # 查看工具定义(TOOL_DEFINITIONS)
  py b05_agent.py test               # 测试用例(2 单工具 + 2 多工具,需 API Key)
  py b05_agent.py failtest           # 执行层失败验证(参数错误/空结果/超时,零 LLM)
  py b05_agent.py --help             # 查看本说明

【分支逻辑(LLM 在收到工具结果后判断)】
  证据充足 → 直接输出最终回答;证据不足 → 继续调用其他工具;
  工具失败或返回 error/insufficient 证据 → 最终回答中标注【证据缺失】降级,不编造。

【业务边界】
  LLM 只做决策,真实查询/API 请求全部由 Python 代码执行;MCP 外部工具连接为
  可选扩展,本模块基础实现不包含(见 文档/b05_agent.md 留白说明)。
=====================================================================
"""

import hashlib
import json
import os
import re
import secrets
import sys
from datetime import datetime
from pathlib import Path

# B05 是规格指定的组装者:闭环要求在进程内调用 B02/B03/B04 的真实函数,
# 旧的"模块互不 import、只读对方证据文件"约定(b05_fusion 时代)在此不再适用。
import b02_memory
import b03_weather
import b04_rag

# ---------------- 配置 ----------------
BASE_DIR = Path(__file__).resolve().parent
EVIDENCE_DIR = BASE_DIR / "证据" / "b05"
LOG_DIR = BASE_DIR / "日志"
AGENT_LOG = LOG_DIR / "b05_agent.log"

# LLM 配置(b01 同款约定:全部可被环境变量覆盖,代码中零硬编码 API Key)
B05_LLM_BASE_URL = os.environ.get("B05_LLM_BASE_URL", "https://api.deepseek.com/chat/completions")
B05_LLM_MODEL = os.environ.get("B05_LLM_MODEL", "deepseek-chat")
try:
    B05_LLM_TIMEOUT = float(os.environ.get("B05_LLM_TIMEOUT", "30"))
except ValueError:
    B05_LLM_TIMEOUT = 30.0
try:
    B05_MAX_STEPS = max(1, int(os.environ.get("B05_MAX_STEPS", "6")))
except ValueError:
    B05_MAX_STEPS = 6

TOOL_RESULT_MAX_CHARS = 4000   # 回传 LLM 的工具结果截断上限(历史时序较大;截断会标注)


def setup_console():
    """强制标准输出为 UTF-8:GBK 管道打印 ℃/CO₂ 会崩溃(本项目已多次实测)。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def _now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _log_agent(text):
    """追加一行时间戳日志到 日志/b05_agent.log 并镜像控制台;绝不记录密钥。
    库级路径(B07 import 调用)不会先走 main 的 setup_console,必须在此兜底。"""
    setup_console()
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), text)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(AGENT_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line)


def _save_evidence(obj, filename):
    try:
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        with open(EVIDENCE_DIR / filename, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        return True
    except OSError as e:
        print("⚠ 证据保存失败: %s" % e)
        return False


# ---------------- 工具定义(交付物:TOOL_DEFINITIONS)----------------
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_environment",
            "description": ("查询当前大棚环境状态:数据库内最新一条模拟样本,返回 sample_time/source_type/"
                            "temperature/humidity/co2/light 6 项字段。数据为课程模拟(SIMULATED),"
                            "回答中须如实说明;库为空时返回 null。"),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_environment_history",
            "description": ("查询最近 N 小时的大棚环境历史时序数据(升序,窗口锚定库内最新样本时间而非墙钟),"
                            "用于趋势与扰动分析;空库返回空列表。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "maximum": 8760,
                        "description": "最近 N 小时,必须大于 0;不传时默认 24",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather_forecast",
            "description": ("查询指定地点的真实天气预报证据(Open-Meteo 公开 API),返回 provider/retrieved_at/"
                            "analysis_time/forecast_time/温度/湿度/降水概率/云量/辐射字段。"
                            "服务出错时返回 status=error 的 error 证据(含 error_type/message),禁止伪造成功;"
                            "收到 error 证据时按【证据缺失】降级处理。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "地点:内置城市(北京/上海/广州)或 \"纬度,经度\" 格式,如 \"39.9,116.4\"",
                    },
                    "horizon_hours": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "description": "可选:预测时间窗口(小时);不传时取首个晚于分析时刻的预报步长",
                    },
                },
                "required": ["location"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_tomato_knowledge",
            "description": ("检索番茄种植知识库,返回相关资料片段(含来源引用 citation)。"
                            "资料不覆盖时返回 status=insufficient 的 insufficient 证据,"
                            "不得编造知识;收到 insufficient 时按【资料不足/无法确认】降级处理。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索问题,如\"番茄适宜生长温度是多少\""},
                    "k": {"type": "integer", "minimum": 1, "maximum": 20,
                          "description": "可选:返回片段数,默认 5"},
                },
                "required": ["query"],
            },
        },
    },
]

# 白名单调度表:工具名 -> B02/B03/B04 的真实函数
TOOL_DISPATCHERS = {
    "get_current_environment": b02_memory.get_current_environment,
    "get_environment_history": b02_memory.get_environment_history,
    "get_weather_forecast": b03_weather.get_weather_forecast,
    "search_tomato_knowledge": b04_rag.search_tomato_knowledge,
}

# 调度器级参数默认值(b02 的 hours 无默认参数,LLM 省略时由调度器注入,与工具描述一致)
DEFAULT_ARGUMENTS = {"get_environment_history": {"hours": 24}}


def dispatch_tool(name, arguments, seq=None, step=None):
    """白名单调度:真实执行工具函数,返回统一 tool_result,永不抛异常。
    成功: {"status":"ok","result":<工具原始返回值>} —— weather/knowledge 的嵌套
    status 如实穿透(调度层 ok,LLM 读嵌套状态做分支判断)。
    失败: {"status":"error","error_type":...,"message":...}。
    每次调度(成功或失败)必写 [工具调用] 日志行 —— 没有日志不算执行。"""
    seq_text = str(seq) if seq is not None else "-"
    step_text = str(step) if step is not None else "-"
    if name not in TOOL_DISPATCHERS:
        result = {"status": "error", "error_type": "unknown_tool",
                  "message": "未知工具: %s(白名单外,拒绝执行)" % name}
        _log_agent("[工具调用] seq=%s step=%s tool=%s arguments=%s result_status=error" % (
            seq_text, step_text, name, json.dumps(arguments, ensure_ascii=False)[:200]))
        _log_agent("[工具失败] seq=%s tool=%s error_type=%s message=%s" % (
            seq_text, name, result["error_type"], result["message"][:120]))
        return result
    if not isinstance(arguments, dict):
        result = {"status": "error", "error_type": "bad_arguments",
                  "message": "arguments 必须是 JSON 对象,收到: %s" % str(arguments)[:120]}
        _log_agent("[工具调用] seq=%s step=%s tool=%s arguments=%s result_status=error" % (
            seq_text, step_text, name, str(arguments)[:200]))
        _log_agent("[工具失败] seq=%s tool=%s error_type=%s message=%s" % (
            seq_text, name, result["error_type"], result["message"][:120]))
        return result
    merged = dict(DEFAULT_ARGUMENTS.get(name, {}))
    merged.update(arguments)
    func = TOOL_DISPATCHERS[name]
    try:
        raw = func(**merged)
    except TypeError as e:
        result = {"status": "error", "error_type": "bad_arguments",
                  "message": "参数类型错误: %s" % str(e)[:120]}
    except ValueError as e:
        result = {"status": "error", "error_type": "value_error",
                  "message": "参数取值非法: %s" % str(e)[:120]}
    except Exception as e:
        result = {"status": "error", "error_type": "tool_exception",
                  "message": "工具执行异常: %s: %s" % (type(e).__name__, str(e)[:120])}
    else:
        result = {"status": "ok", "result": raw}
    nested = ""
    if result["status"] == "ok" and isinstance(raw, dict) and "status" in raw:
        nested = " nested=%s" % raw["status"]
    _log_agent("[工具调用] seq=%s step=%s tool=%s arguments=%s result_status=%s%s" % (
        seq_text, step_text, name, json.dumps(merged, ensure_ascii=False)[:200],
        result["status"], nested))
    if result["status"] == "error":
        _log_agent("[工具失败] seq=%s tool=%s error_type=%s message=%s" % (
            seq_text, name, result["error_type"], result["message"][:120]))
    return result


# ---------------- LLM 层 ----------------
SYSTEM_PROMPT = (
    "你是 GreenMate 番茄大棚环境监测的工具智能体。你的职责是:根据用户任务,"
    "决定调用哪个工具与传什么参数;工具由程序真实执行,你只能阅读工具返回的结果,"
    "不得编造任何工具数据。\n"
    "规则:\n"
    "1. 你只输出两种东西——工具调用(tool_calls)或最终回答(纯文本内容)。需要数据时先调用工具,不要猜测。\n"
    "2. 每次收到工具结果后判断:证据已充分 → 直接输出最终回答,不再调用工具;"
    "证据不足 → 继续调用合适的其他工具补充。\n"
    "3. 某工具失败或返回 error/insufficient 证据时,不得编造数据补全;"
    "在最终回答中明确标注该项证据缺失或降级(如【证据缺失】),并说明原因。\n"
    "4. 最终回答必须用中文,给出结论并说明依据(引用了哪些工具返回的数据)。\n"
    "5. 环境数据为课程模拟数据(SIMULATED),回答中如实说明,不得声称来自真实传感器;"
    "天气数据来自真实公开 API。\n"
    "6. 不得输出与任务无关的调用;每次调用工具前思考它是否是回答任务所必需。"
)


def _get_api_key():
    """读取 LLM API Key: DEEPSEEK_API_KEY 优先,回退 ANTHROPIC_AUTH_TOKEN;绝不打印 key 值。"""
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        return key, "env:DEEPSEEK_API_KEY"
    key = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if key:
        return key, "env:ANTHROPIC_AUTH_TOKEN"
    return None, None


def _call_llm_with_tools(messages):
    """调用 LLM(tools 负载,tool_choice=auto)。返回 (message_dict|None, error_dict|None)。
    成功路径只有解析真实返回一条;失败如实上报。"""
    key, _key_source = _get_api_key()
    if key is None:
        return None, {"error_type": "no_api_key",
                      "message": "未配置 API Key(需设置环境变量 DEEPSEEK_API_KEY 或 ANTHROPIC_AUTH_TOKEN)"}
    try:
        import requests   # 惰性导入:failtest 等执行层路径无需 requests
    except ImportError:
        return None, {"error_type": "missing_dependency",
                      "message": "未安装 requests 库,无法调用 LLM(可执行 pip install requests)"}
    headers = {"Authorization": "Bearer %s" % key, "Content-Type": "application/json"}
    payload = {"model": B05_LLM_MODEL, "messages": messages, "stream": False,
               "tools": TOOL_DEFINITIONS, "tool_choice": "auto"}
    try:
        resp = requests.post(B05_LLM_BASE_URL, json=payload, headers=headers,
                             timeout=(B05_LLM_TIMEOUT, B05_LLM_TIMEOUT))
    except requests.exceptions.ConnectTimeout:
        return None, {"error_type": "network_timeout",
                      "message": "连接超时(超过 %g 秒未建立连接),请检查网络或 B05_LLM_TIMEOUT" % B05_LLM_TIMEOUT}
    except requests.exceptions.ReadTimeout:
        return None, {"error_type": "network_timeout",
                      "message": "读取超时(服务器 %g 秒内未返回结果)" % B05_LLM_TIMEOUT}
    except requests.exceptions.ConnectionError:
        return None, {"error_type": "network_timeout",
                      "message": "网络连接失败,无法访问 %s" % B05_LLM_BASE_URL}
    except Exception as e:
        return None, {"error_type": "network_timeout", "message": "网络异常: %s" % e}
    if resp.status_code in (401, 403):
        return None, {"error_type": "api_key_error",
                      "message": "HTTP %d 认证失败,API Key 无效或已过期" % resp.status_code}
    if resp.status_code != 200:
        return None, {"error_type": "http_error",
                      "message": "HTTP %d: %s" % (resp.status_code, resp.text[:200])}
    try:
        message = resp.json()["choices"][0]["message"]
    except Exception:
        return None, {"error_type": "bad_response",
                      "message": "API 返回内容无法解析(缺少 choices[0].message): %s" % resp.text[:300]}
    if not isinstance(message, dict):
        return None, {"error_type": "bad_response",
                      "message": "choices[0].message 不是对象: %s" % str(message)[:300]}
    return message, None


# ---------------- 闭环调度(官方契约,供 B07 复用)----------------
def run_agent(task, max_steps=None):
    """工具智能体闭环:LLM 决策 → dispatch_tool 真实执行 → 结果回传 → 继续,直到最终回答。
    恒返回 agent_evidence,三种结局(success/error/stopped)均落盘完整调用链。"""
    max_steps = max_steps or B05_MAX_STEPS
    ev = {
        "task": str(task), "started_at": _now_iso(), "ended_at": None,
        "model": B05_LLM_MODEL, "max_steps": max_steps, "status": "error",
        "final_answer": None, "steps": 0, "tool_calls": [], "tool_failures": [], "error": None,
    }
    key, _key_source = _get_api_key()
    if key is None:
        ev["error"] = {"error_type": "no_api_key",
                       "message": "未配置 API Key(需设置环境变量 DEEPSEEK_API_KEY 或 ANTHROPIC_AUTH_TOKEN)"}
        _log_agent("[失败] step=0 error_type=no_api_key 未配置 API Key")
        return _finish_agent(ev)

    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": str(task)}]
    seq = 0
    for step in range(1, max_steps + 1):
        ev["steps"] = step
        _log_agent("[请求] step=%d messages=%d tools=%d" % (step, len(messages), len(TOOL_DEFINITIONS)))
        message, err = _call_llm_with_tools(messages)
        if err is not None:
            ev["error"] = err
            _log_agent("[失败] step=%d error_type=%s message=%s" % (step, err["error_type"], err["message"][:160]))
            break
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            # content 与 tool_calls 并存时 tool_calls 优先,content 原样保留在 assistant 消息中
            messages.append({"role": "assistant", "content": message.get("content"), "tool_calls": tool_calls})
            for tc in tool_calls:
                seq += 1
                fspec = tc.get("function") or {}
                name = fspec.get("name")
                raw_args = fspec.get("arguments")
                args, args_raw = _parse_arguments(raw_args)
                if args is None:
                    tool_result = {"status": "error", "error_type": "bad_arguments",
                                   "message": "arguments 不是合法 JSON: %s" % str(raw_args)[:120]}
                else:
                    tool_result = dispatch_tool(name, args, seq=seq, step=step)
                rec = {"sequence": seq, "timestamp": _now_iso(), "step": step,
                       "tool": name, "arguments": args, "tool_result": tool_result}
                if args is None:
                    rec["arguments_raw"] = str(raw_args)[:200]
                ev["tool_calls"].append(rec)
                if tool_result["status"] == "error":
                    ev["tool_failures"].append({"sequence": seq, "tool": name,
                                                "error_type": tool_result["error_type"],
                                                "message": tool_result["message"]})
                content = json.dumps(tool_result, ensure_ascii=False)
                if len(content) > TOOL_RESULT_MAX_CHARS:
                    content = content[:TOOL_RESULT_MAX_CHARS] + "…(结果已截断)"
                messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": content})
            continue   # 结果回传,LLM 继续判断
        content = message.get("content")
        if content:
            ev["status"] = "success"
            ev["final_answer"] = str(content)
            _log_agent("[成功] step=%d final_answer_len=%d" % (step, len(str(content))))
            break
        ev["error"] = {"error_type": "bad_response",
                       "message": "LLM 返回既无 tool_calls 也无 content"}
        _log_agent("[失败] step=%d error_type=bad_response 响应既无 tool_calls 也无 content" % step)
        break
    else:
        ev["status"] = "stopped"
        _log_agent("[停止] step=%d 达到 max_steps=%d,无最终回答(调用链已保留)" % (ev["steps"], max_steps))
    return _finish_agent(ev)


def _parse_arguments(raw):
    """tool_call.arguments 解析:dict 原样 / JSON 字符串;失败返回 (None, 原文)。"""
    if isinstance(raw, dict):
        return raw, None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None, raw
        return parsed if isinstance(parsed, dict) else None, raw
    return None, raw


def _finish_agent(ev):
    ev["ended_at"] = _now_iso()
    filename = "agent_run_%s_%s.json" % (datetime.now().strftime("%Y%m%d_%H%M%S"), secrets.token_hex(3))
    _save_evidence(ev, filename)
    _log_agent("[最终] status=%s steps=%d tool_calls=%d tool_failures=%d evidence=证据/b05/%s" % (
        ev["status"], ev["steps"], len(ev["tool_calls"]), len(ev["tool_failures"]), filename))
    return ev


# ---------------- CLI ----------------
def _join_query(args):
    parts = [a for a in args if not a.startswith("-")]
    return " ".join(parts) if parts else None


def _print_call_table(ev):
    if ev["tool_calls"]:
        print("\n---------------- 工具调用链(强制日志记录)----------------")
        print("%-4s %-6s %-24s %-46s %-10s" % ("seq", "step", "tool", "arguments", "结果"))
        for rec in ev["tool_calls"]:
            args_text = json.dumps(rec["arguments"], ensure_ascii=False) if rec["arguments"] is not None \
                else (rec.get("arguments_raw") or "-")
            result_status = rec["tool_result"]["status"]
            nested = ""
            r = rec["tool_result"].get("result")
            if isinstance(r, dict) and "status" in r:
                nested = "/%s" % r["status"]
            print("%-4d %-6d %-24s %-46s %-10s" % (
                rec["sequence"], rec["step"], rec["tool"] or "?", args_text[:46], result_status + nested))


def cmd_run(args):
    task = _join_query(args)
    if not task:
        print("⚠ 请提供任务文本,如: py b05_agent.py run 查询当前大棚环境状态")
        return
    print("================ B05 工具智能体闭环 ================")
    print("任务: %s" % task)
    ev = run_agent(task)
    _print_call_table(ev)
    print("\n---------------- 智能体结果 ----------------")
    if ev["status"] == "success":
        print("  状态: 成功(steps=%d,工具调用 %d 次)" % (ev["steps"], len(ev["tool_calls"])))
        print("  最终回答:\n%s" % ev["final_answer"])
    elif ev["status"] == "stopped":
        print("  ⚠ 状态: 步数耗尽(max_steps=%d),未得到最终回答;调用链已保留" % ev["max_steps"])
    else:
        err = ev["error"] or {}
        print("  ⚠ 状态: 失败 error_type=%s | %s(调用链已保留)" % (err.get("error_type"), err.get("message")))
    if ev["tool_failures"]:
        print("  工具失败记录:")
        for f in ev["tool_failures"]:
            print("    seq=%d %s: %s(%s)" % (f["sequence"], f["tool"], f["error_type"], f["message"][:100]))


def cmd_tools():
    print("================ B05 工具定义(TOOL_DEFINITIONS)================")
    for i, tool in enumerate(TOOL_DEFINITIONS, start=1):
        fn = tool["function"]
        print("\n[%d] %s" % (i, fn["name"]))
        print("    描述: %s" % fn["description"])
        print("    参数: %s" % json.dumps(fn["parameters"], ensure_ascii=False))
    print("\n调度白名单: %s" % "、".join(TOOL_DISPATCHERS))


TEST_CASES = (
    ("c1", "single", "查询当前大棚环境状态,并总结温度、湿度、CO₂ 与光照情况",
     ("get_current_environment",)),
    ("c2", "single", "查询北京的天气预报并总结未来天气",
     ("get_weather_forecast",)),
    ("c3", "multi", "分析当前大棚环境状态并给出管理建议,需要时查阅番茄种植知识库",
     ("get_current_environment",)),
    ("c4", "multi", "结合最近 24 小时环境历史与北京天气预报,评估大棚是否需要干预,不确定时查阅知识库",
     ("get_environment_history", "get_weather_forecast")),
)


def _check_tool_shapes(ev, problems):
    """真实结果形状证明:每种工具的结果必须来自真实执行。"""
    for rec in ev["tool_calls"]:
        tr = rec["tool_result"]
        if tr["status"] != "ok":
            continue
        r = tr.get("result")
        tool = rec["tool"]
        if tool == "get_current_environment":
            if r is not None and not (isinstance(r, dict) and "sample_time" in r):
                problems.append("seq=%d current 结果形状非法" % rec["sequence"])
        elif tool == "get_environment_history":
            if not isinstance(r, list) or any(not (isinstance(x, dict) and "sample_time" in x) for x in r):
                problems.append("seq=%d history 结果形状非法" % rec["sequence"])
        elif tool == "get_weather_forecast":
            if not (isinstance(r, dict) and "provider" in r):
                problems.append("seq=%d weather 结果形状非法" % rec["sequence"])
        elif tool == "search_tomato_knowledge":
            if not (isinstance(r, dict) and "top_chunks" in r):
                problems.append("seq=%d knowledge 结果形状非法" % rec["sequence"])


def cmd_test():
    """测试用例(2 单工具 + 2 多工具,需 API Key;无 Key 每例如实记录 no_api_key)。
    断言只查集合成员,多工具顺序由 LLM 决定,绝无硬编码顺序。"""
    print("================ B05 测试用例(单工具 / 多工具闭环)================")
    report = {"checked_at": _now_iso(), "summary": {"total": len(TEST_CASES), "passed": 0, "failed": 0},
              "checks": []}
    no_key_warned = False
    for cid, kind, task, required in TEST_CASES:
        print("\n[%s] %s任务: %s" % (cid, "单工具" if kind == "single" else "多工具", task))
        ev = run_agent(task)
        problems = []
        if ev["status"] != "success":
            problems.append("agent status=%s(%s)" % (
                ev["status"], (ev.get("error") or {}).get("error_type")))
            if (ev.get("error") or {}).get("error_type") == "no_api_key" and not no_key_warned:
                print("  ⚠ 未配置 API Key,各用例已按 no_api_key 如实记录")
                no_key_warned = True
        elif not ev["final_answer"]:
            problems.append("final_answer 为空")
        if ev["status"] == "success":
            used = {rec["tool"] for rec in ev["tool_calls"]}
            for tool in required:
                if tool not in used:
                    problems.append("缺少工具调用 %s(实际调用 %s)" % (tool, sorted(used)))
            if kind == "multi" and len(used) < 2:
                problems.append("多工具任务只调用了 %d 种工具(要求 ≥2)" % len(used))
            # 调用记录完整性 + 序号严格递增
            seqs = [rec["sequence"] for rec in ev["tool_calls"]]
            if any(not isinstance(s, int) for s in seqs) or seqs != sorted(seqs) or len(set(seqs)) != len(seqs):
                problems.append("sequence 非法或未严格递增: %s" % seqs)
            for rec in ev["tool_calls"]:
                if rec.get("tool") is None or rec.get("arguments") is None or rec.get("tool_result") is None:
                    problems.append("seq=%d 记录缺 tool/arguments/tool_result" % rec["sequence"])
            _check_tool_shapes(ev, problems)
        ok = not problems
        report["summary"]["passed" if ok else "failed"] += 1
        report["checks"].append({"id": cid, "type": kind, "task": task, "required_tools": list(required),
                                 "status": ev["status"], "tools_used": sorted({r["tool"] for r in ev["tool_calls"]}),
                                 "passed": ok, "problems": problems,
                                 "final_answer_excerpt": (ev["final_answer"] or "")[:160]})
        print("  [%s] %s %s" % ("✓" if ok else "⚠", cid, "" if ok else ";".join(problems)))
    h = hashlib.sha256()
    for c in report["checks"]:
        h.update(("%s|%s|%s\n" % (c["id"], "1" if c["passed"] else "0", ";".join(c["problems"]))).encode("utf-8"))
    report["fingerprint"] = h.hexdigest()[:12]
    print("\n  判定: %s(通过 %d/%d;指纹 %s)" % (
        "PASS — 工具智能体测试通过" if report["summary"]["failed"] == 0 else "FAIL — 存在未通过用例",
        report["summary"]["passed"], report["summary"]["total"], report["fingerprint"]))
    _save_evidence(report, "test_report.json")
    print("✓ 测试报告已保存: 证据/b05/test_report.json")


def cmd_failtest():
    """执行层失败验证(零 LLM、零 Key):参数错误/空结果/超时,直调 dispatch_tool,
    捕获异常、记录失败证据;并验证强制日志(调用行增量 ==3)。"""
    print("================ B05 执行层失败验证(零 LLM)================")
    log_path = AGENT_LOG
    before = sum(1 for line in log_path.read_text(encoding="utf-8").splitlines()
                 if "[工具调用]" in line) if log_path.exists() else 0
    cases = [
        ("001", "参数错误", "get_environment_history", {"hours": -5},
         lambda r: r["status"] == "error" and r["error_type"] == "value_error" and "hours" in r["message"]),
        ("002", "空结果", "search_tomato_knowledge", {"query": "量子计算是什么"},
         lambda r: r["status"] == "ok" and isinstance(r.get("result"), dict)
         and r["result"].get("status") == "insufficient" and r["result"].get("insufficient") is True
         and "top_chunks" in r["result"]),
        ("003", "超时", "get_weather_forecast", {"location": "北京", "timeout": (0.0001, 0.0001)},
         lambda r: r["status"] == "ok" and isinstance(r.get("result"), dict)
         and r["result"].get("status") == "error"
         and r["result"]["error"].get("error_type") == "network_timeout"),
    ]
    passed = 0
    summaries = []
    for i, (num, name, tool, args, assertion) in enumerate(cases, start=1):
        result = dispatch_tool(tool, args, seq=i, step=None)
        ok = bool(assertion(result))
        passed += 1 if ok else 0
        record = {"case": name, "called_at": _now_iso(), "tool": tool,
                  "arguments": json.loads(json.dumps(args, default=str)),  # timeout 元组转 JSON 安全形式
                  "tool_result": result, "passed": ok}
        _save_evidence(record, "fail_%s_%s.json" % (num, "bad_param" if num == "001" else
                                                    "empty_result" if num == "002" else "timeout"))
        summaries.append(record)
        print("  [%d/3] %s %s | tool=%s | 结果: %s" % (
            i, name, "✓" if ok else "⚠", tool, json.dumps(result, ensure_ascii=False)[:140]))
    after = sum(1 for line in log_path.read_text(encoding="utf-8").splitlines()
                if "[工具调用]" in line) if log_path.exists() else 0
    log_ok = (after - before) == 3
    print("  [日志] 本次新增 [工具调用] 行 %d 条(要求 3)%s" % (after - before, " ✓" if log_ok else " ⚠"))
    summary = {"checked_at": _now_iso(), "passed": passed, "total": 3,
               "log_lines_added": after - before, "log_ok": log_ok, "cases": summaries}
    _save_evidence(summary, "failtest_summary.json")
    print("\n  判定: %s(通过 %d/3;失败证据已保存 证据/b05/fail_00*.json)" % (
        "PASS — 执行层失败验证通过" if passed == 3 and log_ok else "FAIL — 未全部通过", passed))


def print_usage():
    print("用法:")
    print("  py b05_agent.py run <任务文本>      # 完整闭环:LLM 决策 → 工具执行 → 最终回答")
    print("  py b05_agent.py tools              # 查看工具定义(TOOL_DEFINITIONS)")
    print("  py b05_agent.py test               # 测试用例(2 单工具 + 2 多工具,需 API Key)")
    print("  py b05_agent.py failtest           # 执行层失败验证(参数错误/空结果/超时,零 LLM)")
    print("  py b05_agent.py --help")
    print("环境变量: B05_LLM_BASE_URL / B05_LLM_MODEL / B05_LLM_TIMEOUT / B05_MAX_STEPS(默认 6)")
    print("          API Key 读取顺序: DEEPSEEK_API_KEY → ANTHROPIC_AUTH_TOKEN")
    print("公开接口(供 B07 复用): run_agent(task) / dispatch_tool(name, arguments) / TOOL_DEFINITIONS")
    print("日志: 日志/b05_agent.log —— 每次调用强制记录 tool/arguments/tool_result/sequence")


def main():
    """B05 入口:子命令分发。退出码恒 0(清单语义);Ctrl+C/EOF 友好退出。"""
    setup_console()
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print_usage()
        return
    cmd, rest = args[0], args[1:]
    try:
        if cmd == "run":
            cmd_run(rest)
        elif cmd == "tools":
            cmd_tools()
        elif cmd == "test":
            cmd_test()
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
