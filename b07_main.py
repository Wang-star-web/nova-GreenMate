#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 GreenMate B07 — 系统组装模块(完整调度主程序)
=====================================================================

【任务目标】
  把 B01~B06 全部已完成资产组装为完整 GreenMate 系统,不新增基础能力。
  按四遍法执行:
  ① 场景组装 沿 S1~S5 场景配置串联完整运行链路,复用 B01~B06 全部接口;
  ② 机制理解 连续状态认知(历史窗口+趋势)+ 多源证据组织(4 类来源标记)
              + 可信降级(证据失败 → 【证据缺失】,信任 高/中/低);
  ③ 产品化呈现 四视图 HTML 报告:状态总览 / 状态演化 / 证据空间 / 场景回放对比;
  ④ 边界判断 能力边界说明 + 无法处理场景显式降级,LLM 仅解释不覆盖判断。
  业务执行顺序:工具结果 → 证据记录 → 结构化判断 → LLM 自然语言解释。
  统一 run_id 贯穿单次全流程;证据来源标记 SIMULATED / PUBLIC /
  DERIVED_LABEL / MODEL_OUTPUT;结构化判断落盘可保存、可对比。

【运行方式】
  py b07_main.py run S1 [--run-id run_x]  # 单场景(默认自动生成新 run_id)
  py b07_main.py run-all                  # 依次跑通 S1→S5,最后输出对比表
  py b07_main.py report <run_id>          # 重新生成 HTML 报告
  py b07_main.py list                     # 列出全部历史运行与结论
  py b07_main.py exp <type>               # 案例A 第二遍:一键生成实验 run(可复现实验框架)
  py b07_main.py exp-all                  # 依次生成 6 组实验 + 两张对比表
  py b07_main.py trace <run_id> "<结论>"  # 创新点2 溯源:返回支撑该结论的全部证据清单
  py b07_main.py check <run_id>...        # 创新点3 校验:分歧保留/天气不编造/视图一致/溯源自检
  py b07_main.py check-all                # 校验全部实验 run + 汇总表
  py b07_main.py selftest                 # 校验函数负向自测(零 LLM、零网络)
  py b07_main.py --help                   # 查看用法说明

  每个 run 落盘 evidence/b07/<run_id>/run_result.json(B08 契约校验输入)。
  实验 run 使用可读 run_id:run_{type}_{seq:03d}(如 run_agree_001)。
  全部视图与对比只读取已保存 run_id 的存档数据,禁止重新调用 API/LLM。

【说明】
  run-all 中 4 个场景含真实天气 HTTP 调用(每次约 1~3 秒);断网时
  每次最多等待 B03 默认超时,天气证据如实降级,不伪造成功。
  LLM 解释(可选)读 B07_LLM_BASE_URL / B07_LLM_MODEL / B07_LLM_TIMEOUT,
  API Key 读 DEEPSEEK_API_KEY → ANTHROPIC_AUTH_TOKEN;无 Key 时如实
  标记 omitted,结构化判断不受影响。
=====================================================================
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

# ---------------- 导入已完成模块的公开接口(组装者角色,不调任何模块的 main()) ----------------
import b01_cli
import b02_memory
import b03_weather
import b04_rag
import b06_ml
import b07_report
from b06_ml import BANDS, DAYLIGHT_RANGE

# ---------------- 配置 ----------------
BASE_DIR = Path(__file__).resolve().parent
SCENARIOS_DIR = BASE_DIR / "testdata" / "scenarios"
EXPERIMENTS_DIR = BASE_DIR / "testdata" / "experiments"
EVIDENCE_B07_DIR = BASE_DIR / "evidence" / "b07"
LOG_DIR = BASE_DIR / "logs"
B07_LOG = LOG_DIR / "b07_main.log"
RULES_FILE = BASE_DIR / "b03_rules.json"

RUN_ID_RE = re.compile(r"^run_\d{8}_\d{6}_[0-9a-f]{6}$")   # 与 b08_verify 一致的 run_id 格式
# 案例A 第二遍:实验 run 可读命名 run_{type}_{seq:03d}(如 run_agree_001)
EXPERIMENT_TYPES = ("agree", "conflict", "missing_weather",
                    "current_only", "with_history", "with_history_prediction")
EXP_RUN_ID_RE = re.compile(r"^run_(%s)_(\d{3})$" % "|".join(EXPERIMENT_TYPES))

PARAM_KEYS = ("temperature", "humidity", "co2", "light")
PARAM_NAMES = {"temperature": "温度", "humidity": "湿度", "co2": "CO₂浓度", "light": "光照"}
PARAM_UNITS = {"temperature": "℃", "humidity": "%RH", "co2": "ppm", "light": "lux"}
# 适宜带 → 规则映射(规则库 b03_rules.json R1~R8,只读载入后校验)
RULE_HIGH = {"temperature": "R1", "humidity": "R3", "co2": "R6", "light": "R8"}
RULE_LOW = {"temperature": "R2", "humidity": "R4", "co2": "R5", "light": "R7"}

# 趋势预警双条款:① 2 小时外推超出适宜带;② 斜率幅值超阈值(每小时)
TREND_SLOPE_ALERT = {"temperature": 0.5, "humidity": 1.0, "co2": 25.0, "light": 2000.0}
EXTRAPOLATION_HOURS = 2

# LLM 解释配置(全部可被环境变量覆盖,代码中零硬编码 API Key)
B07_LLM_BASE_URL = os.environ.get("B07_LLM_BASE_URL", "https://api.deepseek.com/chat/completions")
B07_LLM_MODEL = os.environ.get("B07_LLM_MODEL", "deepseek-chat")
try:
    B07_LLM_TIMEOUT = float(os.environ.get("B07_LLM_TIMEOUT", "30"))
except ValueError:
    B07_LLM_TIMEOUT = 30.0   # 环境变量非法时退回默认值,不崩溃

SYSTEM_PROMPT = ("你是 GreenMate 番茄大棚监测系统的解释助手,只做「证据忠实解释」,不做新推理。硬性规则:\n"
                 "1. 只能依据下方【数据源配置】【锚点观测】【历史/趋势(已发生)】【预测(未来)】【天气(未来外部条件)】"
                 "【知识(静态规则)】【证据清单】【结构化判断】输出解释;不得改变、否定或补充结构化判断的结论、关注级与信任级;"
                 "不得编造任何未在输入中出现的数据、工具调用结果或来源。\n"
                 "2. 四类证据不可互相替代:历史=已发生事实、天气=未来外部条件、知识=静态规则、预测=短时推演。"
                 "引用时必须分别标注类型与来源标记(SIMULATED/PUBLIC/DERIVED_LABEL/MODEL_OUTPUT),不得混为一谈。\n"
                 "3. 趋势(已发生)与预测(未来)必须分句表述,禁止合并为一句;历史趋势只能描述为\"已发生\","
                 "B06 预测只能描述为\"未来窗口\",天气只能描述为\"未来外部条件\"。\n"
                 "4. 某来源数据源配置关闭或证据缺失时,必须显式标注【证据缺失】并说明判断边界相应收窄;"
                 "不得用其他来源的证据替代该来源下结论。\n"
                 "5. 证据之间存在分歧或证据不足时,必须保留分歧、降低确定性表述;"
                 "禁止使用\"一定/必然/肯定/绝对/必定/毫无疑问/势必/铁定\"等强确定词。\n"
                 "6. 输出结构(中文,总长≤400字):① 结论与可信度(原样引用结构化判断)② 逐条依据(证据类型+来源标记+关键数值)"
                 "③ 分歧与缺失(如有,保留分歧)④ 判断边界说明。")
USER_CONTENT_CAP = 8000   # 用户内容硬截断上限(8 条摘要 + 判断 JSON 远小于此,仅作保险)

# ---- 案例A 第二遍:LLM 输出校验与工具调用溯源规则 ----
STRONG_WORDS = ("一定", "必然", "肯定", "绝对", "必定", "毫无疑问", "势必", "铁定")
# 负向后视排除"不一定/未必"类否定用法;误报项在报告附上下文供人工复核
STRONG_WORD_PATTERNS = {w: re.compile("(?<![不未])" + re.escape(w)) for w in STRONG_WORDS}
WEATHER_CLAIM_RE = re.compile(r"天气预报|根据天气|天气显示|天气预计|未来.{0,6}降温|降温至|外部天气|降水")
TOOL_CLAIM_RULES = (
    (re.compile(r"历史.{0,8}(显示|数据|趋势)|趋势.{0,6}(显示|统计|上升|下降)|升温|降温"), ("b02_history", "b02_stats"), "历史/趋势"),
    (re.compile(r"知识库|知识检索|检索结果|资料建议|管理建议"), ("b04_knowledge",), "知识"),
    (re.compile(r"模型预测|模型显示|B06|预测.{0,6}(WATCH|NORMAL)"), ("b06_predict",), "模型预测"),
    (re.compile(r"天气|降水|预报"), ("b03_weather",), "天气"),
)


