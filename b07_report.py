#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 GreenMate B07 — 四视图 HTML 报告渲染(b07_report.py)
=====================================================================

【任务目标】
  将 B07 一次完整流程的 trace / 结构化判断 / 证据记录 / 场景配置渲染为
  自包含 HTML 报告,提供四个核心视图:
  ① 状态总览   结论横幅 + 信任徽章 + 证据标记 chips + 4 参数卡 + LLM 解释
  ② 状态演化   4 参数时序折线图(锚点竖虚线)+ 窗口统计表
  ③ 证据空间   证据记录表 + 工具结果表 + 结构化判断 JSON
  ④ 场景回放对比 本场景配置与实际/预期对比 + 全部历史运行对比表
  Chart.js 经相对路径 ../b02_assets/chart.umd.js 离线加载(file:// 可用),
  缺失时降级为横幅提示,表格与文本照常渲染。

【运行方式】
  通常由 b07_main.py 自动调用;也可单独执行重新生成报告:
  py b07_report.py <run_id>
=====================================================================
"""

import html
import json
import sys
from datetime import datetime
from pathlib import Path

# ---------------- 配置 ----------------
BASE_DIR = Path(__file__).resolve().parent
REPORT_DIR = BASE_DIR / "report"
EVIDENCE_B07_DIR = BASE_DIR / "evidence" / "b07"

PARAM_KEYS = ("temperature", "humidity", "co2", "light")
PARAM_NAMES = {"temperature": "温度", "humidity": "湿度", "co2": "CO₂浓度", "light": "光照"}
PARAM_UNITS = {"temperature": "℃", "humidity": "%RH", "co2": "ppm", "light": "lux"}
MARKER_ORDER = ("SIMULATED", "PUBLIC", "DERIVED_LABEL", "MODEL_OUTPUT")


def setup_console():
    """强制标准输出为 UTF-8:GBK 管道打印 ℃/CO₂ 会崩溃(本项目已多次实测)。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def _now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _esc(text):
    """HTML 转义:全部注入文本必经此函数,防注入。"""
    return html.escape(str(text), quote=True)


def _js_json(obj):
    """注入 <script> 的 JSON:转义 </ 防止提前闭合脚本标签。"""
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


def _fmt(value, nd=1):
    if value is None:
        return "—"
    try:
        return ("%%.%df" % nd) % float(value)
    except (TypeError, ValueError):
        return str(value)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ---------------- 视图片段构建 ----------------

def _marker_chips(evidence_records, judgment):
    """证据来源标记 chips(计数):evidence_records 的 source_marker +
    source_findings 中的 DERIVED_LABEL(规则/趋势派生标记)。"""
    counts = {}
    for rec in evidence_records:
        m = rec.get("source_marker")
        if m:
            counts[m] = counts.get(m, 0) + 1
    for f in (judgment or {}).get("source_findings", []):
        m = f.get("marker")
        if m == "DERIVED_LABEL":
            counts[m] = counts.get(m, 0) + 1
    cls = {"SIMULATED": "sim", "PUBLIC": "pub", "DERIVED_LABEL": "der", "MODEL_OUTPUT": "model"}
    parts = []
    for m in MARKER_ORDER:
        if counts.get(m):
            parts.append('<span class="chip %s" title="证据来源标记">%s ×%d</span>' % (cls.get(m, "sim"), _esc(m), counts[m]))
    return '<div class="marker-row">%s</div>' % "".join(parts) if parts else '<div class="marker-row muted">无证据标记</div>'


def _conclusion_banner(judgment):
    """结论横幅:颜色按 attention_level,含结论 + 信任徽章。"""
    j = judgment or {}
    level = j.get("attention_level", "normal")
    label = {"normal": "正常", "attention": "需要关注", "warning": "需要预警"}.get(level, level)
    return (
        '<div class="banner %s">'
        '<span class="banner-title">系统结论:%s</span>'
        '<span class="banner-sub">关注级:%s</span>'
        '<span class="trust-badge">可信度:%s</span>'
        "</div>"
    ) % (_esc(level), _esc(j.get("conclusion", "—")), _esc(label), _esc(j.get("trust_level", "—")))


def _param_cards(judgment):
    """4 参数卡:值/适宜带/判定/命中规则与建议。"""
    cls_map = {"适宜": "ok", "偏高": "bad", "偏低": "bad", "夜间不检查": "muted", "无法判定": "muted"}
    cards = []
    for p in (judgment or {}).get("per_param", []):
        band = p.get("band")
        band_text = "%s ~ %s %s" % (_fmt(band[0], 1), _fmt(band[1], 1), _esc(p.get("unit", ""))) if band else "—"
        rules = p.get("rule_names") or []
        advice = p.get("advice") or []
        rule_html = ""
        if rules:
            items = []
            for name, ad in zip(rules, advice):
                items.append('<li><strong>%s</strong>:%s</li>' % (_esc(name), _esc(ad)))
            rule_html = '<ul class="advice">%s</ul>' % "".join(items)
        note = p.get("note")
        note_html = '<div class="muted small">%s</div>' % _esc(note) if note else ""
        cards.append(
            '<div class="card param-card">'
            '<h4>%s</h4>'
            '<div class="value">%s<span class="unit">%s</span></div>'
            '<div class="band">适宜带:%s</div>'
            '<div><span class="status %s">%s</span></div>'
            "%s%s"
            "</div>"
            % (_esc(p.get("name", "?")), _fmt(p.get("value"), 1), _esc(p.get("unit", "")),
               band_text, cls_map.get(p.get("verdict"), "muted"), _esc(p.get("verdict", "—")),
               rule_html, note_html)
        )
    return '<div class="grid4">%s</div>' % "".join(cards)


def _explanation_block(explanation):
    """LLM 自然语言解释块:success 原文 / omitted-skipped 如实说明原因。"""
    ex = explanation or {}
    status = ex.get("status")
    if status == "success":
        return ('<div class="card"><h4>LLM 自然语言解释(仅解释,不改变结构化判断)</h4>'
                '<div class="explain-text">%s</div></div>') % _esc(ex.get("text") or "")
    note = ex.get("note") or "未生成"
    if status == "skipped":
        return '<div class="card"><h4>LLM 自然语言解释</h4><div class="muted">⏭ 跳过:场景配置 llm_explain=false。</div></div>'
    return '<div class="card"><h4>LLM 自然语言解释</h4><div class="muted">⚠ 未生成:%s(结构化判断不受影响)。</div></div>' % _esc(note)


def _degraded_list(judgment):
    reasons = (judgment or {}).get("degraded_reasons") or []
    if not reasons:
        return '<div class="card"><h4>降级与分歧事项</h4><div class="ok-text">无——全部证据来源健康,结论可信。</div></div>'
    items = "".join('<li>%s</li>' % _esc(r) for r in reasons)
    return '<div class="card"><h4>降级与分歧事项(%d 项)</h4><ul class="degraded">%s</ul></div>' % (len(reasons), items)


def _boundary_list(judgment):
    notes = (judgment or {}).get("capability_boundary_notes") or []
    items = "".join('<li>%s</li>' % _esc(n) for n in notes)
    return '<div class="card"><h4>能力边界说明</h4><ul class="degraded">%s</ul></div>' % items


def _source_chips(data_source_config):
    """数据源配置 chips:历史/天气/知识/预测 开·关·注入(视图1)。"""
    src = (data_source_config or {}).get("sources") or {}
    labels = [("历史", src.get("history", True)), ("天气", src.get("weather", "real")),
              ("知识", src.get("knowledge", True)), ("预测", src.get("prediction", True))]
    parts = []
    for name, v in labels:
        if name == "天气":
            text = {"real": "真实 API", "injected_cooling": "实验注入(降温)", "off": "关闭"}.get(v, str(v))
            cls = {"real": "on", "injected_cooling": "inject", "off": "off"}.get(v, "on")
        else:
            text, cls = ("开", "on") if v else ("关", "off")
        parts.append('<span class="src-chip %s">%s:%s</span>' % (cls, _esc(name), _esc(text)))
    etype = (data_source_config or {}).get("experiment_type")
    return ('<div class="card"><h4>数据源配置(输入开关,存档回显)</h4><div>%s</div>'
            '<div class="muted small">experiment_type: %s</div></div>'
            % ("".join(parts), _esc(etype or "无(标准场景)")))


def _evidence_completeness(judgment):
    """证据完整度 N/4(视图1)。"""
    comp = (judgment or {}).get("evidence_completeness") or {}
    avail = comp.get("available", "—")
    disabled = comp.get("disabled") or []
    dtext = "、".join(disabled) if disabled else "无"
    return ('<div class="card"><h4>证据完整度</h4>'
            '<div class="value">%s<span class="unit">/ %s 类证据可用</span></div>'
            '<div class="muted small">四类证据:历史(已发生)/ 天气(未来外部条件)/ 知识(静态规则)/ 预测(短时推演);'
            '配置关闭的来源: %s</div>'
            '<div class="muted small">%s</div></div>'
            % (_esc(avail), _esc(comp.get("expected", 4)), _esc(dtext), _esc(comp.get("note", ""))))


def _raw_vs_understanding(judgment, tool_results):
    """原始观测数值 vs 系统结构化理解 对照块(视图1)。
    左=存档锚点原始数值;右=判断引擎理解(状态/关注级/趋势【已发生】/预测【未来】/完整度)。"""
    anchor = None
    for tr in tool_results:
        if tr.get("tool") == "b02_anchor" and tr.get("status") == "ok" and isinstance(tr.get("result"), list) and tr["result"]:
            anchor = tr["result"][0]
            break
    if anchor:
        raw_rows = "".join('<tr><td>%s</td><td class="mono">%s %s</td><td>%s</td></tr>'
                           % (_esc(PARAM_NAMES[k]), _fmt(anchor.get(k), 1), _esc(PARAM_UNITS[k]),
                              _esc(anchor.get("source_type", "—"))) for k in PARAM_KEYS)
        raw_block = ('<table class="tbl"><tr><th>参数</th><th>存档原始数值</th><th>来源</th></tr>%s</table>'
                     '<div class="muted small">sample_time: %s(仅展示存档值,不重新采集)</div>'
                     % (raw_rows, _esc(anchor.get("sample_time", "—"))))
    else:
        raw_block = '<div class="muted">锚点样本缺失,无原始观测数值。</div>'
    j = judgment or {}
    trend_txt = "—(历史数据源关闭)"
    for f in j.get("trend_findings", []):
        if f.get("alert"):
            trend_txt = "%s:%s" % (f.get("name"), f.get("reason"))
            break
    if trend_txt.startswith("—") and j.get("trend_findings"):
        trend_txt = "平稳(无预警)"
    pred_txt = "—(预测数据源关闭)"
    for f in j.get("source_findings", []):
        if f.get("source") == "b06_predict":
            pred_txt = f.get("finding", "—")
            break
    comp = (j.get("evidence_completeness") or {}).get("available", "—")
    understanding = ('<table class="tbl">'
                     '<tr><th>当前环境状态</th><td>%s</td></tr>'
                     '<tr><th>关注等级</th><td>%s(%s)</td></tr>'
                     '<tr><th>近期趋势(已发生)</th><td>%s</td></tr>'
                     '<tr><th>下一窗口预测(未来1~2h)</th><td>%s</td></tr>'
                     '<tr><th>证据完整度</th><td>%s / 4</td></tr>'
                     "</table>"
                     % (_esc(j.get("conclusion", "—")), _esc(j.get("attention_level", "—")),
                        _esc({"normal": "正常", "attention": "需要关注", "warning": "需要预警"}.get(j.get("attention_level"), "—")),
                        _esc(trend_txt), _esc(pred_txt), _esc(comp)))
    return ('<div class="card"><h4>原始数值 vs 系统理解(区分「30℃ 读数」与「稳定/漂移」状态)</h4>'
            '<div class="raw-vs-grid">'
            '<div class="raw-vs-col"><h5>原始观测数值(存档)</h5>%s</div>'
            '<div class="raw-vs-col"><h5>系统结构化理解</h5>%s</div>'
            "</div></div>" % (raw_block, understanding))


def _events_table(events_data, history_rows):
    """视图2 关键事件标注表;history 空时给出空态说明。"""
    events = (events_data or {}).get("events") or []
    note = (events_data or {}).get("note")
    if not history_rows:
        head = ('<div class="chart-missing">⚠ 本 run 数据源 history=false,无历史时序可绘制——'
                '仅展示锚点当前值(实验设计:系统无法区分「稳定态」与「漂移态」)。</div>')
    elif not events:
        head = '<div class="muted">本 run 无关键事件标注(历史平稳且无模型/天气/关注级事件)。</div>'
    else:
        head = ""
    if not events:
        table = ""
    else:
        rows = ["<tr><th>event_id</th><th>类型</th><th>时刻</th><th>标签</th><th>说明</th></tr>"]
        type_names = {"trend_start": "趋势起点", "model_watch": "模型预测", "weather": "天气证据",
                      "weather_missing": "天气缺失", "attention_raised": "关注级提升", "rule_hit": "规则命中"}
        for e in events:
            rows.append('<tr><td>%s</td><td>%s</td><td class="mono">%s</td><td>%s</td><td class="summary">%s</td></tr>'
                        % (_esc(e.get("event_id")), _esc(type_names.get(e.get("type"), e.get("type"))),
                           _esc(e.get("sample_time")), _esc(e.get("label")), _esc(e.get("note", ""))))
        table = '<table class="tbl">%s</table>' % "".join(rows)
    note_html = '<div class="muted small">%s</div>' % _esc(note) if note else ""
    return '<div class="card"><h4>关键事件标注(状态如何形成)</h4>%s%s%s</div>' % (head, table, note_html)


def _experiment_comparison(run_id):
    """视图4 实验并排对比:渲染时扫描 evidence/b07/*/scenario.json,按 experiment_type 分组。
    只读其他 run 的存档数据,零 API/LLM。"""
    by_type = {}
    if EVIDENCE_B07_DIR.exists():
        for sub in EVIDENCE_B07_DIR.iterdir():
            if not sub.is_dir():
                continue
            try:
                sc = json.loads((sub / "scenario.json").read_text(encoding="utf-8"))
                j = json.loads((sub / "structured_judgment.json").read_text(encoding="utf-8"))
                trace = json.loads((sub / "trace.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            etype = (sc.get("config") or {}).get("experiment_type") or j.get("experiment_type")
            if not etype:
                continue
            val = (trace.get("steps", {}).get("llm_validation") or {}).get("verdict", "—")
            by_type[etype] = {"run_id": sub.name, "conclusion": j.get("conclusion", "—"),
                              "attention_level": j.get("attention_level", "—"),
                              "trust_level": j.get("trust_level", "—"),
                              "degraded": len(j.get("degraded_reasons") or []),
                              "completeness": (j.get("evidence_completeness") or {}).get("available", "—"),
                              "llm_verdict": val}
    groups = [
        ("创新点3 证据分歧/缺失(并排对比)", ("agree", "conflict", "missing_weather")),
        ("创新点1 连续时间认知(拿掉→恢复→比较)", ("current_only", "with_history", "with_history_prediction")),
    ]
    blocks = []
    for title, types in groups:
        rows = ["<tr><th>实验</th><th>run_id</th><th>结论</th><th>关注级</th><th>信任</th>"
                "<th>降级</th><th>完整度</th><th>LLM 校验</th></tr>"]
        for t in types:
            r = by_type.get(t)
            if not r:
                rows.append('<tr><td>%s</td><td colspan="7" class="muted">(未运行)</td></tr>' % _esc(t))
                continue
            cur = ' class="current-row"' if r["run_id"] == run_id else ""
            rows.append('<tr%s><td>%s</td><td class="mono">%s</td><td>%s</td><td>%s</td><td>%s</td>'
                        '<td>%d</td><td>%s/4</td><td>%s</td></tr>'
                        % (cur, _esc(t), _esc(r["run_id"]), _esc(r["conclusion"]),
                           _esc(r["attention_level"]), _esc(r["trust_level"]), r["degraded"],
                           _esc(r["completeness"]), _esc(r["llm_verdict"])))
        blocks.append('<div class="card"><h4>%s</h4><table class="tbl">%s</table>'
                      '<div class="muted small">对比数据只读取各 run_id 的存档文件,不重新调用任何 API/LLM。</div></div>'
                      % (_esc(title), "".join(rows)))
    return "".join(blocks) if blocks else ('<div class="card"><h4>实验并排对比</h4>'
                                           '<div class="muted">暂无实验运行记录(执行 py b07_main.py exp-all 后生成)。</div></div>')


def _tool_replay(tool_results):
    """视图4 B05 工具调用回放(基于存档 tool_results,含 disabled)。"""
    if not tool_results:
        return '<div class="card"><h4>B05 工具调用回放</h4><div class="muted">无工具调用记录。</div></div>'
    rows = ["<tr><th>seq</th><th>调用时间</th><th>工具</th><th>标记</th><th>参数</th><th>状态</th><th>嵌套状态</th></tr>"]
    for tr in tool_results:
        args_text = json.dumps(tr.get("arguments"), ensure_ascii=False)
        if len(args_text) > 80:
            args_text = args_text[:80] + "…"
        st = tr.get("status")
        st_text = {"ok": "OK", "disabled": "关闭", "error": "失败"}.get(st, st)
        nested = _nested_status(tr.get("result")) if st != "disabled" else (tr.get("arguments") or {}).get("reason", "")
        rows.append('<tr><td>%s</td><td class="mono">%s</td><td>%s</td><td>%s</td>'
                    '<td class="summary mono">%s</td><td><span class="status %s">%s</span></td>'
                    '<td class="muted">%s</td></tr>'
                    % (_esc(tr.get("seq")), _esc(tr.get("called_at", "—")), _esc(tr.get("tool")),
                       _esc(tr.get("marker")), _esc(args_text),
                       "ok" if st == "ok" else ("disabled" if st == "disabled" else "error"),
                       _esc(st_text), _esc(nested)))
    return ('<div class="card"><h4>B05 工具调用回放(输入/返回/成功与否,存档逐条可查)</h4>'
            '<table class="tbl">%s</table>'
            '<div class="muted small">B07 自持薄包装调度;真实执行证据,与报告表述可交叉校验。</div></div>'
            % "".join(rows))


def _llm_validation_card(llm_validation):
    """视图4 LLM 输出校验卡片(案例A 第二遍)。"""
    val = llm_validation or {}
    items = val.get("items") or []
    if not items:
        return ""
    rows = ["<tr><th>检查项</th><th>结果</th><th>说明</th></tr>"]
    for i in items:
        res = i.get("result")
        mark = {"pass": "✓ 通过", "pass_warning": "✓ 通过(注意)", "fail": "✗ 不通过", "skip": "— 跳过"}.get(res, res)
        cls = {"pass": "ok", "pass_warning": "warn", "fail": "bad", "skip": "muted"}.get(res, "muted")
        rows.append('<tr><td>%s</td><td class="%s-text">%s</td><td class="summary">%s</td></tr>'
                    % (_esc(i.get("check")), _esc(cls), _esc(mark), _esc(i.get("detail", ""))))
    verdict = val.get("verdict", "—")
    vcls = {"PASS": "ok-text", "FAIL": "bad-text"}.get(verdict.split("(")[0].strip(), "muted")
    return ('<div class="card"><h4>LLM 输出校验(强确定词/天气不编造/工具调用校验)</h4>'
            '<table class="tbl">%s</table>'
            '<div class="%s">校验结论: %s</div>'
            '<div class="muted small">%s</div></div>'
            % ("".join(rows), vcls, _esc(verdict), _esc(val.get("note", ""))))


def _evolution_charts():
    """状态演化:4 个 small-multiple 折线图画布(数据与绘制全部在 JS 侧)。"""
    cards = []
    for key in PARAM_KEYS:
        cards.append(
            '<div class="card chart-card"><h4>%s (%s)</h4>'
            '<div class="chart-wrap"><canvas id="chart-%s"></canvas></div></div>'
            % (_esc(PARAM_NAMES[key]), _esc(PARAM_UNITS[key]), _esc(key))
        )
    return ('<div class="chart-missing" id="chart-missing" hidden>⚠ Chart.js 未能加载'
            '(../b02_assets/chart.umd.js 缺失或无法读取),折线图不可用,下方统计表仍可查看。</div>'
            '<div class="charts-grid">%s</div>') % "".join(cards)


def _stats_table(stats_result, anchor_text):
    """窗口统计表:count/min/max/mean/std/斜率/首末样本。"""
    if not stats_result or not stats_result.get("count"):
        return ('<div class="card"><h4>窗口统计(趋势窗口)</h4>'
                '<div class="muted">趋势窗口无样本,统计不可用。</div></div>')
    rows = ['<tr><th>参数</th><th>count</th><th>min</th><th>max</th><th>mean</th><th>std</th><th>斜率(/小时)</th></tr>']
    for key in PARAM_KEYS:
        st = (stats_result.get("parameters") or {}).get(key) or {}
        rows.append('<tr><td>%s (%s)</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>'
                    % (_esc(PARAM_NAMES[key]), _esc(PARAM_UNITS[key]), _fmt(st.get("count")),
                       _fmt(st.get("min")), _fmt(st.get("max")), _fmt(st.get("mean")), _fmt(st.get("std")),
                       _fmt(st.get("slope_per_hour"), 3)))
    win = stats_result.get("window") or {}
    first = (stats_result.get("first") or {}).get("sample_time")
    last = (stats_result.get("last") or {}).get("sample_time")
    return ('<div class="card"><h4>窗口统计(趋势窗口 %s ~ %s,共 %d 点;锚点 %s)</h4>'
            '<table class="tbl">%s</table>'
            '<div class="muted small">首样本:%s / 末样本:%s(末样本即锚点时刻样本)</div></div>'
            % (_esc(win.get("start", "—")), _esc(win.get("end", "—")), stats_result.get("count"),
               _esc(anchor_text), "".join(rows), _esc(first or "—"), _esc(last or "—")))


def _evidence_table(evidence_records):
    if not evidence_records:
        return '<div class="card"><h4>证据记录</h4><div class="muted">无证据记录。</div></div>'
    rows = ['<tr><th>seq</th><th>evidence_id</th><th>类型</th><th>来源</th><th>标记</th>'
            '<th>自报时间</th><th>摘要</th><th>状态</th><th>关系</th></tr>']
    rel_cls = {"支持": "ok-text", "限制": "bad-text", "中性": "muted"}
    for rec in evidence_records:
        rows.append(
            '<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>'
            '<td class="mono">%s</td><td class="summary">%s</td>'
            '<td><span class="status %s">%s</span></td><td class="%s">%s</td></tr>'
            % (_esc(rec.get("seq")), _esc(rec.get("evidence_id")), _esc(rec.get("evidence_type", "—")),
               _esc(rec.get("source")), _esc(rec.get("source_marker")),
               _esc(str(rec.get("evidence_time") or "—")[:19]), _esc(rec.get("summary", "")),
               _esc(rec.get("status")),
               {"ok": "OK", "error": "失败", "insufficient": "不足", "disabled": "关闭"}.get(rec.get("status"), rec.get("status")),
               rel_cls.get(rec.get("relation"), "muted"), _esc(rec.get("relation") or "—"))
        )
    return ('<div class="card"><h4>证据记录(%d 条)——每份证据自报身份:类型/来源/获取时间/可用性/支持·限制结论</h4>'
            '<table class="tbl">%s</table></div>') % (len(evidence_records), "".join(rows))


def _tool_table(tool_results):
    if not tool_results:
        return '<div class="card"><h4>工具结果</h4><div class="muted">无工具调用。</div></div>'
    rows = ['<tr><th>seq</th><th>工具</th><th>标记</th><th>参数</th><th>状态</th><th>嵌套状态</th></tr>']
    for tr in tool_results:
        args_text = json.dumps(tr.get("arguments"), ensure_ascii=False)
        if len(args_text) > 110:
            args_text = args_text[:110] + "…"
        nested = _nested_status(tr.get("result"))
        rows.append(
            '<tr><td>%s</td><td>%s</td><td>%s</td><td class="summary mono">%s</td>'
            '<td><span class="status %s">%s</span></td><td class="muted">%s</td></tr>'
            % (_esc(tr.get("seq")), _esc(tr.get("tool")), _esc(tr.get("marker")), _esc(args_text),
               "ok" if tr.get("status") == "ok" else "error",
               "OK" if tr.get("status") == "ok" else "失败", _esc(nested))
        )
    return ('<div class="card"><h4>工具结果(%d 次调用,原始执行证据)</h4>'
            '<table class="tbl">%s</table></div>') % (len(tool_results), "".join(rows))


def _nested_status(result):
    """天气/知识/模型证据的嵌套状态(调度层 ok 但证据本身可能 error/insufficient)。"""
    if isinstance(result, dict):
        st = result.get("status")
        if st == "error":
            err = result.get("error") or {}
            return "error: %s" % (err.get("error_type") or err.get("code") or "unknown")
        if st == "insufficient":
            return "insufficient: 资料不足"
        if st == "ok":
            return "ok"
        if st == "success":
            return "success"
    return "—"


def _judgment_pre(judgment):
    return ('<div class="card"><h4>structured_judgment.json(可保存、可对比的结构化判断)</h4>'
            '<pre class="json-pre">%s</pre></div>') % _esc(json.dumps(judgment or {}, ensure_ascii=False, indent=2))


def _replay_card(scenario_cfg, actuals, expected, run_id):
    """场景回放:配置回显 + actuals vs expected + 重跑命令。"""
    cfg = scenario_cfg or {}
    degrade = cfg.get("degrade") or {}
    rows = []
    for key, label in (("conclusion", "结论"), ("attention_level", "关注级"), ("trust", "可信度")):
        av = actuals.get(key) if actuals else None
        ev = (expected or {}).get(key)
        mark = "✓" if (av is not None and ev is not None and str(av) == str(ev)) else "✗"
        rows.append('<tr><td>%s</td><td>%s</td><td>%s</td><td class="%s">%s</td></tr>'
                    % (_esc(label), _esc(ev if ev is not None else "—"), _esc(av if av is not None else "—"),
                       "ok-text" if mark == "✓" else "bad-text", mark))
    note = (expected or {}).get("note")
    return (
        '<div class="card"><h4>本场景配置(回放输入)</h4>'
        '<table class="tbl">'
        '<tr><th>项</th><th>值</th></tr>'
        '<tr><td>scenario_id</td><td>%s</td></tr>'
        '<tr><td>名称</td><td>%s</td></tr>'
        '<tr><td>描述</td><td class="summary">%s</td></tr>'
        '<tr><td>锚点样本</td><td>%s</td></tr>'
        '<tr><td>历史窗口 / 趋势窗口</td><td>%s h / %s h</td></tr>'
        '<tr><td>天气地点</td><td>%s</td></tr>'
        '<tr><td>知识查询</td><td>%s</td></tr>'
        '<tr><td>降级配置</td><td>weather=%s / knowledge=%s</td></tr>'
        '<tr><td>LLM 解释</td><td>%s</td></tr>'
        '<tr><td>experiment_type</td><td>%s</td></tr>'
        '<tr><td>数据源开关</td><td>history=%s / weather=%s / knowledge=%s / prediction=%s</td></tr>'
        "</table></div>"
        '<div class="card"><h4>实际 vs 预期(expected 字段供 B08 下一轮验证)</h4>'
        '<table class="tbl"><tr><th>项</th><th>预期</th><th>实际</th><th></th></tr>%s</table>'
        '<div class="muted small">预期说明:%s</div></div>'
        '<div class="card"><h4>场景回放机制</h4>'
        '<div>每次重跑使用<b>新的 run_id</b>,完整链路按同一配置重新执行并落盘,'
        '右侧对比表即历史回放结果。重新执行本场景:</div>'
        '<pre class="cmd">py b07_main.py run %s</pre></div>'
    ) % (_esc(cfg.get("scenario_id", "—")), _esc(cfg.get("name", "—")), _esc(cfg.get("description", "—")),
         _esc(cfg.get("anchor_sample_time", "—")), _esc(cfg.get("history_hours", "—")),
         _esc(cfg.get("trend_hours", "—")), _esc(cfg.get("location", "—")),
         _esc(cfg.get("knowledge_query", "—")), _esc(degrade.get("weather")),
         _esc(degrade.get("knowledge")), _esc(cfg.get("llm_explain", True)),
         _esc(cfg.get("experiment_type") or "无(标准场景)"),
         _esc("开" if (cfg.get("sources") or {}).get("history", True) else "关"),
         _esc((cfg.get("sources") or {}).get("weather", "real")),
         _esc("开" if (cfg.get("sources") or {}).get("knowledge", True) else "关"),
         _esc("开" if (cfg.get("sources") or {}).get("prediction", True) else "关"),
         "".join(rows), _esc(note or "—"), _esc(cfg.get("scenario_id", "?")))


def _comparison_table(run_id):
    """全部历史运行对比(渲染时扫描 evidence/b07/*/structured_judgment.json)。"""
    rows = []
    if EVIDENCE_B07_DIR.exists():
        for sub in EVIDENCE_B07_DIR.iterdir():
            if not sub.is_dir():
                continue
            j = _read_json(sub / "structured_judgment.json")
            if j:
                rows.append({"run_id": sub.name, "scenario_id": j.get("scenario_id", "?"),
                             "name": j.get("scenario_name", "?"), "conclusion": j.get("conclusion", "—"),
                             "trust_level": j.get("trust_level", "—"), "judged_at": j.get("judged_at", "—")})
    rows.sort(key=lambda r: r["run_id"], reverse=True)
    if not rows:
        return ('<div class="card"><h4>历史运行对比</h4>'
                '<div class="muted">暂无其他运行记录(evidence/b07/ 为空)。执行 py b07_main.py run-all 后本表将展示 S1~S5 全部回放结果。</div></div>')
    body = []
    for r in rows:
        cur = ' class="current-row"' if r["run_id"] == run_id else ""
        body.append(
            '<tr%s><td>%s%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>'
            % (cur, _esc(r["run_id"]), " ←本次" if r["run_id"] == run_id else "",
               _esc(r["scenario_id"]), _esc(r["name"]), _esc(r["conclusion"]),
               _esc(r["trust_level"]), _esc(r["judged_at"]))
        )
    return ('<div class="card"><h4>历史运行对比(%d 次)</h4>'
            '<table class="tbl"><tr><th>run_id</th><th>场景</th><th>名称</th><th>结论</th><th>可信度</th><th>判断时间</th></tr>%s</table></div>'
            % (len(rows), "".join(body)))


# ---------------- 页面模板 ----------------
_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GreenMate B07 报告 {{RUN_ID}} / {{SCENARIO}}</title>
<style>
:root {
  --page:#f9f9f7; --panel:#ffffff; --text:#22252a; --muted:#6b7280; --line:#e2e3e0;
  --accent:#2a78d6; --ok:#2e9e5b; --warn:#d97a1e; --bad:#c0392b;
  --series:#2a78d6; --grid:#ececea;
  --chip-sim:#5b8def; --chip-pub:#2e9e5b; --chip-der:#8e6fd6; --chip-model:#d97a1e;
  --banner-normal-bg:#e9f6ee; --banner-normal-bd:#b8e2c6;
  --banner-attention-bg:#fdf3e7; --banner-attention-bd:#f0d3ae;
  --banner-warning-bg:#fdecea; --banner-warning-bd:#f0b8b0;
}
[data-theme="dark"] {
  --page:#17181b; --panel:#1f2126; --text:#e8e9eb; --muted:#9aa0a8; --line:#33363d;
  --accent:#5a9ae6; --ok:#4caf74; --warn:#e09a45; --bad:#e06c62;
  --series:#5a9ae6; --grid:#2a2d33;
  --chip-sim:#4a6fc2; --chip-pub:#3b8c5a; --chip-der:#7a63b8; --chip-model:#b56a20;
  --banner-normal-bg:#15301f; --banner-normal-bd:#2c5a3c;
  --banner-attention-bg:#33260f; --banner-attention-bd:#6b521f;
  --banner-warning-bg:#331715; --banner-warning-bd:#6b2f2a;
}
* { box-sizing: border-box; }
body { margin:0; font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif; background:var(--page); color:var(--text); }
header { padding:16px 24px; border-bottom:1px solid var(--line); background:var(--panel);
         display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
header h1 { font-size:19px; margin:0; }
.meta { color:var(--muted); font-size:13px; }
.spacer { flex:1; }
button { cursor:pointer; font-family:inherit; }
#themeBtn { border:1px solid var(--line); background:var(--panel); color:var(--text);
            border-radius:6px; padding:6px 12px; font-size:13px; }
main { max-width:1200px; margin:0 auto; padding:18px 24px 60px; }
.tabs { display:flex; gap:8px; margin:16px 0 18px; flex-wrap:wrap; }
.tab-btn { border:1px solid var(--line); background:var(--panel); color:var(--muted);
           padding:8px 16px; border-radius:8px; font-size:14px; }
.tab-btn.active { background:var(--accent); color:#fff; border-color:var(--accent); }
.view { display:none; }
.view.active { display:block; }
.banner { border-radius:10px; padding:16px 18px; display:flex; gap:16px; align-items:center;
          flex-wrap:wrap; margin-bottom:16px; border:1px solid; }
.banner.normal { background:var(--banner-normal-bg); border-color:var(--banner-normal-bd); }
.banner.attention { background:var(--banner-attention-bg); border-color:var(--banner-attention-bd); }
.banner.warning { background:var(--banner-warning-bg); border-color:var(--banner-warning-bd); }
.banner-title { font-size:20px; font-weight:700; }
.banner-sub { color:var(--muted); font-size:14px; }
.trust-badge { border:1px solid var(--line); background:var(--panel); border-radius:999px;
               padding:4px 14px; font-size:14px; font-weight:600; }
.marker-row { margin-bottom:16px; }
.chip { display:inline-block; border-radius:999px; padding:3px 10px; font-size:12px;
        margin-right:6px; color:#fff; }
.chip.sim { background:var(--chip-sim); } .chip.pub { background:var(--chip-pub); }
.chip.der { background:var(--chip-der); } .chip.model { background:var(--chip-model); }
.grid4 { display:grid; grid-template-columns:repeat(auto-fit, minmax(230px, 1fr)); gap:12px; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:10px;
        padding:14px 16px; margin-bottom:14px; }
.card h4 { margin:0 0 10px; font-size:14px; color:var(--muted); font-weight:600; }
.card .value { font-size:26px; font-weight:700; }
.card .unit { font-size:13px; color:var(--muted); margin-left:4px; font-weight:400; }
.band { color:var(--muted); font-size:12px; margin:2px 0 8px; }
.status { display:inline-block; border-radius:6px; padding:2px 8px; font-size:12px; font-weight:600; }
.status.ok { background:rgba(46,158,91,.15); color:var(--ok); }
.status.error { background:rgba(192,57,43,.15); color:var(--bad); }
.status.insufficient { background:rgba(217,122,30,.15); color:var(--warn); }
.status.disabled { background:rgba(107,114,128,.15); color:var(--muted); }
.src-chip { display:inline-block; border-radius:999px; padding:3px 12px; font-size:12px;
            margin:2px 6px 2px 0; border:1px solid var(--line); background:var(--page); }
.src-chip.on { border-color:var(--ok); color:var(--ok); }
.src-chip.off { border-color:var(--muted); color:var(--muted); text-decoration:line-through; }
.src-chip.inject { border-color:var(--chip-der); color:var(--chip-der); }
.raw-vs-grid { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
.raw-vs-col h5 { margin:0 0 8px; font-size:13px; color:var(--muted); font-weight:600; }
@media (max-width:760px) { .raw-vs-grid { grid-template-columns:1fr; } }
.muted { color:var(--muted); }
.ok-text { color:var(--ok); } .bad-text { color:var(--bad); }
.small { font-size:12px; margin-top:6px; }
.advice, .degraded { margin:8px 0 0; padding-left:20px; font-size:13px; line-height:1.7; }
.explain-text { white-space:pre-wrap; line-height:1.8; font-size:14px; }
.charts-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(340px, 1fr)); gap:12px; }
.chart-wrap { position:relative; height:190px; }
.chart-missing { border:1px dashed var(--warn); color:var(--warn); border-radius:8px;
                 padding:10px 14px; margin-bottom:14px; font-size:13px; }
.tbl { width:100%; border-collapse:collapse; font-size:13px; }
.tbl th, .tbl td { border:1px solid var(--line); padding:7px 10px; text-align:left; vertical-align:top; }
.tbl th { background:var(--page); color:var(--muted); font-weight:600; }
.tbl .summary { max-width:420px; }
.current-row { background:rgba(42,120,214,.08); }
.mono { font-family:Consolas,"Courier New",monospace; font-size:12px; }
.json-pre { background:var(--page); border:1px solid var(--line); border-radius:8px;
            padding:12px; overflow:auto; max-height:480px; font-size:12px; line-height:1.5;
            font-family:Consolas,"Courier New",monospace; white-space:pre; }
.cmd { background:var(--page); border:1px solid var(--line); border-radius:8px; padding:10px 12px;
       font-family:Consolas,"Courier New",monospace; font-size:13px; }
footer { color:var(--muted); font-size:12px; margin-top:24px; text-align:center; }
</style>
</head>
<body>
<header>
  <h1>🌱 GreenMate 系统报告 <span class="meta">{{SCENARIO_ID}} {{SCENARIO_NAME}}</span></h1>
  <span class="meta">run_id: {{RUN_ID}} · 生成于 {{GENERATED_AT}}</span>
  <span class="spacer"></span>
  <button id="themeBtn">🌙 深色</button>
</header>
<main>
  <nav class="tabs">
    <button class="tab-btn active" data-view="overview">① 状态总览</button>
    <button class="tab-btn" data-view="evolution">② 状态演化</button>
    <button class="tab-btn" data-view="evidence">③ 证据空间</button>
    <button class="tab-btn" data-view="replay">④ 场景回放对比</button>
  </nav>

  <section class="view active" id="view-overview">
    {{CONCLUSION_BANNER}}
    {{MARKER_CHIPS}}
    {{SOURCE_CHIPS}}
    {{EVIDENCE_COMPLETENESS}}
    {{RAW_VS_UNDERSTANDING}}
    {{PARAM_CARDS}}
    <div class="spacer"></div>
    {{EXPLANATION_BLOCK}}
    {{DEGRADED_LIST}}
    {{BOUNDARY_LIST}}
  </section>

  <section class="view" id="view-evolution">
    {{EVOLUTION_CHARTS}}
    {{EVENTS_TABLE}}
    {{STATS_TABLE}}
  </section>

  <section class="view" id="view-evidence">
    {{EVIDENCE_TABLE}}
    {{TOOL_TABLE}}
    {{JUDGMENT_PRE}}
  </section>

  <section class="view" id="view-replay">
    {{REPLAY_CARD}}
    {{EXP_COMPARISON}}
    {{TOOL_REPLAY}}
    {{LLM_VALIDATION}}
    {{COMPARISON_TABLE}}
  </section>

  <footer>GreenMate B07 系统组装模块 · 证据标记:SIMULATED / PUBLIC / DERIVED_LABEL / MODEL_OUTPUT · 业务顺序:工具结果 → 证据记录 → 结构化判断 → LLM 解释</footer>
</main>
<script src="../b02_assets/chart.umd.js"></script>
<script>
const REPORT = {{REPORT_JSON}};

// ---- 锚点竖虚线插件(自注册,按 x 像素绘制,不受 y 轴范围影响)----
if (typeof Chart !== 'undefined') {
  const anchorLine = {
    id: 'anchorLine',
    afterDatasetsDraw(chart) {
      const opts = (chart.options.plugins || {}).anchorLine;
      if (!opts || !opts.anchorLabel) return;
      const idx = chart.data.labels.indexOf(opts.anchorLabel);
      if (idx < 0) return;
      const x = chart.scales.x.getPixelForValue(idx);
      if (x < chart.chartArea.left || x > chart.chartArea.right) return;
      const ctx = chart.ctx;
      ctx.save();
      ctx.setLineDash([6, 4]);
      ctx.strokeStyle = cssVar('--bad');
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(x, chart.chartArea.top);
      ctx.lineTo(x, chart.chartArea.bottom);
      ctx.stroke();
      ctx.restore();
    }
  };
  Chart.register(anchorLine);
  // 关键事件竖线插件(案例A 第三遍 视图2):按 parameter 过滤到对应参数图,
  // 事件标签在 chartArea 顶部纵向堆叠防重叠;索引不存在(事件时刻不在窗口内)→ 跳过。
  const eventLines = {
    id: 'eventLines',
    afterDatasetsDraw(chart) {
      const opts = (chart.options.plugins || {}).eventLines;
      const events = (opts && opts.events) || [];
      const colorMap = {
        trend_start: cssVar('--warn'),
        model_watch: cssVar('--chip-der'),
        weather: cssVar('--chip-pub'),
        weather_missing: cssVar('--bad'),
        attention_raised: cssVar('--bad'),
        rule_hit: cssVar('--bad')
      };
      let stack = 0;
      events.forEach(function (e) {
        if (e.parameter !== null && e.parameter !== opts.paramKey) return;
        const idx = chart.data.labels.indexOf(e.sample_time);
        if (idx < 0) return;
        const x = chart.scales.x.getPixelForValue(idx);
        if (x < chart.chartArea.left || x > chart.chartArea.right) return;
        const ctx = chart.ctx;
        ctx.save();
        ctx.setLineDash([3, 3]);
        ctx.strokeStyle = colorMap[e.type] || cssVar('--muted');
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        ctx.moveTo(x, chart.chartArea.top);
        ctx.lineTo(x, chart.chartArea.bottom);
        ctx.stroke();
        // 顶点圆点 + 顶部堆叠标签
        ctx.fillStyle = colorMap[e.type] || cssVar('--muted');
        ctx.beginPath();
        ctx.arc(x, chart.chartArea.top + 3, 2.5, 0, Math.PI * 2);
        ctx.fill();
        ctx.font = '10px "Segoe UI","Microsoft YaHei",sans-serif';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'top';
        const label = (e.type === 'trend_start' ? e.label : (e.label || ''));
        ctx.fillText(label, x + 5, chart.chartArea.top + 2 + stack * 13);
        stack += 1;
        ctx.restore();
      });
    }
  };
  Chart.register(eventLines);
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

const charts = [];
function makeCharts() {
  const rows = (REPORT.history || []);
  if (typeof Chart === 'undefined') {
    document.getElementById('chart-missing').hidden = false;
    return;
  }
  if (!rows.length) return;
  const labels = rows.map(r => r.sample_time);
  const anchorLabel = (REPORT.judgment || {}).anchor_sample_time;
  const events = (REPORT.events && REPORT.events.events) || [];
  const params = [['temperature','温度 (℃)'],['humidity','湿度 (%RH)'],['co2','CO₂浓度 (ppm)'],['light','光照 (lux)']];
  params.forEach(function (item) {
    const key = item[0], title = item[1];
    const el = document.getElementById('chart-' + key);
    if (!el) return;
    const color = cssVar('--series'), grid = cssVar('--grid'), tick = cssVar('--muted');
    charts.push(new Chart(el, {
      type: 'line',
      data: {
        labels: labels,
        datasets: [{
          label: title, data: rows.map(r => r[key]),
          borderColor: color, backgroundColor: color + '22',
          pointRadius: 0, borderWidth: 2, tension: 0.25, fill: false
        }]
      },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { display: false },
          anchorLine: { anchorLabel: anchorLabel },
          eventLines: { events: events, paramKey: key },
          tooltip: { callbacks: { title: function (items) { return items[0].label; } } }
        },
        scales: {
          x: { ticks: { color: tick, maxRotation: 0, autoSkip: true, maxTicksLimit: 8 }, grid: { color: grid } },
          y: { ticks: { color: tick }, grid: { color: grid } }
        }
      }
    }));
  });
}

function destroyCharts() { charts.forEach(function (c) { c.destroy(); }); charts.length = 0; }

function applyTheme() {
  const dark = document.documentElement.getAttribute('data-theme') === 'dark';
  document.getElementById('themeBtn').textContent = dark ? '☀ 浅色' : '🌙 深色';
  destroyCharts();
  makeCharts();
}

function showView(name) {
  document.querySelectorAll('.tab-btn').forEach(function (b) {
    b.classList.toggle('active', b.dataset.view === name);
  });
  document.querySelectorAll('.view').forEach(function (v) {
    v.classList.toggle('active', v.id === 'view-' + name);
  });
}

document.getElementById('themeBtn').addEventListener('click', function () {
  const root = document.documentElement;
  root.setAttribute('data-theme', root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark');
  applyTheme();
});
document.querySelectorAll('.tab-btn').forEach(function (b) {
  b.addEventListener('click', function () { showView(b.dataset.view); });
});
makeCharts();
</script>
</body>
</html>
"""


# ---------------- 渲染入口 ----------------
def render_report(run_dir, run_id):
    """读取 evidence/b07/{run_id}/ 的产物,渲染 report/{run_id}_report.html。
    缺失关键文件时友好报错,不崩溃;返回是否成功。"""
    trace = _read_json(run_dir / "trace.json")
    if trace is None:
        print("⚠ 无法渲染报告:trace.json 缺失或损坏: %s" % (run_dir / "trace.json"))
        return False
    judgment = trace.get("steps", {}).get("structured_judgment") or {}
    explanation = trace.get("steps", {}).get("explanation") or {}
    tool_results = trace.get("steps", {}).get("tool_results") or []
    evidence_records = trace.get("steps", {}).get("evidence_records") or []
    scenario_cfg = trace.get("scenario") or {}
    # 场景配置优先读 scenario.json(含 actuals/expected),回退 trace
    scenario_json = _read_json(run_dir / "scenario.json")
    if scenario_json:
        scenario_cfg = scenario_json.get("config") or scenario_cfg
        actuals = scenario_json.get("actuals") or {}
        expected = scenario_json.get("expected") or {}
    else:
        actuals = {
            "conclusion": judgment.get("conclusion"),
            "attention_level": judgment.get("attention_level"),
            "trust": judgment.get("trust_level"),
        }
        expected = scenario_cfg.get("expected") or {}

    # 视图 2 数据:从工具结果提取历史序列与窗口统计
    history, stats_result = [], None
    for tr in tool_results:
        if tr.get("tool") == "b02_history" and tr.get("status") == "ok" and isinstance(tr.get("result"), list):
            history = tr["result"]
        if tr.get("tool") == "b02_stats" and tr.get("status") == "ok" and isinstance(tr.get("result"), dict):
            stats_result = tr["result"]

    # 案例A 第二遍/第三遍扩展字段(.get 兜底,旧 trace 兼容不崩)
    data_source_config = trace.get("data_source_config") or {}
    events_data = trace.get("steps", {}).get("events") or {}
    llm_validation = trace.get("steps", {}).get("llm_validation") or {}

    report_data = {
        "run_id": run_id,
        "generated_at": trace.get("generated_at", ""),
        "scenario": scenario_cfg,
        "judgment": judgment,
        "explanation": explanation,
        "evidence_records": evidence_records,
        "tool_results": tool_results,
        "history": history,
        "stats": stats_result,
        "data_source_config": data_source_config,
        "events": events_data,
        "llm_validation": llm_validation,
        "raw_record_saved": trace.get("steps", {}).get("raw_record_saved") or {},
    }

    page = _PAGE
    page = page.replace("{{RUN_ID}}", _esc(run_id))
    page = page.replace("{{GENERATED_AT}}", _esc(trace.get("generated_at", "—")))
    page = page.replace("{{SCENARIO_ID}}", _esc(scenario_cfg.get("scenario_id", "?")))
    page = page.replace("{{SCENARIO_NAME}}", _esc(scenario_cfg.get("name", "")))
    page = page.replace("{{SCENARIO}}", _esc(scenario_cfg.get("scenario_id", "")))
    page = page.replace("{{CONCLUSION_BANNER}}", _conclusion_banner(judgment))
    page = page.replace("{{MARKER_CHIPS}}", _marker_chips(evidence_records, judgment))
    page = page.replace("{{SOURCE_CHIPS}}", _source_chips(data_source_config))
    page = page.replace("{{EVIDENCE_COMPLETENESS}}", _evidence_completeness(judgment))
    page = page.replace("{{RAW_VS_UNDERSTANDING}}", _raw_vs_understanding(judgment, tool_results))
    page = page.replace("{{PARAM_CARDS}}", _param_cards(judgment))
    page = page.replace("{{EXPLANATION_BLOCK}}", _explanation_block(explanation))
    page = page.replace("{{DEGRADED_LIST}}", _degraded_list(judgment))
    page = page.replace("{{BOUNDARY_LIST}}", _boundary_list(judgment))
    page = page.replace("{{EVOLUTION_CHARTS}}", _evolution_charts())
    page = page.replace("{{EVENTS_TABLE}}", _events_table(events_data, history))
    page = page.replace("{{STATS_TABLE}}", _stats_table(stats_result, judgment.get("anchor_sample_time", "")))
    page = page.replace("{{EVIDENCE_TABLE}}", _evidence_table(evidence_records))
    page = page.replace("{{TOOL_TABLE}}", _tool_table(tool_results))
    page = page.replace("{{JUDGMENT_PRE}}", _judgment_pre(judgment))
    page = page.replace("{{REPLAY_CARD}}", _replay_card(scenario_cfg, actuals, expected, run_id))
    page = page.replace("{{EXP_COMPARISON}}", _experiment_comparison(run_id))
    page = page.replace("{{TOOL_REPLAY}}", _tool_replay(tool_results))
    page = page.replace("{{LLM_VALIDATION}}", _llm_validation_card(llm_validation))
    page = page.replace("{{COMPARISON_TABLE}}", _comparison_table(run_id))
    page = page.replace("{{REPORT_JSON}}", _js_json(report_data))

    try:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = REPORT_DIR / ("%s_report.html" % run_id)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(page)
    except OSError as e:
        print("⚠ 报告写入失败: %s" % e)
        return False
    print("✓ 报告已生成: %s" % out_path)
    return True


def print_usage():
    print("用法: py b07_report.py <run_id>")
    print("  从 evidence/b07/<run_id>/ 重新生成 report/<run_id>_report.html(同输入字节一致)")


def main():
    setup_console()
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print_usage()
        return
    run_id = args[0]
    run_dir = EVIDENCE_B07_DIR / run_id
    if not run_dir.is_dir():
        print("⚠ run_id 运行目录不存在: %s(请先执行 py b07_main.py run <场景>)" % run_dir)
        return
    render_report(run_dir, run_id)


if __name__ == "__main__":
    main()