def setup_console():
    """强制标准输出为 UTF-8:GBK 管道打印 ℃/CO₂ 会崩溃(本项目已多次实测)。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def _now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _log_b07(text):
    """一行日志到 logs/b07_main.log 并镜像控制台;绝不写入其他模块的日志文件。"""
    setup_console()
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), text)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(B07_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line)


def _save_json(path, obj):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        return True
    except OSError as e:
        print("⚠ 文件写入失败 %s: %s" % (path, e))
        return False


# ---------------- 场景载入 ----------------
def load_scenario(sid):
    """载入 testdata/scenarios/<sid>.json 或 testdata/experiments/exp_<sid>.json
    (scenarios 目录优先);支持 'S1'/'exp_agree' 或文件名 stem。
    失败返回 {"error": ...}(诚实降级,不崩溃)。"""
    search_dirs = [d for d in (SCENARIOS_DIR, EXPERIMENTS_DIR) if d.exists()]
    files = []
    for d in search_dirs:
        files.extend(sorted(d.glob("*.json")))
    target = None
    for p in files:
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if p.stem.lower() == str(sid).lower() or cfg.get("scenario_id", "").upper() == str(sid).upper():
            target = p
            break
    if target is None:
        return {"error": "场景/实验配置不存在: %s(可选项:%s)" % (
            sid, ", ".join(p.stem for p in files) or "无")}
    try:
        cfg = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"error": "场景配置读取失败 %s: %s" % (target, e)}
    for key in ("scenario_id", "name", "anchor_sample_time"):
        if not cfg.get(key):
            return {"error": "场景配置缺少必填字段 %s: %s" % (key, target)}
    cfg.setdefault("history_hours", 24)
    cfg.setdefault("trend_hours", 24)
    cfg.setdefault("location", "北京")
    cfg.setdefault("knowledge_query", "番茄适宜生长温度是多少")
    cfg.setdefault("degrade", {"weather": None, "knowledge": None})
    cfg.setdefault("llm_explain", True)
    cfg.setdefault("experiment_type", None)
    cfg.setdefault("sources", {"history": True, "weather": "real", "knowledge": True, "prediction": True})
    return cfg


def load_rules():
    """只读载入 b03_rules.json → {id: rule};缺失返回空表(规则判断降级为空)。"""
    try:
        data = json.loads(RULES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, None
    return {r["id"]: r for r in data.get("rules", [])}, data.get("version")


# ---------------- 工具调度(自持薄包装,不借用 B05 调度层)----------------
def b07_tool_call(seq, tool, marker, fn, arguments):
    """B07 自己的工具调度:真实执行 Python 函数,永不抛异常,每次调用必写日志。
    错误分类与 B05 同源:bad_arguments / value_error / tool_exception。
    (不用 b05.dispatch_tool:它会写 logs/b05_agent.log——那是 B05 自己的
    执行证据,failtest 按行增量计数——且无 marker 字段、不覆盖 B06。)"""
    rec = {"seq": seq, "step": "tool_results", "tool": tool, "marker": marker,
           "arguments": arguments, "result": None, "status": "error", "error": None,
           "called_at": _now_iso()}
    try:
        result = fn(**arguments)
    except TypeError as e:
        rec["error"] = {"error_type": "bad_arguments", "message": str(e)}
    except ValueError as e:
        rec["error"] = {"error_type": "value_error", "message": str(e)}
    except Exception as e:
        rec["error"] = {"error_type": "tool_exception", "message": "%s: %s" % (type(e).__name__, e)}
    else:
        rec["status"] = "ok"
        rec["result"] = result
    nested = ""
    if rec["status"] == "ok" and isinstance(rec["result"], dict):
        nested = rec["result"].get("status", "")
    if rec["status"] != "ok":
        nested = rec["error"]["error_type"]
    _log_b07("[工具] seq=%d tool=%s marker=%s status=%s%s" % (
        seq, tool, marker, rec["status"], " [nested=%s]" % nested if nested else ""))
    return rec


# ---------------- 案例A 第二遍:数据源开关 / 注入天气 / 实验 run_id ----------------
def _valid_run_id(run_id):
    """run_id 双格式放行:时间戳格式 run_YYYYmmdd_HHMMSS_<6hex> 或实验可读命名 run_<type>_<seq>。"""
    return bool(RUN_ID_RE.match(run_id) or EXP_RUN_ID_RE.match(run_id))


def _normalize_sources(cfg):
    """归一化数据源开关:{history:bool, weather:"real"|"injected_cooling"|"off",
    knowledge:bool, prediction:bool}。非法值回退默认并记日志;缺键全开(向后兼容)。"""
    raw = cfg.get("sources") or {}
    sources = {
        "history": bool(raw.get("history", True)),
        "weather": raw.get("weather", "real") if raw.get("weather") in ("real", "injected_cooling", "off") else "real",
        "knowledge": bool(raw.get("knowledge", True)),
        "prediction": bool(raw.get("prediction", True)),
    }
    if raw.get("weather") not in (None, "real", "injected_cooling", "off"):
        _log_b07("[场景] ⚠ sources.weather 非法值 %s,回退 real" % raw.get("weather"))
    return sources


def _disabled_tool_call(seq, tool, marker, reason):
    """数据源配置关闭时的占位工具记录:与 b07_tool_call 同构(status="disabled"),
    保证 trace/report 消费方无需特判。不执行任何真实调用。"""
    return {"seq": seq, "step": "tool_results", "tool": tool, "marker": marker,
            "arguments": {"skipped": True, "reason": reason}, "result": None,
            "status": "disabled", "error": None, "called_at": _now_iso()}


def inject_cooling_weather(cfg, anchor_text):
    """实验注入降温天气证据(案例A 第二遍 run_conflict 专用)。
    构造与 b03 成功证据同形状的 dict,自报身份 provider=experiment_injection、
    source_marker=SIMULATED、query.method=INJECTION——绝不冒充 PUBLIC 真实天气,
    绝不写入 evidence/b03/(B03"绝不伪造成功"原则零触碰)。
    forecast_time=锚点+forecast_hours,强制 > analysis_time(沿用 b03 同款校验)。"""
    inj = cfg.get("injected_weather") or {}
    anchor_dt = datetime.strptime(anchor_text, "%Y-%m-%d %H:%M:%S")
    analysis_iso = anchor_dt.astimezone().isoformat(timespec="seconds")
    forecast_dt = anchor_dt + timedelta(hours=float(inj.get("forecast_hours", 2)))
    forecast_iso = forecast_dt.astimezone().isoformat(timespec="seconds")

    def vf(key, unit, default):
        value = inj.get(key)
        return {"value": float(value) if value is not None else default, "unit": unit}

    return {
        "provider": inj.get("provider", "experiment_injection"),
        "retrieved_at": _now_iso(),
        "query": {"method": "INJECTION",
                  "url": "testdata/experiments/%s.json" % (cfg.get("experiment_type") or "exp"),
                  "params": {"simulated": True, "forecast_hours": inj.get("forecast_hours", 2)}},
        "status": "success",
        "error": None,
        "location": cfg.get("location", "北京"),
        "analysis_time": analysis_iso,
        "forecast_time": forecast_iso,
        "temperature": vf("temperature", "℃", 18.0),
        "humidity": vf("humidity", "%RH", 88.0),
        "precipitation_probability": vf("precipitation_probability", "%", 80.0),
        "cloud_cover": vf("cloud_cover", "%", 90.0),
        "shortwave_radiation": vf("shortwave_radiation", "W/m²", 250.0),
        "source_marker": "SIMULATED",
        "simulated": True,
        "note": inj.get("note", "实验注入天气证据,绝不冒充 PUBLIC 真实天气"),
    }


def _next_exp_seq(etype):
    """实验 run 序号:扫描 evidence/b07/ 同类型目录最大值 +1;目录已存在则继续 +1(防覆盖)。"""
    seq = 0
    if EVIDENCE_B07_DIR.exists():
        for sub in EVIDENCE_B07_DIR.iterdir():
            m = EXP_RUN_ID_RE.match(sub.name)
            if m and m.group(1) == etype:
                seq = max(seq, int(m.group(2)))
    while (EVIDENCE_B07_DIR / ("run_%s_%03d" % (etype, seq + 1))).exists():
        seq += 1
    return seq + 1


# ---------------- 证据记录(与工具结果分离的可引用形式)----------------
def _ev_rec(seq, eid, source, marker, tool, summary, status, detail,
            evidence_type="other", evidence_time=None):
    """证据自报身份:类型/来源/标记/获取时间/可用性/结果(案例A 第二遍证据模型)。
    relation/relation_reason 由 compute_evidence_relations 在 run 时回填。"""
    return {"seq": seq, "evidence_id": eid, "source": source, "source_marker": marker,
            "tool": tool, "summary": str(summary)[:200], "status": status,
            "timestamp": _now_iso(), "evidence_type": evidence_type,
            "evidence_time": evidence_time, "relation": None, "relation_reason": None,
            "detail": detail}


def _disabled_ev(seq, source, marker, tool, label, evidence_type, anchor_text):
    """数据源配置关闭的证据记录:自报「配置关闭」,不计入降级(与 error 区分)。"""
    return _ev_rec(seq, "EV%02d" % seq, source, marker, tool,
                   "【数据源配置关闭】%s 未启用,本次运行未获取该来源证据(判断边界相应收窄)" % label,
                   "disabled", {"disabled": True, "anchor": anchor_text},
                   evidence_type=evidence_type, evidence_time=anchor_text)


def build_evidence_records(tool_results, cfg, run_id, anchor_text):
    """每个工具结果 → 一条证据记录;status: ok / error / insufficient / disabled。
    summary ≤200 字符(LLM 解释的输入也有尺寸上限)。"""
    records = []
    by_tool = {tr["tool"]: tr for tr in tool_results}
    seq = 1

    # EV01 b02_current —— SIMULATED,库内最新样本(仅上下文,判断依据是锚点)
    tr = by_tool.get("b02_current")
    res = tr["result"] if tr else None
    if tr and tr["status"] == "ok" and isinstance(res, dict) and res.get("sample_time"):
        summary = ("当前环境状态(库内最新样本,仅上下文): %s 温度%s℃/湿度%s%%RH/CO₂%sppm/光照%slux"
                   % (res["sample_time"], res["temperature"], res["humidity"], res["co2"], res["light"]))
        records.append(_ev_rec(seq, "EV%02d" % seq, "b02_memory", "SIMULATED", "b02_current",
                               summary, "ok", res, evidence_type="current",
                               evidence_time=res.get("sample_time")))
    else:
        records.append(_ev_rec(seq, "EV%02d" % seq, "b02_memory", "SIMULATED", "b02_current",
                               "当前环境状态不可用(B02 库为空或不存在)", "error", {"result": res},
                               evidence_type="current", evidence_time=anchor_text))
    seq += 1

    # EV02 b02_anchor —— SIMULATED,场景锚点样本(判断依据)
    tr = by_tool.get("b02_anchor")
    rows = tr["result"] if tr else None
    if tr and tr["status"] == "ok" and isinstance(rows, list) and rows:
        r = rows[0]
        summary = ("场景锚点样本 %s: 温度%s℃/湿度%s%%RH/CO₂%sppm/光照%slux,source_type=%s"
                   % (anchor_text, r["temperature"], r["humidity"], r["co2"], r["light"], r["source_type"]))
        records.append(_ev_rec(seq, "EV%02d" % seq, "b02_memory", "SIMULATED", "b02_anchor",
                               summary, "ok", r, evidence_type="current", evidence_time=anchor_text))
    else:
        records.append(_ev_rec(seq, "EV%02d" % seq, "b02_memory", "SIMULATED", "b02_anchor",
                               "锚点样本缺失: %s(B02 库中无该时刻样本)" % anchor_text, "error",
                               {"query": {"start": anchor_text, "end": anchor_text}, "result": rows or []},
                               evidence_type="current", evidence_time=anchor_text))
    seq += 1

    # EV03 b01_raw —— SIMULATED,原始采集记录存档
    tr = by_tool.get("b01_raw")
    ok = bool(tr and tr["status"] == "ok" and tr["result"] is True)
    if tr:
        args = tr.get("arguments") or {}
        record = args.get("record") or {}
        detail = {"path": "evidence/raw/%s.json" % run_id, "saved": ok,
                  "collect_time": record.get("collect_time"),
                  "source_type": record.get("source_type"),
                  "valid_ranges": record.get("valid_ranges")}
        records.append(_ev_rec(seq, "EV%02d" % seq, "b01_cli", "SIMULATED", "b01_raw",
                               ("B01 原始采集记录已存档 evidence/raw/%s.json(collect_time=%s,source_type=SIMULATED)"
                                % (run_id, record.get("collect_time", "—"))) if ok
                               else "B01 原始记录保存失败(evidence/raw/%s.json)" % run_id,
                               "ok" if ok else "error", detail, evidence_type="raw",
                               evidence_time=record.get("collect_time") or anchor_text))
    else:
        records.append(_ev_rec(seq, "EV%02d" % seq, "b01_cli", "SIMULATED", "b01_raw",
                               "锚点缺失,B01 原始记录未生成(无样本可存档)", "error", {"path": None, "saved": False},
                               evidence_type="raw", evidence_time=anchor_text))
    seq += 1

    # EV04 b02_history —— SIMULATED,历史窗口(已发生事实)
    tr = by_tool.get("b02_history")
    rows = tr["result"] if tr else None
    if tr and tr["status"] == "disabled":
        records.append(_disabled_ev(seq, "b02_memory", "SIMULATED", "b02_history",
                                    "历史时序", "history", anchor_text))
    elif tr and tr["status"] == "ok" and isinstance(rows, list):
        detail = {"start": (tr["arguments"] or {}).get("start"), "end": anchor_text,
                  "count": len(rows), "first": rows[0] if rows else None,
                  "last": rows[-1] if rows else None}
        records.append(_ev_rec(seq, "EV%02d" % seq, "b02_memory", "SIMULATED", "b02_history",
                               "历史窗口 %s ~ %s 共 %d 个样本(源=environment_samples,30 分钟网格,已发生事实)"
                               % (detail["start"], anchor_text, len(rows)),
                               "ok" if rows else "error", detail, evidence_type="history",
                               evidence_time=("%s~%s" % (detail["start"], anchor_text))))
    else:
        records.append(_ev_rec(seq, "EV%02d" % seq, "b02_memory", "SIMULATED", "b02_history",
                               "历史窗口查询失败或无样本", "error", {"result": rows},
                               evidence_type="history", evidence_time=anchor_text))
    seq += 1

    # EV05 b02_stats —— SIMULATED,趋势窗口统计(已发生)
    tr = by_tool.get("b02_stats")
    res = tr["result"] if tr else None
    if tr and tr["status"] == "disabled":
        records.append(_disabled_ev(seq, "b02_memory", "SIMULATED", "b02_stats",
                                    "历史趋势统计", "trend", anchor_text))
    elif tr and tr["status"] == "ok" and isinstance(res, dict) and res.get("count"):
        parts = []
        for key in PARAM_KEYS:
            s = (res.get("parameters") or {}).get(key, {}).get("slope_per_hour")
            parts.append("%s%s" % (PARAM_NAMES[key], ("%+.2f" % s) if s is not None else "n/a"))
        detail = {"window": res.get("window"), "count": res.get("count"),
                  "parameters": {k: {kk: vv for kk, vv in (res.get("parameters") or {}).get(k, {}).items()}
                                 for k in PARAM_KEYS}}
        records.append(_ev_rec(seq, "EV%02d" % seq, "b02_memory", "SIMULATED", "b02_stats",
                               "趋势窗口 %s ~ %s 共 %d 点;斜率(每小时,已发生): %s"
                               % ((res.get("window") or {}).get("start"), anchor_text, res["count"], "/".join(parts)),
                               "ok", detail, evidence_type="trend",
                               evidence_time=("%s~%s" % ((res.get("window") or {}).get("start"), anchor_text))))
    else:
        records.append(_ev_rec(seq, "EV%02d" % seq, "b02_memory", "SIMULATED", "b02_stats",
                               "趋势窗口无样本(趋势判断跳过)", "error", {"result": res},
                               evidence_type="trend", evidence_time=anchor_text))
    seq += 1

    # EV06 b03_weather —— PUBLIC(标记由 B07 附加;注入实验时为 SIMULATED)
    tr = by_tool.get("b03_weather")
    res = tr["result"] if tr else None
    if tr and tr["status"] == "disabled":
        records.append(_disabled_ev(seq, "b03_weather", "PUBLIC", "b03_weather",
                                    "外部天气", "weather", anchor_text))
    elif tr and tr["status"] == "ok" and isinstance(res, dict) and res.get("status") == "success":
        injected = bool(res.get("simulated"))
        marker = tr.get("marker") or ("SIMULATED" if injected else "PUBLIC")
        detail = {"provider": res.get("provider"), "forecast_time": res.get("forecast_time"),
                  "analysis_time": res.get("analysis_time"), "location": res.get("location"),
                  "temperature": res.get("temperature"), "humidity": res.get("humidity"),
                  "precipitation_probability": res.get("precipitation_probability"),
                  "cloud_cover": res.get("cloud_cover"), "shortwave_radiation": res.get("shortwave_radiation"),
                  "simulated": injected}
        summary = ("%s 天气预报: forecast_time=%s,温度%s℃/湿度%s%%RH/降水概率%s%%"
                   % (res.get("location", "—"), res.get("forecast_time", "—"),
                      (res.get("temperature") or {}).get("value", "—"),
                      (res.get("humidity") or {}).get("value", "—"),
                      (res.get("precipitation_probability") or {}).get("value", "—")))
        if injected:
            summary += "(实验注入 SIMULATED,非真实天气)"
        records.append(_ev_rec(seq, "EV%02d" % seq, "b03_weather", marker, "b03_weather",
                               summary, "ok", detail, evidence_type="weather",
                               evidence_time=res.get("forecast_time") or res.get("retrieved_at")))
    else:
        err = (res or {}).get("error") or {}
        records.append(_ev_rec(seq, "EV%02d" % seq, "b03_weather", tr.get("marker", "PUBLIC") if tr else "PUBLIC",
                               "b03_weather",
                               "天气证据获取失败: %s: %s" % (err.get("error_type", "error"),
                                                          err.get("message", "未知错误")),
                               "error", {"status": "error", "error": err},
                               evidence_type="weather", evidence_time=anchor_text))
    seq += 1

    # EV07 b04_knowledge —— SIMULATED(课程自编资料,如实标注;静态规则)
    tr = by_tool.get("b04_knowledge")
    res = tr["result"] if tr else None
    note = "知识库为课程自编模拟资料(5 份 source),检索结果仅作建议参考"
    if tr and tr["status"] == "disabled":
        records.append(_disabled_ev(seq, "b04_rag", "SIMULATED", "b04_knowledge",
                                    "专业知识库", "knowledge", anchor_text))
    elif tr and tr["status"] == "ok" and isinstance(res, dict) and res.get("status") == "ok":
        top = res["top_chunks"][0]
        detail = {"query": res.get("query"), "threshold": res.get("threshold"),
                  "top1": {"chunk_id": top.get("chunk_id"), "score": top.get("score"),
                           "source_id": top.get("source_id"), "citation": top.get("citation")},
                  "note": note}
        records.append(_ev_rec(seq, "EV%02d" % seq, "b04_rag", "SIMULATED", "b04_knowledge",
                               "知识检索命中 Top-1 %s(得分 %.4f): %s…"
                               % (top.get("chunk_id"), top.get("score"), (top.get("text") or "")[:60]),
                               "ok", detail, evidence_type="knowledge",
                               evidence_time=res.get("retrieved_at")))
    elif tr and tr["status"] == "ok" and isinstance(res, dict):
        top_score = (res.get("top_chunks") or [{}])[0].get("score", 0.0) if res.get("top_chunks") else 0.0
        records.append(_ev_rec(seq, "EV%02d" % seq, "b04_rag", "SIMULATED", "b04_knowledge",
                               (res.get("message") or "【资料不足/无法确认】知识库未覆盖该问题"),
                               "insufficient", {"query": res.get("query"),
                                                "threshold": res.get("threshold"),
                                                "top_score": top_score, "note": note},
                               evidence_type="knowledge", evidence_time=anchor_text))
    else:
        records.append(_ev_rec(seq, "EV%02d" % seq, "b04_rag", "SIMULATED", "b04_knowledge",
                               "知识检索执行失败", "error", {"result": res},
                               evidence_type="knowledge", evidence_time=anchor_text))
    seq += 1

    # EV08 b06_predict —— MODEL_OUTPUT(独立证据标记;短时推演,未来窗口)
    tr = by_tool.get("b06_predict")
    res = tr["result"] if tr else None
    if tr and tr["status"] == "disabled":
        records.append(_disabled_ev(seq, "b06_ml", "MODEL_OUTPUT", "b06_predict",
                                    "B06 短时预测", "prediction", anchor_text))
    elif tr and tr["status"] == "ok" and isinstance(res, dict) and res.get("status") == "ok":
        prob = (res.get("probability") or {}).get("WATCH")
        detail = {"prediction": res.get("prediction"), "probability": res.get("probability"),
                  "model_version": res.get("model_version"), "window": res.get("window"),
                  "MODEL_OUTPUT": res.get("MODEL_OUTPUT"), "label_source": res.get("label_source")}
        records.append(_ev_rec(seq, "EV%02d" % seq, "b06_ml", "MODEL_OUTPUT", "b06_predict",
                               "B06 预测 next_window_attention=%s(P(WATCH)=%s,模型 %s,未来 1~2h 窗口短时推演)"
                               % (res.get("prediction"), prob, res.get("model_version")),
                               "ok", detail, evidence_type="prediction",
                               evidence_time=res.get("window", {}).get("end") if isinstance(res.get("window"), dict) else anchor_text))
    else:
        err = (res or {}).get("error") or {}
        records.append(_ev_rec(seq, "EV%02d" % seq, "b06_ml", "MODEL_OUTPUT", "b06_predict",
                               "模型证据缺失: %s: %s" % (err.get("code", "error"),
                                                     err.get("message", "未知错误")),
                               "error", {"MODEL_OUTPUT": (res or {}).get("MODEL_OUTPUT"), "error": err},
                               evidence_type="prediction", evidence_time=anchor_text))
    return records


# ---------------- 结构化判断引擎(确定性,复用不复制)----------------
def judge_environment(cfg, run_id, anchor_text, anchor_dt, anchor_record, stats_result,
                      model_ev, weather_ev, knowledge_ev, rules, sources=None):
    """业务顺序第 ③ 步:工具结果之上的确定性结构化判断。
    逐参数规则判定(阈值表 b03_rules.json R1~R8)→ 趋势双条款 → 融合表 →
    信任与降级。sources 为数据源开关(案例A 第二遍):disabled 来源不计入
    degraded(配置关闭 ≠ 证据失败),改记边界说明与证据完整度。
    纯函数,不打印、不调用 LLM。"""
    sources = _normalize_sources(cfg) if sources is None else sources
    hour = anchor_dt.hour + anchor_dt.minute / 60.0
    daylight = DAYLIGHT_RANGE[0] <= hour < DAYLIGHT_RANGE[1]

    # ---- 逐参数判定 ----
    per_param = []
    matched_ids = []
    for key in PARAM_KEYS:
        value = (anchor_record or {}).get(key)
        fallback_note = None
        if value is None and stats_result and (stats_result.get("last") or {}).get(key) is not None:
            value = stats_result["last"][key]
            fallback_note = "锚点样本缺失,取值退用趋势窗口末样本"
        band = list(BANDS[key])
        verdict, rule_ids, note = "适宜", [], None
        if value is None:
            verdict, note = "无法判定", "锚点样本缺失,无法取值"
        elif key == "light" and not daylight:
            verdict, note = "夜间不检查", "夜间(6≤h<18 之外)光照规则 R7/R8 不适用"
        else:
            lo, hi = band
            if value > hi:
                verdict, rule_ids = "偏高", [RULE_HIGH[key]]
            elif value < lo:
                verdict, rule_ids = "偏低", [RULE_LOW[key]]
        if fallback_note:
            note = (note + ";" if note else "") + fallback_note
        matched_ids.extend(rule_ids)
        per_param.append({"parameter": key, "name": PARAM_NAMES[key], "unit": PARAM_UNITS[key],
                          "value": value, "band": band, "verdict": verdict,
                          "matched_rules": rule_ids,
                          "rule_names": [rules[r]["name"] for r in rule_ids if r in rules],
                          "advice": [rules[r]["advice"] for r in rule_ids if r in rules],
                          "note": note})

    # ---- 趋势预警(双条款:2 小时外推 + 斜率幅值)----
    stats_params = (stats_result or {}).get("parameters") or {}
    trend_findings = []
    trend_alert = False
    for key in PARAM_KEYS:
        st = stats_params.get(key) or {}
        s = st.get("slope_per_hour")
        if s is None:
            trend_findings.append({"parameter": key, "name": PARAM_NAMES[key], "slope_per_hour": None,
                                   "alert": False, "reason": "窗口样本不足 2 点,无斜率"})
            continue
        if key == "light" and not daylight:
            trend_findings.append({"parameter": key, "name": PARAM_NAMES[key],
                                   "slope_per_hour": round(s, 4), "anchor_value": (anchor_record or {}).get(key),
                                   "clause1_extrapolation": False, "clause2_magnitude": False,
                                   "alert": False, "reason": "夜间光照趋势不检查"})
            continue
        v = (anchor_record or {}).get(key)
        if v is None and (stats_result or {}).get("last"):
            v = stats_result["last"].get(key)
        clause1, clause2, reason = False, False, None
        if v is not None:
            lo, hi = BANDS[key]
            extrap = v + EXTRAPOLATION_HOURS * s
            if extrap > hi or extrap < lo:
                clause1 = True
                reason = "条款① 2 小时外推值 %.1f 超出适宜带 %.0f~%.0f" % (extrap, lo, hi)
        thr = TREND_SLOPE_ALERT[key]
        if abs(s) >= thr:
            clause2 = True
            extra = "条款② |斜率| %.2f/小时 ≥ 幅值阈值 %.1f" % (abs(s), thr)
            reason = (reason + ";" + extra) if reason else extra
        alert = clause1 or clause2
        trend_alert = trend_alert or alert
        trend_findings.append({"parameter": key, "name": PARAM_NAMES[key], "slope_per_hour": round(s, 4),
                               "anchor_value": v, "clause1_extrapolation": clause1,
                               "clause2_magnitude": clause2, "alert": alert, "reason": reason})

    # ---- 降级原因(逐来源,顺序固定;disabled 来源不计入降级)----
    degraded = []
    weather_degraded = not (isinstance(weather_ev, dict) and weather_ev.get("status") == "success")
    if weather_degraded and sources["weather"] != "off":
        err = ((weather_ev or {}).get("error") or {}) if isinstance(weather_ev, dict) else {}
        degraded.append("PUBLIC b03_weather 证据缺失: %s: %s" % (
            err.get("error_type", "error"), err.get("message", "未取得天气证据")))
    knowledge_insufficient = isinstance(knowledge_ev, dict) and knowledge_ev.get("status") == "insufficient"
    if knowledge_insufficient and sources["knowledge"]:
        degraded.append("SIMULATED b04_knowledge 证据不足: %s" % (
            (knowledge_ev.get("message") or "【资料不足/无法确认】知识库未覆盖该问题")[:160]))
    model_degraded = not (isinstance(model_ev, dict) and model_ev.get("status") == "ok")
    if model_degraded and sources["prediction"]:
        err = ((model_ev or {}).get("error") or {}) if isinstance(model_ev, dict) else {}
        degraded.append("MODEL_OUTPUT b06_predict 证据缺失: %s: %s" % (
            err.get("code", "error"), err.get("message", "未知错误")))
    if anchor_record is None:
        degraded.append("SIMULATED 锚点样本缺失: %s(B02 库中无该时刻样本)" % anchor_text)
    if not (stats_result and stats_result.get("count")) and sources["history"]:
        degraded.append("SIMULATED b02_stats 证据缺失: 趋势窗口无样本,趋势判断跳过")

    # ---- 融合表:结论 / 关注级 / 信任 ----
    rule_alert = bool(matched_ids)
    model_watch = bool(isinstance(model_ev, dict) and model_ev.get("status") == "ok"
                       and model_ev.get("prediction") == "WATCH")
    if rule_alert:
        conclusion, attention = "需要预警", "warning"
    elif model_watch or trend_alert:
        conclusion, attention = "需要关注", "attention"
    else:
        conclusion, attention = "正常", "normal"
    if degraded:
        trust = "低"
    elif rule_alert != model_watch:
        trust = "中"
    else:
        trust = "高"

    reasons = list(degraded)
    if not degraded and rule_alert != model_watch:
        if model_watch and not rule_alert:
            p = (model_ev.get("probability") or {}).get("WATCH")
            reasons.append("健康来源分歧: 规则判定无违规 vs MODEL_OUTPUT WATCH(P=%s),"
                           "按保守原则升级为需要关注,trust 降至中" % (p if p is not None else "?"))
        else:
            reasons.append("健康来源分歧: 规则命中 %s vs MODEL_OUTPUT NORMAL,"
                           "按保守原则维持规则预警,trust 降至中" % "、".join(sorted(set(matched_ids))))
    if not rule_alert and conclusion == "需要关注":
        reasons.append("规则未报警,但模型预警/趋势信号提示风险(结论=需要关注)")

    # ---- 来源冲突检测(案例A 第二遍:注入降温 vs 历史升温 → 保留分歧)----
    source_conflicts = []
    history_warming = bool(stats_params
                           and isinstance((stats_params.get("temperature") or {}).get("slope_per_hour"), (int, float))
                           and stats_params["temperature"]["slope_per_hour"] >= TREND_SLOPE_ALERT["temperature"])
    weather_injected_cooling = (isinstance(weather_ev, dict) and weather_ev.get("status") == "success"
                                and weather_ev.get("provider") == "experiment_injection")
    forecast_temp = ((weather_ev or {}).get("temperature") or {}).get("value") if isinstance(weather_ev, dict) else None
    anchor_temp = (anchor_record or {}).get("temperature")
    if weather_injected_cooling and history_warming and forecast_temp is not None \
            and anchor_temp is not None and forecast_temp <= anchor_temp - 3.0:
        source_conflicts.append({
            "type": "history_vs_weather",
            "history": {"direction": "升温", "slope_per_hour": round(stats_params["temperature"]["slope_per_hour"], 4),
                        "kind": "已发生事实"},
            "weather": {"direction": "降温", "forecast_time": weather_ev.get("forecast_time"),
                        "forecast_temperature": forecast_temp, "provider": "experiment_injection",
                        "marker": "SIMULATED", "kind": "未来外部条件(实验注入,非真实天气)"}})
        reasons.append("来源分歧: 历史 3h 温度持续上升(已发生事实) vs 实验注入天气证据预报降温至 %.1f℃"
                       "(未来外部条件,SIMULATED)——两类证据方向相反,保留分歧、不下绝对化结论" % forecast_temp)

    # ---- 能力边界说明(恒含 + 条件追加)----
    notes = [
        "真实传感器不可用:环境数据均为课程模拟(source_type=SIMULATED),本系统不声称真实监测。",
        "不可处理事项:长期天气预报(>1 天)、跨域农业问题、真实设备控制——系统只输出判断与解释,不执行任何控制动作。",
        "知识库证据为课程自编模拟资料(5 份 source),检索结果仅作管理建议参考,不构成专业农艺结论。",
    ]
    if weather_degraded:
        notes.append("外部天气证据(PUBLIC)不可用时,结论仅基于棚内模拟证据并如实标注【证据缺失】。")
    if knowledge_insufficient:
        notes.append("知识库未覆盖的问题输出【资料不足/无法确认】,不编造内容、不伪造引用。")
    if not daylight:
        notes.append("夜间(6≤h<18 之外)光照规则 R7/R8 不适用,光照不参与本次判断。")
    if model_degraded:
        notes.append("模型文件缺失/损坏时 MODEL_OUTPUT 证据缺失,判断降级为仅规则+趋势。")
    if (cfg.get("degrade") or {}).get("weather") == "timeout" and (cfg.get("degrade") or {}).get("knowledge") == "uncovered":
        notes.append("本场景为边界判断演示:外部证据与知识证据同时退化的显式降级处理路径。")
    # 数据源配置关闭(实验):禁用来源不计入降级,但判断边界必须如实标注
    disabled_labels = []
    if not sources["history"]:
        disabled_labels.append("历史时序/趋势")
        notes.append("数据源配置关闭(实验): 历史与趋势证据未启用,无法区分「稳定态」与「漂移态」,判断仅基于当前瞬时读数。")
    if sources["weather"] == "off":
        disabled_labels.append("外部天气")
        notes.append("数据源配置关闭(实验): 天气证据未启用,判断边界不含外部天气条件,依赖天气的远期推断暂停。")
    if not sources["knowledge"]:
        disabled_labels.append("专业知识库")
        notes.append("数据源配置关闭(实验): 知识库未启用,不输出管理建议类内容。")
    if not sources["prediction"]:
        disabled_labels.append("B06 短时预测")
        notes.append("数据源配置关闭(实验): B06 预测未启用,无未来 1~2h 窗口推演,趋势(已发生)与预测(未来)均不输出。")
    # 证据完整度(案例A 第二遍):四类证据来源中可用数量
    available = 0
    if sources["history"] and (stats_result and stats_result.get("count")):
        available += 1
    if sources["weather"] != "off" and not weather_degraded:
        available += 1
    if sources["knowledge"] and not knowledge_insufficient:
        available += 1
    if sources["prediction"] and not model_degraded:
        available += 1
    evidence_completeness = {"available": available, "expected": 4,
                             "disabled": disabled_labels,
                             "note": "四类证据(历史/天气/知识/预测)中当前可用 %d/4;配置关闭的来源不计入缺失,判断边界已相应收窄" % available}

    # ---- 来源发现(多源证据组织)----
    trend_hits = [f for f in trend_findings if f.get("alert")]
    findings = [
        {"source": "b03_rules", "marker": "DERIVED_LABEL",
         "finding": ("规则判定: 命中 %s" % "、".join(sorted(set(matched_ids)))) if matched_ids else "规则判定: 无违规",
         "detail": "4 参数逐项比对适宜带(阈值表 b03_rules.json R1~R8)"},
        {"source": "b02_trend", "marker": "DERIVED_LABEL",
         "finding": ("趋势预警: %s" % "、".join("%s(%s)" % (f["name"], f["reason"]) for f in trend_hits))
                    if trend_hits else "趋势: 无预警",
         "detail": "趋势窗口 %s 小时;双条款:外推 %d 小时 + 幅值阈值" % (cfg.get("trend_hours"), EXTRAPOLATION_HOURS)},
        {"source": "b06_predict", "marker": "MODEL_OUTPUT",
         "finding": ("模型预测 %s(P(WATCH)=%s,%s)"
                     % (model_ev.get("prediction"), (model_ev.get("probability") or {}).get("WATCH"),
                        model_ev.get("model_version")))
                    if not model_degraded else "模型证据缺失(预测不可用)",
         "detail": "输入窗口 3 小时(6 点)预测未来 1~2 小时 next_window_attention"},
        {"source": "b03_weather", "marker": "PUBLIC",
         "finding": ("天气证据可用: forecast_time=%s" % weather_ev.get("forecast_time"))
                    if not weather_degraded else "天气证据缺失(外部证据不可用)",
         "detail": "provider=%s,location=%s" % ((weather_ev or {}).get("provider"),
                                             ((weather_ev or {}).get("location") or "—"))},
        {"source": "b04_knowledge", "marker": "SIMULATED",
         "finding": ("知识检索命中: %s" % (knowledge_ev.get("top_chunks") or [{}])[0].get("chunk_id"))
                    if (isinstance(knowledge_ev, dict) and knowledge_ev.get("status") == "ok")
                    else "知识检索未覆盖(【资料不足/无法确认】)" if knowledge_insufficient else "知识检索不可用",
         "detail": "查询: %s(课程自编模拟资料)" % (cfg.get("knowledge_query", ""))[:80]},
    ]

    judgment = {
        "run_id": run_id, "scenario_id": cfg["scenario_id"], "scenario_name": cfg["name"],
        "judged_at": _now_iso(), "anchor_sample_time": anchor_text,
        "conclusion": conclusion, "attention_level": attention, "trust_level": trust,
        "experiment_type": cfg.get("experiment_type"),
        "per_param": per_param,
        "matched_rules": [rules[r] for r in sorted(set(matched_ids)) if r in rules],
        "trend_findings": trend_findings,
        "source_findings": findings,
        "source_conflicts": source_conflicts,
        "evidence_completeness": evidence_completeness,
        "degraded_reasons": reasons,
        "capability_boundary_notes": notes,
        "rule_engine": {"rules_file": "b03_rules.json", "bands": {k: list(v) for k, v in BANDS.items()},
                        "daylight_range": list(DAYLIGHT_RANGE),
                        "trend_config": {"trend_hours": cfg.get("trend_hours"),
                                         "slope_alert_thresholds": TREND_SLOPE_ALERT,
                                         "extrapolation_hours": EXTRAPOLATION_HOURS}},
    }
    return judgment


# ---------------- LLM 自然语言解释(仅解释,绝不覆盖判断)----------------
def _get_api_key():
    """API Key 读取: DEEPSEEK_API_KEY → ANTHROPIC_AUTH_TOKEN;绝不打印 key 值。"""
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        return key, "env:DEEPSEEK_API_KEY"
    key = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if key:
        return key, "env:ANTHROPIC_AUTH_TOKEN"
    return None, None


def explain_judgment(cfg, judgment, evidence_records, sources, anchor_rows, tool_results):
    """业务顺序第 ④ 步:对已落盘的结构化判断生成自然语言综合解释。
    输入按证据类型分块(数据源配置/锚点观测/历史(已发生)/趋势(已发生)/
    预测(未来)/天气(未来外部条件)/知识(静态规则)/证据清单/结构化判断);
    数据源关闭的来源输出【数据源配置关闭】并收窄边界——绝不偷偷补齐。
    不带 tools 载荷的简单 chat 调用(b05 的 _call_llm_with_tools 恒带 tools,
    b04 的 _call_llm 为私有函数,均不适配——B07 自持)。失败分类与 b01/b05
    同源,任何失败 → status=omitted,结构化判断保持不变。"""
    if not cfg.get("llm_explain", True):
        return {"status": "skipped", "text": None, "note": "场景配置 llm_explain=false"}
    key, key_src = _get_api_key()
    if key is None:
        return {"status": "omitted", "text": None,
                "note": "no_api_key: 未配置 DEEPSEEK_API_KEY / ANTHROPIC_AUTH_TOKEN,"
                        "跳过 LLM 解释(结构化判断不受影响)"}
    try:
        import requests   # 惰性导入:无 Key 场景零依赖
    except ImportError:
        return {"status": "omitted", "text": None,
                "note": "missing_dependency: 未安装 requests 库,无法调用 LLM"}
    by_tool = {tr["tool"]: tr for tr in tool_results}
    user_lines = ["场景: %s %s(experiment_type=%s)" % (
        cfg["scenario_id"], cfg["name"], cfg.get("experiment_type") or "无")]
    # ① 数据源配置
    weather_text = {"real": "真实 API", "injected_cooling": "实验注入(injected_cooling,降温)",
                    "off": "关闭"}.get(sources["weather"], sources["weather"])
    user_lines.append("【数据源配置】 history=%s weather=%s knowledge=%s prediction=%s" % (
        "开" if sources["history"] else "关", weather_text,
        "开" if sources["knowledge"] else "关", "开" if sources["prediction"] else "关"))
    # ② 锚点观测(原始数值)
    if anchor_rows:
        r = anchor_rows[0]
        user_lines.append("【锚点观测(判断基准,原始数值)】 %s 温度%s℃/湿度%s%%RH/CO₂%sppm/光照%slux(source_type=%s)"
                          % (r["sample_time"], r["temperature"], r["humidity"], r["co2"], r["light"], r["source_type"]))
    else:
        user_lines.append("【锚点观测】 锚点样本缺失: %s" % cfg["anchor_sample_time"])
    # ③ 历史窗口(已发生事实)
    hist_rows = by_tool.get("b02_history", {}).get("result")
    if not sources["history"]:
        user_lines.append("【历史窗口(已发生事实)】 【数据源配置关闭】历史证据未启用,判断边界不含历史趋势。")
    elif isinstance(hist_rows, list) and hist_rows:
        first, last = hist_rows[0], hist_rows[-1]
        user_lines.append("【历史窗口(已发生事实)】 近%s h 共%d样本 %s~%s;温度%s→%s℃(%+.1f)、湿度%s→%s%%RH(%+.1f)、"
                          "CO₂%s→%sppm(%+.0f)、光照%s→%slux(%+.0f)"
                          % (cfg.get("history_hours"), len(hist_rows), first["sample_time"], last["sample_time"],
                             first["temperature"], last["temperature"], last["temperature"] - first["temperature"],
                             first["humidity"], last["humidity"], last["humidity"] - first["humidity"],
                             first["co2"], last["co2"], last["co2"] - first["co2"],
                             first["light"], last["light"], last["light"] - first["light"]))
    else:
        user_lines.append("【历史窗口(已发生事实)】 历史窗口无样本。")
    # ④ 趋势统计(已发生)
    if not sources["history"]:
        user_lines.append("【趋势统计(已发生)】 【数据源配置关闭】")
    else:
        trend_parts = []
        for f in judgment.get("trend_findings", []):
            s = f.get("slope_per_hour")
            if s is None:
                continue
            trend_parts.append("%s %+.2f/小时(%s)" % (f["name"], s, f.get("reason") or "平稳"))
        user_lines.append("【趋势统计(已发生)】 %s" % (";".join(trend_parts) if trend_parts else "无趋势预警(平稳)"))
    # ⑤ 模型预测(未来 1~2h 窗口,短时推演)
    model_ev = by_tool.get("b06_predict", {}).get("result")
    if not sources["prediction"]:
        user_lines.append("【模型预测(未来1~2h窗口)】 【数据源配置关闭】预测证据未启用。")
    elif isinstance(model_ev, dict) and model_ev.get("status") == "ok":
        prob = (model_ev.get("probability") or {}).get("WATCH")
        user_lines.append("【模型预测(未来1~2h窗口,短时推演)】 B06 预测 %s(P(WATCH)=%s,%s,MODEL_OUTPUT)"
                          % (model_ev.get("prediction"), prob, model_ev.get("model_version")))
    else:
        user_lines.append("【模型预测(未来1~2h窗口)】 【证据缺失】模型预测不可用。")
    # ⑥ 天气证据(未来外部条件)
    weather_ev = by_tool.get("b03_weather", {}).get("result")
    if sources["weather"] == "off":
        user_lines.append("【天气证据(未来外部条件)】 【数据源配置关闭】天气证据未启用,不推断外部天气影响。")
    elif isinstance(weather_ev, dict) and weather_ev.get("status") == "success":
        inj_note = "(source_marker=SIMULATED,实验注入,绝不代表真实天气)" if weather_ev.get("simulated") else "(PUBLIC 真实天气 API)"
        user_lines.append("【天气证据(未来外部条件)】 provider=%s,forecast_time=%s,温度%s℃/湿度%s%%RH/降水概率%s%%%s"
                          % (weather_ev.get("provider"), weather_ev.get("forecast_time"),
                             (weather_ev.get("temperature") or {}).get("value", "—"),
                             (weather_ev.get("humidity") or {}).get("value", "—"),
                             (weather_ev.get("precipitation_probability") or {}).get("value", "—"), inj_note))
    else:
        user_lines.append("【天气证据(未来外部条件)】 【证据缺失】天气证据不可用,判断边界不含外部天气条件。")
    # ⑦ 知识证据(静态规则建议)
    know_ev = by_tool.get("b04_knowledge", {}).get("result")
    if not sources["knowledge"]:
        user_lines.append("【知识证据(静态规则建议)】 【数据源配置关闭】知识库未启用。")
    elif isinstance(know_ev, dict) and know_ev.get("status") == "ok":
        top = (know_ev.get("top_chunks") or [{}])[0]
        user_lines.append("【知识证据(静态规则建议)】 命中 %s(得分 %.4f,citation=%s,SIMULATED 课程自编资料,仅建议)"
                          % (top.get("chunk_id"), top.get("score"),
                             (top.get("citation") or {}).get("chunk_id", "—")))
    elif isinstance(know_ev, dict):
        user_lines.append("【知识证据(静态规则建议)】 %s" % (know_ev.get("message") or "【资料不足/无法确认】"))
    else:
        user_lines.append("【知识证据(静态规则建议)】 【证据缺失】知识检索不可用。")
    # ⑧ 证据清单 + ⑨ 结构化判断
    user_lines.append("【证据清单】")
    for rec in evidence_records:
        user_lines.append("[seq=%s] %s(%s,%s) %s %s" % (rec["seq"], rec["evidence_id"],
                                                        rec["evidence_type"], rec["source_marker"],
                                                        rec["summary"], rec["status"]))
    user_lines.append("【结构化判断(权威,不可更改)】")
    user_lines.append(json.dumps(judgment, ensure_ascii=False))
    user = "\n".join(user_lines)[:USER_CONTENT_CAP]
    payload = {"model": B07_LLM_MODEL,
               "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": user}],
               "temperature": 0.3}
    try:
        resp = requests.post(B07_LLM_BASE_URL,
                             headers={"Authorization": "Bearer %s" % key,
                                      "Content-Type": "application/json"},
                             json=payload, timeout=B07_LLM_TIMEOUT)
    except requests.exceptions.Timeout:
        return {"status": "omitted", "text": None,
                "note": "network_timeout: LLM 接口超时(>%g 秒)" % B07_LLM_TIMEOUT}
    except requests.exceptions.ConnectionError:
        return {"status": "omitted", "text": None, "note": "network_timeout: 网络连接失败"}
    except Exception as e:
        return {"status": "omitted", "text": None, "note": "network_timeout: 网络异常: %s" % e}
    if resp.status_code in (401, 403):
        return {"status": "omitted", "text": None,
                "note": "api_key_error: HTTP %d 认证失败,API Key 无效或已过期" % resp.status_code}
    if resp.status_code != 200:
        return {"status": "omitted", "text": None,
                "note": "http_error: LLM 接口返回 HTTP %d" % resp.status_code}
    try:
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        return {"status": "omitted", "text": None, "note": "bad_response: LLM 返回结构非法"}
    if not isinstance(text, str) or not text.strip():
        return {"status": "omitted", "text": None, "note": "bad_response: LLM 返回内容为空"}
    return {"status": "success", "text": text.strip(), "note": None}


# ---------------- 案例A 第二遍:事件标注 / 证据关系 / LLM 输出校验 / 溯源 ----------------
def find_persistent_drift_start(rows, key, delta):
    """持续漂移起点(确定性):首个样本 i,满足 |rows[i][key]-rows[0][key]| ≥ delta
    且其后所有样本 j>i 与首样本的累计差保持同方向且 ≥ delta。
    返回 (direction, start_row, cumulative_delta) 或 None。纯数值比较,无墙钟无随机。"""
    if not rows or len(rows) < 2:
        return None
    base = rows[0][key]
    n = len(rows)
    for i in range(n):
        d = rows[i][key] - base
        if d >= delta and all(rows[j][key] - base >= delta for j in range(i, n)):
            return ("up", rows[i], round(d, 4))
        if d <= -delta and all(rows[j][key] - base <= -delta for j in range(i, n)):
            return ("down", rows[i], round(d, 4))
    return None


def compute_events(cfg, judgment, tool_results, sources, anchor_text, anchor_dt):
    """视图2 关键事件标注(运行时计算、存入 trace,视图只渲染):
    trend_start(四参数漂移起点)/ model_watch / weather(或 weather_missing)/
    attention_raised / rule_hit。全部字段来自存档数据 → 可复现。"""
    events = []
    note = None
    by_tool = {tr["tool"]: tr for tr in tool_results}
    daylight = DAYLIGHT_RANGE[0] <= anchor_dt.hour < DAYLIGHT_RANGE[1]
    hist_rows = by_tool.get("b02_history", {}).get("result")
    if not sources["history"]:
        note = "数据源 history=false,无历史时序与趋势事件(视图2 空态)"
    elif not (isinstance(hist_rows, list) and len(hist_rows) >= 2):
        note = "历史样本不足 2 点,无趋势事件(降级)"
    else:
        drift_delta = {"temperature": 0.5, "humidity": 1.0, "co2": 25.0, "light": 2000.0}
        for key in PARAM_KEYS:
            if key == "light" and not daylight:
                continue   # 夜间光照漂移不标注(规则同趋势判定)
            hit = find_persistent_drift_start(hist_rows, key, drift_delta[key])
            if hit:
                direction, row, cum = hit
                label = "开始持续%s(%+g,已发生)" % ("升温" if direction == "up" else "降温",
                                                    cum) if key == "temperature" else \
                        "开始持续%s(%+g,已发生)" % ("上升" if direction == "up" else "下降", cum)
                events.append({"event_id": "EVT%02d" % (len(events) + 1), "type": "trend_start",
                               "parameter": key, "direction": direction,
                               "sample_time": row["sample_time"], "label": label,
                               "note": "首个满足累计变化≥阈值且其后保持同方向的样本时刻(已发生事实)"})
    anchor_ok = bool(by_tool.get("b02_anchor", {}).get("status") == "ok" and by_tool.get("b02_anchor", {}).get("result"))
    if not anchor_ok:
        note = (note + ";" if note else "") + "锚点样本缺失,锚点事件标注不可用(降级)"
    else:
        model_ev = by_tool.get("b06_predict", {}).get("result")
        if sources["prediction"] and isinstance(model_ev, dict) and model_ev.get("status") == "ok" \
                and model_ev.get("prediction") == "WATCH":
            prob = (model_ev.get("probability") or {}).get("WATCH")
            events.append({"event_id": "EVT%02d" % (len(events) + 1), "type": "model_watch",
                           "parameter": None, "sample_time": anchor_text,
                           "label": "B06 预测 WATCH(P=%s,未来1~2h窗口)" % prob,
                           "note": "模型短时推演由 NORMAL 转为 WATCH(未来,非已发生)"})
        weather_ev = by_tool.get("b03_weather", {}).get("result")
        if sources["weather"] != "off":
            if isinstance(weather_ev, dict) and weather_ev.get("status") == "success":
                injected = bool(weather_ev.get("simulated"))
                t = (weather_ev.get("temperature") or {}).get("value", "—")
                events.append({"event_id": "EVT%02d" % (len(events) + 1), "type": "weather",
                               "parameter": None, "sample_time": anchor_text,
                               "label": ("天气证据获取(实验注入 SIMULATED:预报 %s℃ 降温,未来外部条件)" % t)
                                        if injected else "外部天气证据获取(未来外部条件)",
                               "note": "weather_evidence 获取于锚点时刻"})
            else:
                events.append({"event_id": "EVT%02d" % (len(events) + 1), "type": "weather_missing",
                               "parameter": None, "sample_time": anchor_text,
                               "label": "天气证据缺失(判断边界收窄)", "note": "天气证据获取失败,外部条件不参与判断"})
        if judgment.get("attention_level") not in (None, "normal"):
            events.append({"event_id": "EVT%02d" % (len(events) + 1), "type": "attention_raised",
                           "parameter": None, "sample_time": anchor_text,
                           "label": "系统提升关注等级(%s)" % judgment.get("attention_level"),
                           "note": "结构化判断引擎输出关注级提升"})
        if judgment.get("matched_rules"):
            events.append({"event_id": "EVT%02d" % (len(events) + 1), "type": "rule_hit",
                           "parameter": None, "sample_time": anchor_text,
                           "label": "规则命中 %s" % "、".join(r["id"] for r in judgment["matched_rules"]),
                           "note": "阈值表 b03_rules.json R1~R8 判定"})
    events.sort(key=lambda e: (e["sample_time"], {"trend_start": 0, "rule_hit": 1, "model_watch": 2,
                                                  "weather": 3, "weather_missing": 3,
                                                  "attention_raised": 4}.get(e["type"], 5)))
    return {"events": events, "note": note}


def compute_evidence_relations(judgment, evidence_records, sources):
    """每条证据与最终结论的关系(确定性规则表):支持 / 限制 / 中性。
    结论 需要预警/需要关注:趋势 alert、预测 WATCH、历史/当前/原始 ok → 支持;
    趋势无 alert、预测 NORMAL → 限制;天气 ok → 中性(注入降温 → 限制+分歧);
    知识 ok → 中性;error/insufficient/disabled → 限制。
    结论 正常:方向相反(无 alert/NORMAL/平稳 → 支持,alert/WATCH → 限制)。"""
    conclusion = judgment.get("conclusion", "")
    elevated = conclusion in ("需要预警", "需要关注")
    trend_has_alert = any(f.get("alert") for f in judgment.get("trend_findings", []))
    relations = {}
    for rec in evidence_records:
        et = rec.get("evidence_type")
        reason = None
        if rec["status"] in ("error", "insufficient", "disabled"):
            relation = "限制"
            reason = {"error": "证据失败,不能支撑结论", "insufficient": "证据不足,结论确定性受限",
                      "disabled": "数据源配置关闭,判断边界收窄"}[rec["status"]]
        elif et in ("trend",):
            alert = trend_has_alert
            if elevated:
                relation, reason = ("支持", "趋势预警(已发生事实)支撑关注级提升") if alert \
                    else ("限制", "趋势平稳,不支持关注级提升")
            else:
                relation, reason = ("限制", "趋势预警与「正常」结论方向相反") if alert \
                    else ("支持", "趋势平稳支撑「正常」结论")
        elif et == "prediction":
            pred = (rec.get("detail") or {}).get("prediction")
            if pred == "WATCH":
                relation, reason = ("支持", "模型预测 WATCH(未来1~2h短时推演)支撑关注级提升") if elevated \
                    else ("限制", "模型预测 WATCH 与「正常」结论方向相反")
            else:
                relation, reason = ("限制", "模型预测 NORMAL,不支持关注级提升") if elevated \
                    else ("支持", "模型预测 NORMAL 支撑「正常」结论")
        elif et == "weather":
            injected = bool((rec.get("detail") or {}).get("simulated"))
            if injected:
                relation, reason = "限制", "实验注入降温预报(未来外部条件)与历史升温方向相反——保留分歧"
            else:
                relation, reason = "中性", "外部天气为未来外部条件,不直接决定棚内当前判断"
        elif et == "knowledge":
            relation, reason = "中性", "静态规则建议,不直接决定当前结论"
        elif et == "history":
            relation, reason = ("支持", "历史窗口(已发生事实)为判断提供时间上下文") if elevated \
                else ("支持", "历史窗口(已发生事实)支撑「正常」结论")
        elif et in ("current", "raw"):
            relation, reason = ("支持", "锚点当前事实为判断基准") if elevated \
                else ("支持", "锚点当前事实支撑「正常」结论")
        else:
            relation, reason = "中性", "上下文证据"
        rec["relation"] = relation
        rec["relation_reason"] = reason
        relations[rec["evidence_id"]] = (relation, reason)
    return relations


def validate_llm_output(text, judgment, sources, tool_results, evidence_records):
    """案例A 第二遍 LLM 输出校验(纯函数,零 LLM 零网络):
    ① 强确定词扫描(证据不足时 fail,充分时 pass+warning);
    ② 天气编造(weather off 或无 b03 成功调用但出现天气断言 → fail);
    ③ B05 工具调用校验(声称某来源结果但对应工具无成功记录 → fail);
    ④ 编造已关闭来源(history/prediction off 但文本声称 → fail)。
    LLM 缺失(text=None)→ status=skip,结构类检查(②③中不依赖文本的分支)仍执行。"""
    items = []
    strong_words = []
    evidence_sufficient = (not judgment.get("degraded_reasons") and not judgment.get("source_conflicts")
                           and not (judgment.get("evidence_completeness") or {}).get("disabled")
                           and judgment.get("trust_level") == "高")
    if text:
        for w in STRONG_WORDS:
            if STRONG_WORD_PATTERNS[w].search(text):
                strong_words.append(w)
        if strong_words:
            if evidence_sufficient:
                items.append({"check": "强确定词", "result": "pass_warning",
                              "detail": "含强确定词 %s;证据充分,可接受,仍建议替换为不确定性表述" % "、".join(strong_words)})
            else:
                items.append({"check": "强确定词", "result": "fail",
                              "detail": "证据不足(降级/分歧/来源关闭)仍使用强确定词: %s" % "、".join(strong_words)})
        else:
            items.append({"check": "强确定词", "result": "pass", "detail": "未使用强确定词"})
    else:
        items.append({"check": "强确定词", "result": "skip", "detail": "LLM 未生成文本,文本类检查跳过(诚实说明)"})

    by_tool = {tr["tool"]: tr for tr in tool_results}
    weather_call_ok = bool(by_tool.get("b03_weather", {}).get("status") == "ok"
                           and isinstance(by_tool["b03_weather"].get("result"), dict)
                           and by_tool["b03_weather"]["result"].get("status") == "success")
    if text and WEATHER_CLAIM_RE.search(text):
        if sources["weather"] == "off":
            items.append({"check": "天气不编造", "result": "fail",
                          "detail": "文本断言天气信息,但天气数据源配置关闭(weather=off)"})
        elif not weather_call_ok:
            items.append({"check": "天气不编造", "result": "fail",
                          "detail": "文本声称根据天气信息,但工具调用日志无 b03_weather 成功调用"})
        else:
            injected = bool(by_tool["b03_weather"]["result"].get("simulated"))
            items.append({"check": "天气不编造", "result": "pass_warning" if injected else "pass",
                          "detail": ("天气断言与 b03 调用记录一致;注入证据未标注「模拟/实验」,建议复核" if injected
                                     and "注入" not in text and "模拟" not in text else "天气断言与 b03 调用记录一致")})
    elif sources["weather"] == "off":
        items.append({"check": "天气不编造", "result": "pass", "detail": "无天气断言(weather=off,边界已收窄)"})
    else:
        items.append({"check": "天气不编造", "result": "pass" if weather_call_ok else "skip",
                      "detail": "无天气断言" if weather_call_ok else "无天气断言;天气调用未成功(证据缺失已如实标注)"})

    if text:
        for claim_re, tools, label in TOOL_CLAIM_RULES:
            if claim_re.search(text):
                ok_calls = [t for t in tools if by_tool.get(t, {}).get("status") == "ok"]
                if not ok_calls:
                    enabled = any(sources.get({"b02_history": "history", "b02_stats": "history",
                                               "b04_knowledge": "knowledge", "b06_predict": "prediction",
                                               "b03_weather": "weather"}.get(t, ""), True)
                                  and (sources.get("weather") != "off" if t == "b03_weather" else True)
                                  for t in tools)
                    items.append({"check": "工具调用校验", "result": "fail",
                                  "detail": "文本声称使用%s结果,但对应工具(%s)无成功调用记录%s"
                                            % (label, ",".join(tools),
                                               "(来源已配置关闭——编造未调用工具的结果)" if not enabled else "")})
                    break
        else:
            items.append({"check": "工具调用校验", "result": "pass", "detail": "文本中的来源表述均有对应工具调用记录"})
    else:
        items.append({"check": "工具调用校验", "result": "skip", "detail": "LLM 未生成文本,工具调用结构校验跳过"})

    if text:
        fabricated = []
        if not sources["prediction"] and re.search(r"预测|未来.{0,4}窗口|WATCH|NORMAL", text):
            fabricated.append("预测来源已关闭但文本出现预测表述")
        if not sources["history"] and re.search(r"历史|趋势|升温|降温", text):
            fabricated.append("历史来源已关闭但文本出现历史/趋势表述")
        items.append({"check": "关闭来源不编造", "result": "fail" if fabricated else "pass",
                      "detail": ";".join(fabricated) if fabricated else "未引用已关闭的数据源"})
    else:
        items.append({"check": "关闭来源不编造", "result": "skip", "detail": "LLM 未生成文本"})

    failed = [i for i in items if i["result"] == "fail"]
    status = "fail" if failed else ("skip" if text is None else "pass")
    return {"checked_at": _now_iso(), "status": status,
            "items": items, "strong_words": strong_words,
            "verdict": "FAIL" if failed else ("SKIP(LLM 未生成,文本类校验跳过;结构校验见各检查项)"
                                              if text is None else "PASS"),
            "note": "校验基于存档数据,零 LLM 零网络;文本类校验仅对已生成的 LLM 输出执行"}


def trace_conclusion(run_id, conclusion_text):
    """创新点2 溯源接口:输入 run_id+结论 → 返回支撑该结论的全部证据清单
    (证据自报身份 + 支持/限制关系)+ B05 工具调用校验。只读存档,不调 LLM。"""
    run_dir = EVIDENCE_B07_DIR / run_id
    if not run_dir.is_dir():
        return {"error": "运行目录不存在: %s" % run_dir}
    try:
        trace = json.loads((run_dir / "trace.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"error": "trace.json 缺失或损坏: %s" % (run_dir / "trace.json")}
    judgment = trace.get("steps", {}).get("structured_judgment") or {}
    records = trace.get("steps", {}).get("evidence_records") or []
    tool_results = trace.get("steps", {}).get("tool_results") or []
    sources = (trace.get("data_source_config") or {}).get("sources") or _normalize_sources(trace.get("scenario") or {})
    stored = judgment.get("conclusion")
    consistent = (conclusion_text == stored)
    # 按查询结论重算关系(确定性;与存档结论不一致时如实提示)
    tmp_judgment = dict(judgment)
    tmp_judgment["conclusion"] = conclusion_text
    relations = compute_evidence_relations(tmp_judgment, records, sources)
    rows = []
    for rec in records:
        rel, reason = relations.get(rec["evidence_id"], (None, None))
        rows.append({"seq": rec["seq"], "evidence_id": rec["evidence_id"],
                     "evidence_type": rec.get("evidence_type"), "source": rec["source"],
                     "source_marker": rec.get("source_marker"), "evidence_time": rec.get("evidence_time"),
                     "status": rec["status"], "relation": rel, "reason": reason,
                     "summary": rec["summary"]})
    # B05 工具调用校验(文本声称 vs 存档调用记录)
    llm_text = (trace.get("steps", {}).get("explanation") or {}).get("text")
    claim_rows = []
    if llm_text:
        for claim_re, tools, label in TOOL_CLAIM_RULES:
            if claim_re.search(llm_text):
                ok_calls = [t for t in tools if any(tr.get("tool") == t and tr.get("status") == "ok"
                                                    for tr in tool_results)]
                claim_rows.append({"claim": label, "tools": list(tools),
                                   "called_ok": bool(ok_calls), "matched_tools": ok_calls})
    else:
        claim_rows.append({"claim": "(LLM 未生成文本)", "tools": [], "called_ok": None, "matched_tools": []})
    return {"run_id": run_id, "query_conclusion": conclusion_text, "stored_conclusion": stored,
            "consistent": consistent,
            "warning": None if consistent else "查询结论(%s)与存档结论(%s)不一致,证据关系按查询结论重算,仅供参考"
                      % (conclusion_text, stored),
            "evidence": rows, "tool_call_checks": claim_rows}
# ---------------- 单场景完整流程 ----------------
def run_scenario(sid, run_id=None):
    """完整执行一个场景/实验:① 场景载入 → ② 工具结果(按数据源开关分派)→
    ③ 证据记录 → ④ 结构化判断 → ⑤ LLM 解释(分块证据输入)→
    ⑥ 证据关系/事件/LLM 校验 → ⑦ 产物落盘 + HTML 报告。
    返回汇总 dict;任何步骤失败都如实降级,不崩溃。"""
    cfg = load_scenario(sid)
    if "error" in cfg:
        _log_b07("[场景] ⚠ %s" % cfg["error"])
        return None

    if run_id is None:
        run_id = b01_cli.gen_run_id()
    elif not _valid_run_id(run_id):
        _log_b07("[场景] ⚠ --run-id 格式非法(%s),已换用新 run_id" % run_id)
        run_id = b01_cli.gen_run_id()
    run_dir = EVIDENCE_B07_DIR / run_id
    if run_dir.exists():
        _log_b07("[场景] ⚠ evidence/b07/%s 已存在,覆盖重跑" % run_id)

    anchor_text = cfg["anchor_sample_time"]
    anchor_dt = datetime.strptime(anchor_text, "%Y-%m-%d %H:%M:%S")
    history_start = (anchor_dt - timedelta(hours=cfg["history_hours"])).strftime("%Y-%m-%d %H:%M:%S")
    trend_start = (anchor_dt - timedelta(hours=cfg["trend_hours"])).strftime("%Y-%m-%d %H:%M:%S")
    degrade = cfg.get("degrade") or {}
    sources = _normalize_sources(cfg)

    _log_b07("[场景] run_id=%s scenario=%s name=%s anchor=%s experiment=%s sources=history:%s/weather:%s/knowledge:%s/prediction:%s"
             % (run_id, cfg["scenario_id"], cfg["name"], anchor_text, cfg.get("experiment_type") or "无",
                "开" if sources["history"] else "关", sources["weather"],
                "开" if sources["knowledge"] else "关", "开" if sources["prediction"] else "关"))

    # ---- ② 工具结果(真实执行 B01~B06 公开接口;disabled 来源不调用)----
    tool_results = []

    tr = b07_tool_call(1, "b02_current", "SIMULATED", b02_memory.get_current_environment, {})
    tool_results.append(tr)

    tr = b07_tool_call(2, "b02_anchor", "SIMULATED", b02_memory.query_range,
                       {"start": anchor_text, "end": anchor_text})
    tool_results.append(tr)

    # b01_raw:锚点行 → 完整保真的 B01 原始采集记录(collect_time=锚点时间,非墙钟)
    anchor_rows = tr["result"] if tr["status"] == "ok" else []
    if anchor_rows:
        record = {
            "run_id": run_id,
            "collect_time": anchor_dt.astimezone().isoformat(timespec="seconds"),
            "source_type": "SIMULATED",
            "data": {key: {"value": anchor_rows[0][key], "unit": spec["unit"]}
                     for key, spec in zip(PARAM_KEYS, b01_cli.PARAM_SPECS)},
            "valid_ranges": {spec["key"]: {"min": spec["min"], "max": spec["max"], "unit": spec["unit"]}
                             for spec in b01_cli.PARAM_SPECS},
        }
        tool_results.append(b07_tool_call(3, "b01_raw", "SIMULATED", b01_cli.save_record,
                                          {"record": record}))
    else:
        tool_results.append({"seq": 3, "step": "tool_results", "tool": "b01_raw", "marker": "SIMULATED",
                             "arguments": {"skipped": True, "reason": "锚点样本缺失,无数据可存档"},
                             "result": None, "status": "error",
                             "error": {"error_type": "value_error", "message": "锚点样本缺失"},
                             "called_at": _now_iso()})
        _log_b07("[工具] seq=3 tool=b01_raw marker=SIMULATED status=error [nested=value_error]")

    if sources["history"]:
        tool_results.append(b07_tool_call(4, "b02_history", "SIMULATED", b02_memory.query_range,
                                          {"start": history_start, "end": anchor_text}))
        tool_results.append(b07_tool_call(5, "b02_stats", "SIMULATED", b02_memory.stats_window,
                                          {"start": trend_start, "end": anchor_text}))
    else:
        tool_results.append(_disabled_tool_call(4, "b02_history", "SIMULATED", "数据源配置 history=false"))
        tool_results.append(_disabled_tool_call(5, "b02_stats", "SIMULATED", "数据源配置 history=false"))
        _log_b07("[工具] seq=4/5 tool=b02_history/b02_stats marker=SIMULATED status=disabled")

    if sources["weather"] == "real":
        weather_timeout = (0.0001, 0.0001) if degrade.get("weather") == "timeout" else None
        tool_results.append(b07_tool_call(6, "b03_weather", "PUBLIC", b03_weather.get_weather_forecast,
                                          {"location": cfg["location"], "run_id": run_id,
                                           "analysis_time": anchor_text, "timeout": weather_timeout}))
    elif sources["weather"] == "injected_cooling":
        # 实验注入降温天气证据:SIMULATED 标记,自报身份,绝不冒充 PUBLIC
        tool_results.append(b07_tool_call(6, "b03_weather", "SIMULATED", inject_cooling_weather,
                                          {"cfg": cfg, "anchor_text": anchor_text}))
    else:
        tool_results.append(_disabled_tool_call(6, "b03_weather", "PUBLIC", "数据源配置 weather=off"))
        _log_b07("[工具] seq=6 tool=b03_weather marker=PUBLIC status=disabled")

    if sources["knowledge"]:
        tool_results.append(b07_tool_call(7, "b04_knowledge", "SIMULATED", b04_rag.search_tomato_knowledge,
                                          {"query": cfg["knowledge_query"], "k": 5}))
    else:
        tool_results.append(_disabled_tool_call(7, "b04_knowledge", "SIMULATED", "数据源配置 knowledge=false"))
        _log_b07("[工具] seq=7 tool=b04_knowledge marker=SIMULATED status=disabled")

    if sources["prediction"]:
        tool_results.append(b07_tool_call(8, "b06_predict", "MODEL_OUTPUT", b06_ml.predict_next_attention,
                                          {"sample_time": anchor_text}))
    else:
        tool_results.append(_disabled_tool_call(8, "b06_predict", "MODEL_OUTPUT", "数据源配置 prediction=false"))
        _log_b07("[工具] seq=8 tool=b06_predict marker=MODEL_OUTPUT status=disabled")

    # ---- ③ 证据记录(与工具结果分离)----
    evidence_records = build_evidence_records(tool_results, cfg, run_id, anchor_text)
    for rec in evidence_records:
        _log_b07("[证据] seq=%d evidence_id=%s source=%s marker=%s status=%s type=%s" % (
            rec["seq"], rec["evidence_id"], rec["source"], rec["source_marker"], rec["status"],
            rec.get("evidence_type")))

    # ---- ④ 结构化判断(保存前先落盘,LLM 解释不覆盖)----
    by_tool = {tr["tool"]: tr for tr in tool_results}
    rules, rules_version = load_rules()
    judgment = judge_environment(
        cfg, run_id, anchor_text, anchor_dt,
        anchor_rows[0] if anchor_rows else None,
        by_tool.get("b02_stats", {}).get("result") if by_tool.get("b02_stats") else None,
        by_tool.get("b06_predict", {}).get("result") if by_tool.get("b06_predict") else None,
        by_tool.get("b03_weather", {}).get("result") if by_tool.get("b03_weather") else None,
        by_tool.get("b04_knowledge", {}).get("result") if by_tool.get("b04_knowledge") else None,
        rules, sources)
    if rules_version:
        judgment["rule_engine"]["rules_version"] = rules_version
    _log_b07("[判断] conclusion=%s attention_level=%s trust=%s matched_rules=%s degraded=%d completeness=%s/4"
             % (judgment["conclusion"], judgment["attention_level"], judgment["trust_level"],
                ",".join(r["id"] for r in judgment["matched_rules"]) or "无",
                len(judgment["degraded_reasons"]), judgment["evidence_completeness"]["available"]))

    # ---- ⑤ LLM 解释(判断已定型,解释仅存 trace)----
    explanation = explain_judgment(cfg, judgment, evidence_records, sources, anchor_rows, tool_results)
    if explanation["status"] == "success":
        _log_b07("[解释] status=success chars=%d" % len(explanation["text"]))
    elif explanation["status"] == "skipped":
        _log_b07("[解释] status=skipped reason=%s" % explanation["note"])
    else:
        _log_b07("[解释] status=omitted reason=%s" % explanation["note"])

    # ---- ⑥ 证据关系 / 事件标注 / LLM 输出校验(案例A 第二遍)----
    compute_evidence_relations(judgment, evidence_records, sources)
    events = compute_events(cfg, judgment, tool_results, sources, anchor_text, anchor_dt)
    llm_validation = validate_llm_output(explanation.get("text"), judgment, sources, tool_results, evidence_records)
    _log_b07("[校验] verdict=%s strong_words=%d items=%s" % (
        llm_validation["verdict"], len(llm_validation["strong_words"]),
        ",".join("%s:%s" % (i["check"], i["result"]) for i in llm_validation["items"])))

    # ---- ⑦ 产物落盘 + HTML 报告 ----
    raw_saved = {"path": "evidence/raw/%s.json" % run_id,
                 "ok": bool(anchor_rows and by_tool.get("b01_raw", {}).get("result") is True),
                 "collect_time": anchor_dt.astimezone().isoformat(timespec="seconds"),
                 "source_type": "SIMULATED"}
    trace = {"run_id": run_id, "trace_version": "1.1", "generated_at": _now_iso(), "scenario": cfg,
             "data_source_config": {"experiment_type": cfg.get("experiment_type"), "sources": sources},
             "steps": {"tool_results": tool_results, "evidence_records": evidence_records,
                       "structured_judgment": judgment, "explanation": explanation,
                       "events": events, "llm_validation": llm_validation,
                       "raw_record_saved": raw_saved},
             "summary": {"conclusion": judgment["conclusion"],
                         "attention_level": judgment["attention_level"],
                         "trust_level": judgment["trust_level"],
                         "matched_rules": [r["id"] for r in judgment["matched_rules"]],
                         "degraded_reasons": judgment["degraded_reasons"]}}
    _save_json(run_dir / "trace.json", trace)
    _save_json(run_dir / "structured_judgment.json", judgment)
    _save_json(run_dir / "evidence_log.json",
               {"run_id": run_id, "generated_at": _now_iso(), "evidence_records": evidence_records})
    _save_json(run_dir / "scenario.json", {
        "run_id": run_id, "config": cfg,
        "actuals": {"conclusion": judgment["conclusion"], "attention_level": judgment["attention_level"],
                    "trust_level": judgment["trust_level"],
                    "matched_rules": [r["id"] for r in judgment["matched_rules"]],
                    "degraded_reasons": judgment["degraded_reasons"]},
        "expected": cfg.get("expected") or {}})
    # ---- ⑦+ run_result.json(B08 契约校验唯一输入)----
    # parsed.history 嵌入 b02_history 原始行(不加工):保证 b08 校验
    # "history 末行==anchor"(query_range 闭区间含锚点,恒成立)。
    # history 数据源关闭时退用锚点行(单行):b08 契约不破坏。
    history_rows = by_tool.get("b02_history", {}).get("result")
    if not isinstance(history_rows, list):
        history_rows = []
    if sources["history"] is False and anchor_rows:
        history_rows = [anchor_rows[0]]
    raw_ok = bool(anchor_rows and by_tool.get("b01_raw", {}).get("result") is True)
    raw_record_loaded = None
    if raw_ok:
        try:
            # 回读盘上真实文件:保证 b08 所见即落盘内容;--run-id 复用重跑时
            # 只有本次 save_record 成功才回读,避免嵌入同名旧文件。
            raw_record_loaded = json.loads(
                (b01_cli.RAW_DIR / ("%s.json" % run_id)).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw_ok = False
    run_result = {
        "run_id": run_id,
        "scenario_id": cfg["scenario_id"],
        "generated_at": _now_iso(),
        "data_source_config": {"experiment_type": cfg.get("experiment_type"), "sources": sources},
        "raw": {"status": "ok" if raw_ok else "missing",
                "path": "evidence/raw/%s.json" % run_id,
                "record": raw_record_loaded},
        "parsed": {"status": "ok" if anchor_rows else "missing",
                   "source": "b02_memory environment_samples",
                   "anchor": anchor_rows[0] if anchor_rows else None,
                   "history_count": len(history_rows),
                   "history": history_rows,
                   "grid_step_minutes": b02_memory.GRID_STEP_MINUTES},
        "rule": None, "ml": None, "final": None,
    }
    _save_json(run_dir / "run_result.json", run_result)
    # 注入天气证据单独落盘副本(不写 evidence/b03/,B03 实证目录零污染)
    if sources["weather"] == "injected_cooling":
        injected_ev = by_tool.get("b03_weather", {}).get("result")
        if isinstance(injected_ev, dict):
            _save_json(run_dir / "weather_evidence.json", injected_ev)

    b07_report.render_report(run_dir, run_id)

    report_path = "report/%s_report.html" % run_id
    _log_b07("[完成] run_id=%s scenario=%s report=%s run_result=evidence/b07/%s/run_result.json"
             % (run_id, cfg["scenario_id"], report_path, run_id))
    return {"run_id": run_id, "scenario_id": cfg["scenario_id"], "name": cfg["name"],
            "conclusion": judgment["conclusion"], "attention_level": judgment["attention_level"],
            "trust_level": judgment["trust_level"],
            "degraded_count": len(judgment["degraded_reasons"]), "report": report_path,
            "experiment_type": cfg.get("experiment_type"),
            "evidence_completeness": judgment["evidence_completeness"],
            "llm_validation": llm_validation["verdict"]}


# ---------------- CLI 子命令 ----------------
def print_verdict(summary):
    print("  ✓ %s %s → 结论=%s 关注级=%s 信任=%s 降级=%d" % (
        summary["scenario_id"], summary["name"], summary["conclusion"],
        summary["attention_level"], summary["trust_level"], summary["degraded_count"]))
    print("    run_id=%s  report=%s" % (summary["run_id"], summary["report"]))


def cmd_run(args):
    run_id = None
    rest = []
    i = 0
    while i < len(args):
        if args[i] == "--run-id" and i + 1 < len(args):
            run_id = args[i + 1]
            i += 2
        else:
            rest.append(args[i])
            i += 1
    if not rest:
        print("用法: py b07_main.py run <S1~S5> [--run-id run_xxx]")
        return
    summary = run_scenario(rest[0], run_id=run_id)
    if summary:
        print_verdict(summary)


def cmd_run_all():
    files = sorted(SCENARIOS_DIR.glob("*.json")) if SCENARIOS_DIR.exists() else []
    if not files:
        print("⚠ 场景配置目录为空: %s" % SCENARIOS_DIR)
        return
    summaries = []
    for i, p in enumerate(files, start=1):
        print("\n========== 场景 %d/%d ==========" % (i, len(files)))
        summary = run_scenario(p.stem)
        if summary:
            summaries.append(summary)
            print_verdict(summary)
    if summaries:
        print("\n================ S1~S5 场景回放对比(本轮) ================")
        print("%-6s %-12s %-8s %-8s %-4s %-4s %s" % ("场景", "run_id", "结论", "关注级", "信任", "降级", "报告"))
        for s in summaries:
            print("%-6s %-12s %-8s %-8s %-4s %-4d %s" % (
                s["scenario_id"], s["run_id"], s["conclusion"], s["attention_level"],
                s["trust_level"], s["degraded_count"], s["report"]))


def cmd_report(args):
    if len(args) != 1 or not _valid_run_id(args[0]):
        print("用法: py b07_main.py report <run_id>(时间戳格式或实验格式 run_<type>_<seq>)")
        return
    run_dir = EVIDENCE_B07_DIR / args[0]
    if not run_dir.is_dir():
        print("⚠ 运行目录不存在: %s(请先执行 py b07_main.py run <场景> 或 exp <type>)" % run_dir)
        return
    b07_report.render_report(run_dir, args[0])


def cmd_list():
    rows = []
    if EVIDENCE_B07_DIR.exists():
        for sub in EVIDENCE_B07_DIR.iterdir():
            if not sub.is_dir():
                continue
            try:
                j = json.loads((sub / "structured_judgment.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            rows.append({"run_id": sub.name, "scenario_id": j.get("scenario_id", "?"),
                         "name": j.get("scenario_name", "?"), "conclusion": j.get("conclusion", "—"),
                         "attention_level": j.get("attention_level", "—"),
                         "trust_level": j.get("trust_level", "—"), "judged_at": j.get("judged_at", "—"),
                         "experiment_type": j.get("experiment_type") or "—"})
    rows.sort(key=lambda r: r["run_id"], reverse=True)
    if not rows:
        print("暂无历史运行记录,请先执行 py b07_main.py run S1")
        return
    print("\n================ 历史运行列表(evidence/b07,按时间倒序) ================")
    print("%-14s %-6s %-12s %-8s %-8s %-4s %-12s %s" % (
        "run_id", "场景", "名称", "结论", "关注级", "信任", "实验类型", "判断时间"))
    for r in rows:
        print("%-14s %-6s %-12s %-8s %-8s %-4s %-12s %s" % (
            r["run_id"], r["scenario_id"], r["name"], r["conclusion"],
            r["attention_level"], r["trust_level"], r["experiment_type"], r["judged_at"]))


# ---------------- 案例A 第二遍:实验命令 ----------------
def cmd_exp(args):
    if len(args) != 1 or args[0] not in EXPERIMENT_TYPES:
        print("用法: py b07_main.py exp <%s>" % "|".join(EXPERIMENT_TYPES))
        return
    etype = args[0]
    run_id = "run_%s_%03d" % (etype, _next_exp_seq(etype))
    summary = run_scenario("exp_%s" % etype, run_id=run_id)
    if summary:
        print_verdict(summary)


def cmd_exp_all():
    summaries = []
    for i, etype in enumerate(EXPERIMENT_TYPES, start=1):
        print("\n========== 实验 %d/%d ==========" % (i, len(EXPERIMENT_TYPES)))
        summary = run_scenario("exp_%s" % etype, run_id="run_%s_%03d" % (etype, _next_exp_seq(etype)))
        if summary:
            summaries.append(summary)
            print_verdict(summary)
    if not summaries:
        return
    groups = {
        "创新点3 证据分歧/缺失": ["agree", "conflict", "missing_weather"],
        "创新点1 连续时间认知": ["current_only", "with_history", "with_history_prediction"],
    }
    for title, types in groups.items():
        print("\n================ %s 对比表 ================" % title)
        print("%-22s %-8s %-8s %-4s %-4s %-8s %s" % (
            "实验", "结论", "关注级", "信任", "降级", "完整度", "LLM校验"))
        for t in types:
            s = next((x for x in summaries if x.get("experiment_type") == t), None)
            if not s:
                print("%-22s (未生成)" % t)
                continue
            comp = s.get("evidence_completeness") or {}
            print("%-22s %-8s %-8s %-4s %-4d %-8s %s" % (
                "%s(%s)" % (t, s["run_id"]), s["conclusion"], s["attention_level"], s["trust_level"],
                s["degraded_count"], "%s/4" % comp.get("available"), s.get("llm_validation")))


def cmd_trace(args):
    if len(args) < 2:
        print("用法: py b07_main.py trace <run_id> \"<结论文本>\"")
        return
    run_id, conclusion_text = args[0], args[1]
    result = trace_conclusion(run_id, conclusion_text)
    if "error" in result:
        print("⚠ %s" % result["error"])
        return
    if result["warning"]:
        print("⚠ %s" % result["warning"])
    print("run_id=%s  存档结论=%s(查询结论与存档%s)" % (
        result["run_id"], result["stored_conclusion"], "一致" if result["consistent"] else "不一致"))
    print("证据清单(支撑「%s」):" % conclusion_text)
    print("  %-3s %-11s %-12s %-14s %-24s %-9s %-4s %s" % (
        "seq", "evidence_id", "evidence_type", "来源标记", "自报时间", "状态", "关系", "理由"))
    for r in result["evidence"]:
        print("  %-3s %-11s %-12s %-14s %-24s %-9s %-4s %s" % (
            r["seq"], r["evidence_id"], r["evidence_type"] or "—", r["source_marker"],
            str(r["evidence_time"] or "—")[:24], r["status"], r["relation"] or "—", r["reason"] or ""))
    print("B05 工具调用校验(基于存档 tool_results):")
    for c in result["tool_call_checks"]:
        if c["called_ok"] is None:
            print("  — %s" % c["claim"])
        elif c["called_ok"]:
            print("  ✓ 「%s」表述 → %s 有成功调用记录" % (c["claim"], ",".join(c["matched_tools"])))
        else:
            print("  ✗ 「%s」表述 → 工具 %s 无成功调用记录(编造未调用工具的结果)" % (c["claim"], ",".join(c["tools"])))


def _load_run_archive(run_id):
    """读取 run 存档四件套(只读,不调 LLM/API);缺失返回 None。"""
    run_dir = EVIDENCE_B07_DIR / run_id
    if not run_dir.is_dir():
        return None
    try:
        trace = json.loads((run_dir / "trace.json").read_text(encoding="utf-8"))
        judgment = json.loads((run_dir / "structured_judgment.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return run_dir, trace, judgment


def _check_one(run_id):
    """对一个 run 执行四项自检,返回 rows(每项 (名称, 结果, 说明))。"""
    loaded = _load_run_archive(run_id)
    if loaded is None:
        return [("存档读取", "FAIL", "运行目录或 trace/structured_judgment 缺失: %s" % run_id)], None
    run_dir, trace, judgment = loaded
    explanation = (trace.get("steps", {}).get("explanation") or {})
    llm_text = explanation.get("text")
    sources = (trace.get("data_source_config") or {}).get("sources") or {}
    tool_results = trace.get("steps", {}).get("tool_results") or []
    etype = judgment.get("experiment_type")
    rows = []

    # [1/4] 分歧保留(conflict)
    if etype == "conflict":
        has_dispute = any("分歧" in r for r in judgment.get("degraded_reasons", [])) \
            or bool(judgment.get("source_conflicts"))
        if not has_dispute:
            rows.append(("分歧保留", "FAIL", "degraded_reasons/source_conflicts 未包含「分歧」"))
        elif llm_text:
            strong_hits = [w for w in STRONG_WORDS if STRONG_WORD_PATTERNS[w].search(llm_text)]
            both_sides = ("升温" in llm_text or "上升" in llm_text) and ("降温" in llm_text or "下降" in llm_text)
            if strong_hits:
                rows.append(("分歧保留", "FAIL", "LLM 使用了强确定词: %s" % "、".join(strong_hits)))
            elif not both_sides:
                rows.append(("分歧保留", "FAIL", "LLM 未同时表述冲突双方(升温/降温)"))
            else:
                rows.append(("分歧保留", "PASS", "存档含「分歧」×%d;LLM 保留双方表述,强词 0" % len(
                    [r for r in judgment.get("degraded_reasons", []) if "分歧" in r])))
        else:
            rows.append(("分歧保留", "SKIP", "存档结构含分歧;LLM 未生成文本,文本类检查跳过(诚实说明)"))
    else:
        rows.append(("分歧保留", "SKIP", "本 run 非 conflict 实验(%s)" % (etype or "无实验类型")))

    # [2/4] 天气不编造(missing_weather)
    if etype == "missing_weather":
        b03_calls = [tr for tr in tool_results if tr.get("tool") == "b03_weather" and tr.get("status") == "ok"]
        ok = not b03_calls
        if not ok:
            rows.append(("天气不编造", "FAIL", "trace 中存在 b03_weather 成功调用(应无)"))
        elif llm_text and WEATHER_CLAIM_RE.search(llm_text):
            rows.append(("天气不编造", "FAIL", "LLM 文本出现天气断言但天气来源关闭"))
        elif llm_text:
            rows.append(("天气不编造", "PASS", "无 b03 调用;LLM 文本无天气断言"))
        else:
            rows.append(("天气不编造", "PASS", "无 b03 调用;LLM 未生成文本(结构检查通过,文本检查跳过)"))
    else:
        rows.append(("天气不编造", "SKIP", "本 run 非 missing_weather 实验(%s)" % (etype or "无实验类型")))

    # [3/4] 视图一致性(同一 run_id,四次视图读同一存档,不产生新 LLM 回答)
    try:
        import hashlib as _hl
        report_path = BASE_DIR / "report" / ("%s_report.html" % run_id)
        b07_report.render_report(run_dir, run_id)
        h1 = _hl.sha256(report_path.read_bytes()).hexdigest()
        b07_report.render_report(run_dir, run_id)
        h2 = _hl.sha256(report_path.read_bytes()).hexdigest()
        html = report_path.read_text(encoding="utf-8")
        # 运行时网络加载构造检测(存档文本中出现 API 历史 URL 字串属合法证据数据,不算外部引用)
        net_patterns = [r'<script[^>]+src=["\']http', r'<link[^>]+href=["\']http',
                        r'<img[^>]+src=["\']http', r'fetch\s*\(', r'XMLHttpRequest',
                        r'new\s+WebSocket', r'url\(\s*["\']?http', r'@import\s+["\']http']
        net_hits = [p for p in net_patterns if re.search(p, html)]
        if h1 != h2:
            rows.append(("视图一致性", "FAIL", "报告两次重生成 sha256 不一致(非确定性)"))
        elif net_hits:
            rows.append(("视图一致性", "FAIL", "HTML 含运行时网络加载构造(视图必须只读存档): %s" % net_hits[0]))
        elif llm_text and llm_text[:40] not in html:
            rows.append(("视图一致性", "FAIL", "HTML 内嵌 LLM 文本与存档 explanation.text 不一致(产生了新回答?)"))
        else:
            rows.append(("视图一致性", "PASS", "两次重生成 sha256 一致(短 %s);HTML 内嵌 LLM 文本与存档一致;无运行时网络加载" % h1[:12]))
    except Exception as e:
        rows.append(("视图一致性", "FAIL", "报告重生成异常: %s" % e))

    # [4/4] 溯源自检
    stored = judgment.get("conclusion", "")
    tr_result = trace_conclusion(run_id, stored)
    if "error" in tr_result:
        rows.append(("溯源自检", "FAIL", tr_result["error"]))
    else:
        n_support = sum(1 for r in tr_result["evidence"] if r["relation"] == "支持")
        n_limit = sum(1 for r in tr_result["evidence"] if r["relation"] == "限制")
        n_neutral = sum(1 for r in tr_result["evidence"] if r["relation"] == "中性")
        bad_claims = [c for c in tr_result["tool_call_checks"]
                      if c["called_ok"] is False]
        if not tr_result["consistent"]:
            rows.append(("溯源自检", "FAIL", "查询结论与存档结论不一致(应一致)"))
        elif bad_claims:
            rows.append(("溯源自检", "FAIL", "工具调用校验失败: %s" % ",".join(c["claim"] for c in bad_claims)))
        else:
            rows.append(("溯源自检", "PASS", "结论「%s」→ %d 条证据(支持%d/限制%d/中性%d);工具调用校验通过"
                         % (stored, len(tr_result["evidence"]), n_support, n_limit, n_neutral)))
    return rows, judgment


def _print_check_rows(run_id, rows):
    print("\n================ B07 实验自检: %s ================" % run_id)
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    for i, (name, result, detail) in enumerate(rows, start=1):
        counts[result] = counts.get(result, 0) + 1
        mark = {"PASS": "✓ PASS", "FAIL": "✗ FAIL", "SKIP": "— SKIP"}[result]
        print("  [%d/4] %-16s %-8s %s" % (i, name, mark, detail))
    print("  汇总: %d PASS / %d FAIL / %d SKIP" % (counts["PASS"], counts["FAIL"], counts["SKIP"]))
    return counts


def cmd_check(args):
    if not args:
        print("用法: py b07_main.py check <run_id> [<run_id> ...]")
        return
    for run_id in args:
        rows, _j = _check_one(run_id)
        _print_check_rows(run_id, rows)


def cmd_check_all():
    run_ids = []
    if EVIDENCE_B07_DIR.exists():
        run_ids = sorted((sub.name for sub in EVIDENCE_B07_DIR.iterdir()
                          if sub.is_dir() and EXP_RUN_ID_RE.match(sub.name)))
    if not run_ids:
        print("暂无实验 run,请先执行 py b07_main.py exp-all")
        return
    table = []
    for run_id in run_ids:
        rows, judgment = _check_one(run_id)
        counts = _print_check_rows(run_id, rows)
        table.append({"run_id": run_id, "type": (judgment or {}).get("experiment_type") or "—",
                      "conclusion": (judgment or {}).get("conclusion", "—"),
                      "trust": (judgment or {}).get("trust_level", "—"),
                      "counts": counts})
    print("\n================ check-all 汇总表 ================")
    print("%-22s %-22s %-8s %-4s %s" % ("run_id", "实验类型", "结论", "信任", "PASS/FAIL/SKIP"))
    for t in table:
        print("%-22s %-22s %-8s %-4s %d/%d/%d" % (
            t["run_id"], t["type"], t["conclusion"], t["trust"],
            t["counts"]["PASS"], t["counts"]["FAIL"], t["counts"]["SKIP"]))


def cmd_selftest():
    """校验函数负向自测(11 项,零 LLM 零网络)。"""
    results = []
    healthy = {"conclusion": "正常", "attention_level": "normal", "trust_level": "高",
               "degraded_reasons": [], "source_conflicts": [],
               "evidence_completeness": {"disabled": []}, "trend_findings": [], "source_findings": []}
    degraded = dict(healthy); degraded["degraded_reasons"] = ["x"]; degraded["trust_level"] = "低"
    sources_all = {"history": True, "weather": "real", "knowledge": True, "prediction": True}
    no_tools = []

    def t(name, ok, detail):
        results.append((name, ok, detail))

    r = validate_llm_output("温度一定继续上升", degraded, sources_all, no_tools, [])
    t("强词+证据不足", r["status"] == "fail" and r["strong_words"] == ["一定"], str(r["strong_words"]))
    r = validate_llm_output("温度一定适宜", healthy, sources_all, no_tools, [])
    t("强词+证据充分", r["status"] == "pass" and any(i["result"] == "pass_warning" for i in r["items"]), r["verdict"])
    r = validate_llm_output("根据天气预报明天降温", healthy, {"history": True, "weather": "off", "knowledge": True, "prediction": True}, no_tools, [])
    t("天气编造(off)", r["status"] == "fail", r["verdict"])
    r = validate_llm_output("根据天气预报明天降温", healthy, sources_all, no_tools, [])
    t("天气编造(无调用)", r["status"] == "fail", r["verdict"])
    r = validate_llm_output("历史数据显示持续升温", healthy, {"history": False, "weather": "real", "knowledge": True, "prediction": True}, no_tools, [])
    t("编造已关历史", r["status"] == "fail", r["verdict"])
    r = validate_llm_output("不一定会继续升温", degraded, sources_all, no_tools, [])
    t("负向后视不误报", r["strong_words"] == [], str(r["strong_words"]))
    r = validate_llm_output(None, degraded, sources_all, no_tools, [])
    t("LLM缺失skip", r["status"] == "skip", r["verdict"])
    rows = [{"sample_time": "2026-10-27 10:00:00", "temperature": 24.7, "humidity": 59.2, "co2": 1065.0, "light": 42411.0},
            {"sample_time": "2026-10-27 10:30:00", "temperature": 25.2, "humidity": 58.5, "co2": 1040.0, "light": 43100.0},
            {"sample_time": "2026-10-27 11:00:00", "temperature": 25.9, "humidity": 57.8, "co2": 1015.0, "light": 44800.0},
            {"sample_time": "2026-10-27 11:30:00", "temperature": 26.5, "humidity": 56.4, "co2": 980.0, "light": 47000.0},
            {"sample_time": "2026-10-27 12:00:00", "temperature": 27.1, "humidity": 55.0, "co2": 950.0, "light": 52000.0},
            {"sample_time": "2026-10-27 12:30:00", "temperature": 27.8, "humidity": 53.8, "co2": 930.0, "light": 59322.0}]
    hit = find_persistent_drift_start(rows, "temperature", 0.5)
    t("漂移起点", bool(hit) and hit[0] == "up" and hit[1]["sample_time"] == "2026-10-27 10:30:00", str(hit))
    night_dt = datetime.strptime("2026-10-28 02:00:00", "%Y-%m-%d %H:%M:%S")
    fake_tools = [{"tool": "b02_history", "status": "ok", "result": rows, "marker": "SIMULATED"},
                  {"tool": "b02_anchor", "status": "ok", "result": [rows[-1]], "marker": "SIMULATED"},
                  {"tool": "b06_predict", "status": "ok", "result": {"status": "ok", "prediction": "NORMAL"}, "marker": "MODEL_OUTPUT"},
                  {"tool": "b03_weather", "status": "ok", "result": {"status": "success"}, "marker": "PUBLIC"},
                  {"tool": "b04_knowledge", "status": "ok", "result": {"status": "ok"}, "marker": "SIMULATED"}]
    ev = compute_events({"history_hours": 3, "trend_hours": 3, "experiment_type": None}, healthy,
                        fake_tools, sources_all, "2026-10-28 02:00:00", night_dt)
    light_events = [e for e in ev["events"] if e.get("parameter") == "light"]
    t("夜间光照事件跳过", not light_events, "light events=%d" % len(light_events))
    recs = [_ev_rec(1, "EV01", "b02_memory", "SIMULATED", "x", "锚点", "ok", {}, evidence_type="current"),
            _ev_rec(4, "EV04", "b02_memory", "SIMULATED", "x", "历史", "ok", {}, evidence_type="history"),
            _ev_rec(5, "EV05", "b02_memory", "SIMULATED", "x", "趋势窗口 ok", "ok", {}, evidence_type="trend"),
            _ev_rec(6, "EV06", "b03_weather", "PUBLIC", "x", "天气", "ok", {"simulated": False}, evidence_type="weather"),
            _ev_rec(7, "EV07", "b04_rag", "SIMULATED", "x", "知识", "ok", {}, evidence_type="knowledge"),
            _ev_rec(8, "EV08", "b06_ml", "MODEL_OUTPUT", "x", "预测", "ok", {"prediction": "WATCH"}, evidence_type="prediction")]
    elevated = dict(healthy); elevated["conclusion"] = "需要预警"
    elevated["trend_findings"] = [{"parameter": "temperature", "alert": True}]
    rel = compute_evidence_relations(elevated, recs, sources_all)
    t("关系规则", rel["EV08"] == ("支持", "模型预测 WATCH(未来1~2h短时推演)支撑关注级提升") and rel["EV06"][0] == "中性"
      and rel["EV07"][0] == "中性", str({k: v[0] for k, v in rel.items()}))
    dirs = [sub for sub in EVIDENCE_B07_DIR.iterdir()
            if sub.is_dir() and (sub / "trace.json").exists()] if EVIDENCE_B07_DIR.exists() else []
    if dirs:
        sample_run = dirs[0].name
        r = trace_conclusion(sample_run, "不存在的结论X")
        t("溯源结论不一致提示", not r.get("consistent") and r.get("warning"), r.get("warning", "")[:50])
    else:
        t("溯源结论不一致提示", True, "SKIP(无既有 run 目录)")

    n_ok = sum(1 for _, ok, _ in results if ok)
    print("\n================ selftest(校验函数负向自测) ================")
    for i, (name, ok, detail) in enumerate(results, start=1):
        print("  [%02d] %-18s %s  %s" % (i, name, "✓ PASS" if ok else "✗ FAIL", detail))
    print("  汇总: %d/%d 通过" % (n_ok, len(results)))


def print_usage():
    print("""GreenMate B07 系统组装模块 — 用法
  py b07_main.py run S1 [--run-id run_x]   # 单场景;默认自动生成新 run_id
  py b07_main.py run-all                   # 依次跑通 S1~S5,最后输出对比表
  py b07_main.py report <run_id>           # 从 evidence/b07/<run_id>/ 重新生成 HTML 报告
  py b07_main.py list                      # 列出全部历史运行与结论
  py b07_main.py --help                    # 本说明

案例A 第二遍·可复现实验框架(配置位于 testdata/experiments/):
  py b07_main.py exp <type>                # 一键生成实验 run(run_<type>_<seq> 可读命名)
                                           # type: agree | conflict | missing_weather
                                           #       current_only | with_history | with_history_prediction
  py b07_main.py exp-all                   # 依次生成 6 组实验 + 创新点3/创新点1 两张对比表
  py b07_main.py trace <run_id> "<结论>"   # 创新点2 溯源:支撑该结论的全部证据清单 + B05 工具调用校验
  py b07_main.py check <run_id>...         # 创新点3 校验:分歧保留/天气不编造/视图一致/溯源自检
  py b07_main.py check-all                 # 校验全部实验 run + 汇总表
  py b07_main.py selftest                  # 校验函数负向自测(11 项,零 LLM 零网络)

五大场景(配置位于 testdata/scenarios/):
  S1 稳态基线   S2 渐进漂移   S3 外部扰动   S4 多源证据分歧   S5 证据退化
说明:
  · 四个视图与全部对比只读取已保存 run_id 的存档数据,禁止重新调用 API/LLM。
  · run-all/exp-all 含真实天气 HTTP 调用(约 1~3 秒/次);断网时如实降级,不伪造成功。
  · LLM 解释为可选:B07_LLM_BASE_URL / B07_LLM_MODEL / B07_LLM_TIMEOUT;
    API Key 读 DEEPSEEK_API_KEY → ANTHROPIC_AUTH_TOKEN;无 Key 时标记 omitted。
  · 全程日志: logs/b07_main.log;产物: evidence/b07/<run_id>/(含 run_result.json,
    B08 契约校验输入)+ report/<run_id>_report.html""")


def main():
    """程序入口:setup_console 最先;缺失/降级是场景结论不是脚本错误,退出码恒 0。"""
    setup_console()
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print_usage()
        return
    cmd = args[0]
    if cmd == "run":
        cmd_run(args[1:])
    elif cmd == "run-all":
        cmd_run_all()
    elif cmd == "report":
        cmd_report(args[1:])
    elif cmd == "list":
        cmd_list()
    elif cmd == "exp":
        cmd_exp(args[1:])
    elif cmd == "exp-all":
        cmd_exp_all()
    elif cmd == "trace":
        cmd_trace(args[1:])
    elif cmd == "check":
        cmd_check(args[1:])
    elif cmd == "check-all":
        cmd_check_all()
    elif cmd == "selftest":
        cmd_selftest()
    else:
        print("未知子命令: %s" % cmd)
        print_usage()


if __name__ == "__main__":
    main()
