#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 GreenMate B06 — 多视图报告生成模块
=====================================================================

【任务目标】
  根据 run_id 读取 B05 的最终结论 evidence/final/{run_id}_final.json,
  渲染完整自包含 HTML 报告(四个视图: ①原始数据 ②指标解析
  ③规则与机器学习证据 ④综合结论),保存到 report/{run_id}_report.html。
  报告零外部依赖(无 CDN、无图片、无第三方库),离线可直接打开。

【运行方式】
  py b06_report.py run_20261022_220400_485a96   # 直接生成指定 run_id 的报告
  py b06_report.py                              # 不带参数:列出可用结论交互选择
  py b06_report.py --help                       # 查看用法说明

【数据来源】
  final 证据为必需输入(缺失则报错提示先运行 B05);raw/parsed/rule/ml
  子证据按 B05 的 evidence_status 权威口径加载,缺失时在对应视图内
  以【证据缺失】占位块提示,报告照常生成,不硬补数据。
=====================================================================
"""

import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path

# ---------------- 配置 ----------------
BASE_DIR = Path(__file__).resolve().parent     # 脚本所在目录,数据路径不受启动位置影响
RAW_DIR = BASE_DIR / "evidence" / "raw"
PARSED_DIR = BASE_DIR / "evidence" / "parsed"
RULE_DIR = BASE_DIR / "evidence" / "rule"
ML_DIR = BASE_DIR / "evidence" / "ml"
FINAL_DIR = BASE_DIR / "evidence" / "final"
REPORT_DIR = BASE_DIR / "report"               # 报告输出目录(运行时自动创建)

# 本模块不 import b01~b05(独立运行便于评分),只读取各模块输出的证据文件。
PARAM_KEYS = ("temperature", "humidity", "co2", "light")  # 与 B01 采集顺序一致
PARAM_NAMES = {"temperature": "温度", "humidity": "湿度", "co2": "CO₂浓度", "light": "光照"}
FEATURE_UNITS = {"temperature": "℃", "humidity": "%RH", "co2": "ppm", "light": "lux"}

# run_id 格式: run_YYYYmmdd_HHMMSS_<6位小写hex>
RUN_ID_RE = re.compile(r"^run_\d{8}_\d{6}_[0-9a-f]{6}$")


def setup_console():
    """强制标准输出为 UTF-8:输出被管道/重定向时避免 Windows 默认 GBK 导致乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass  # 环境不支持时保持默认,不影响程序运行


def is_valid_run_id(run_id):
    """校验 run_id 格式,防止拼接出非法文件路径(报告名由其拼出)。"""
    return bool(RUN_ID_RE.match(run_id))


def find_final_files():
    """列出 evidence/final 下的最终结论文件,按文件名倒序(时间戳定宽,字典序即时间序)。"""
    files = []
    for path in sorted(FINAL_DIR.glob("run_*_final.json"), reverse=True):
        if path.stem.endswith("_final") and is_valid_run_id(path.stem[:-6]):
            files.append(path)
    return files


def print_file_list(files):
    """打印可生成报告的 run_id 列表(编号 + 采集时间,按时间倒序)。"""
    print("\n---------------- 可生成报告的 run_id 列表 ----------------")
    print("共 %d 条 (按时间倒序):" % len(files))
    skipped = 0
    for i, path in enumerate(files, start=1):
        try:
            time_text = datetime.strptime(path.stem[4:19], "%Y%m%d_%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            skipped += 1
            continue
        print("  %d. %s  (%s)" % (i, path.stem[:-6], time_text))
    if skipped:
        print("(另有 %d 个文件名格式不符的文件被忽略)" % skipped)


def interactive_pick_run_id(files):
    """交互选择要生成报告的 run_id:输入序号或完整 run_id(可带 .json 后缀);
    返回 run_id;输入 q / Ctrl+C / EOF 时返回 None。"""
    while True:
        print_file_list(files)
        try:
            choice = input("请选择: 输入序号或完整 run_id (可带 .json),输入 q 退出: ").strip()
        except KeyboardInterrupt:
            print("\n已取消,程序安全退出。")
            return None
        except EOFError:
            print("\n输入已结束 (EOF),程序退出。")
            return None
        if not choice:
            print("输入不能为空,请重新输入。")
            continue
        if choice.lower() == "q":
            print("已退出。")
            return None
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(files):
                return files[idx - 1].stem[:-6]  # 去掉 "_final" 后缀
        # 允许带 .json / _final / _final.json 后缀(大小写不敏感),依次剥掉
        run_id = choice
        if run_id.lower().endswith(".json"):
            run_id = run_id[:-5]
        if run_id.lower().endswith("_final"):
            run_id = run_id[:-6]
        if is_valid_run_id(run_id) and (FINAL_DIR / f"{run_id}_final.json").exists():
            return run_id
        print("未匹配到该序号或 run_id,请重试。")


def load_json_or_none(path, required=()):
    """读取并轻量校验 JSON 文件,返回 (记录|None, 失败原因);任何失败不抛异常。"""
    if not path.exists():
        return None, "文件不存在"
    try:
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, "文件损坏(非合法 JSON)"
    except OSError as e:
        return None, "读取失败: %s" % e
    if not isinstance(record, dict):
        return None, "顶层结构不是对象"
    missing = [k for k in required if k not in record]
    if missing:
        return None, "缺少必要字段: %s" % ", ".join(missing)
    return record, ""


def load_final(run_id):
    """加载最终结论证据(必需输入):缺失/损坏属致命错误,返回 (记录|None, 提示文案)。"""
    path = FINAL_DIR / f"{run_id}_final.json"
    if not path.exists():
        return None, "[错误] 未找到 evidence/final/%s_final.json,请先运行 B05 生成最终结论证据。" % run_id
    record, reason = load_json_or_none(path, required=["final_conclusion", "trust_level"])
    if record is None:
        return None, "[错误] evidence/final/%s_final.json 内容无效: %s,请重新运行 B05。" % (run_id, reason)
    return record, ""


# ---------------- HTML 片段辅助 ----------------
esc = html.escape  # 所有来自 JSON 的内容一律转义后再进入 HTML


def fmt_num(x):
    """数值显示格式化: None 显示 —,其余按 :g。"""
    if x is None:
        return "—"
    return "%g" % x


def chip_html(text, level):
    """状态标签: 状态色恒配图标+文字,颜色从不单独承载含义。level: good/warn/serious/critical。"""
    icon = "✓" if level == "good" else "⚠"
    return '<span class="chip chip-%s">%s %s</span>' % (level, icon, esc(str(text)))


def stars_html(trust):
    """可信度星级: 纯文字,不用颜色编码。"""
    stars = {"高": "★★★", "中": "★★", "低": "★"}.get(trust, "—")
    return '<span class="stars">%s</span>' % esc(stars)


def placeholder_html(title, detail):
    """统一缺失占位块: 数据缺失时展示,不中断报告。"""
    return (
        '<div class="placeholder">'
        '<div class="ph-head"><span class="chip chip-serious">⚠ %s</span></div>'
        '<p>%s</p></div>'
    ) % (esc(title), esc(detail))


def bar_html(name, value, unit, lo, hi):
    """范围定位条: track=[lo,hi],fill 从 lo 到 clamp(value),越界标 serious 色文字。
    left%/width% 在 Python 内计算并 clamp,保证任何数据都不会产生非法 CSS。"""
    span = hi - lo
    if span <= 0:
        left, width = 0.0, 100.0
    else:
        ratio = max(0.0, min(1.0, (value - lo) / span))
        left, width = ratio * 100.0, ratio * 100.0
    out_of_range = value < lo or value > hi
    note = ' <span class="oob">超出范围</span>' if out_of_range else ""
    return (
        '<div class="bar-row">'
        '<div class="bar-head"><span class="bar-label">%s</span>'
        '<span class="bar-value">%s %s%s</span></div>'
        '<div class="bar-track"><div class="bar-fill" style="left:%.1f%%;width:%.1f%%"></div></div>'
        '<div class="bar-ticks"><span>%s %s</span><span>%s %s</span></div>'
        '</div>'
    ) % (esc(name), esc(fmt_num(value)), esc(unit), note, left, width,
         esc(fmt_num(lo)), esc(unit), esc(fmt_num(hi)), esc(unit))


# ---------------- 四个视图 ----------------
def view_raw(raw_rec, run_id):
    """① 原始数据视图: 4 张数值卡片 + 4 条 vs B01 合法采集范围的位置条。"""
    if raw_rec is None:
        return placeholder_html(
            "该视图数据缺失",
            "evidence/raw/%s.json 不存在或内容无效,请先运行 py b01_cli.py 采集该组数据。" % run_id,
        )
    data = raw_rec.get("data", {})
    ranges = raw_rec.get("valid_ranges", {})
    cards, bars = [], []
    for key in PARAM_KEYS:
        item = data.get(key, {})
        value = item.get("value")
        unit = item.get("unit", "")
        name = PARAM_NAMES[key]
        cards.append(
            '<div class="card"><div class="card-name">%s</div>'
            '<div class="card-value">%s<span class="card-unit">%s</span></div></div>'
            % (esc(name), esc(fmt_num(value)), esc(unit))
        )
        rng = ranges.get(key, {})
        lo, hi = rng.get("min"), rng.get("max")
        if isinstance(value, (int, float)) and isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
            bars.append(bar_html(name, value, unit, lo, hi))
        else:
            bars.append(placeholder_html(
                "该指标数据无效", "evidence/raw/%s.json 中指标 %s 的数据不完整。" % (run_id, key)))
    return (
        '<h2>① 原始数据视图</h2>'
        '<p class="view-sub">采集时间: %s (来自 B01 原始数据)</p>'
        '<div class="cards">%s</div>'
        '<div class="panel"><h3>数值在合法采集范围内的位置</h3>%s</div>'
    ) % (esc(str(raw_rec.get("collect_time", "—"))), "".join(cards), "".join(bars))


def view_parsed(parsed_rec, run_id):
    """② 指标解析视图: 判定表 + 4 条 vs 适宜区间的位置条。"""
    if parsed_rec is None:
        return placeholder_html(
            "该视图数据缺失",
            "evidence/parsed/%s_parsed.json 不存在或内容无效,请先运行 py b02_parse.py %s。" % (run_id, run_id),
        )
    indicators = parsed_rec.get("indicators", [])
    rules = parsed_rec.get("rules", {})
    rows, bars = [], []
    for item in indicators:
        key = item.get("key", "")
        name = item.get("name", key)
        value = item.get("value")
        unit = item.get("unit", "")
        verdict = item.get("verdict", "—")
        level = "good" if verdict == "适宜" else ("warn" if verdict in ("偏低", "偏高") else "serious")
        rows.append(
            '<tr><td>%s</td><td class="num">%s %s</td><td>%s</td><td class="basis">%s</td></tr>'
            % (esc(name), esc(fmt_num(value)), esc(unit), chip_html(verdict, level),
               esc(str(item.get("basis", "—"))))
        )
        rng = rules.get(key, {})
        lo, hi = rng.get("low"), rng.get("high")
        if isinstance(value, (int, float)) and isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
            bars.append(bar_html(name, value, unit, lo, hi))
    return (
        '<h2>② 指标解析视图</h2>'
        '<p class="view-sub">解析时间: %s (来自 B02 指标解析)</p>'
        '<div class="panel"><table class="grid"><thead><tr>'
        '<th>指标</th><th>数值</th><th>判定</th><th>判定依据</th></tr></thead>'
        '<tbody>%s</tbody></table></div>'
        '<div class="panel"><h3>数值在适宜区间内的位置</h3>%s</div>'
    ) % (esc(str(parsed_rec.get("parsed_time", "—"))), "".join(rows), "".join(bars))


def view_evidence(final, rule_rec, ml_rec, run_id):
    """③ 规则与机器学习证据视图: 规则面板 + ML 面板,优先完整证据文件,
    文件不可用且 final 投影非 null 时渲染投影精简版。"""
    # ---- 规则面板 ----
    rule_proj = final.get("rule_result")
    if rule_proj is None:
        rule_panel = placeholder_html(
            "规则证据缺失", "B05 融合时规则证据缺失(【证据缺失】),本面板无可展示内容。")
    elif rule_rec is None:
        concl = rule_proj.get("conclusion", "—")
        level = "good" if concl == "正常" else "warn"
        triggered = rule_proj.get("triggered_rules") or []
        trig_html = "".join(chip_html(r, "warn") for r in triggered) if triggered \
            else '<span class="muted">无触发规则</span>'
        rule_panel = (
            '<div class="panel"><div class="panel-head"><h3>规则证据 (B03)</h3>%s %s</div>'
            '<p class="muted">精简展示: evidence/rule/%s_rule.json 不可用,以下内容来自 B05 融合记录。</p>'
            '<p class="panel-basis"><span class="muted">规则结论依据: </span>%s</p></div>'
        ) % (chip_html("结论: " + str(concl), level), trig_html, run_id,
             esc(str(rule_proj.get("conclusion_basis", "—"))))
    else:
        concl = rule_rec.get("conclusion", "—")
        level = "good" if concl == "正常" else "warn"
        triggered = rule_rec.get("triggered_rules") or []
        trig_html = "".join(chip_html(r, "warn") for r in triggered) if triggered \
            else '<span class="muted">无触发规则</span>'
        rows = []
        for r in rule_rec.get("rule_results", []):
            matched = bool(r.get("matched"))
            trig_cell = '<span class="chip chip-warn">⚠ 触发</span>' if matched \
                else '<span class="muted">未触发</span>'
            row_class = ' class="hit"' if matched else ""
            rows.append(
                '<tr%s><td>%s %s</td><td>%s</td><td class="num">%s %s</td><td>%s</td><td>%s</td></tr>'
                % (row_class, esc(str(r.get("id", "—"))), esc(str(r.get("name", ""))),
                   esc(str(r.get("indicator", ""))), esc(fmt_num(r.get("value"))),
                   esc(str(r.get("unit", ""))), esc(str(r.get("verdict", "—"))), trig_cell)
            )
        rule_panel = (
            '<div class="panel"><div class="panel-head"><h3>规则证据 (B03)</h3>%s %s</div>'
            '<table class="grid"><thead><tr>'
            '<th>规则</th><th>指标</th><th>实测值</th><th>判定</th><th>触发</th></tr></thead>'
            '<tbody>%s</tbody></table>'
            '<p class="panel-basis"><span class="muted">规则结论依据: </span>%s</p></div>'
        ) % (chip_html("结论: " + str(concl), level), trig_html, "".join(rows),
             esc(str(rule_rec.get("conclusion_basis", "—"))))

    # ---- ML 面板 ----
    ml_proj = final.get("ml_result")
    if ml_proj is None:
        ml_panel = placeholder_html(
            "机器学习证据缺失", "B05 融合时机器学习证据缺失(【证据缺失】),本面板无可展示内容。")
    elif ml_rec is None:
        anom = bool(ml_proj.get("is_anomaly"))
        concl = "异常" if anom else "正常"
        level = "serious" if anom else "good"
        score = ml_proj.get("anomaly_score")
        ml_panel = (
            '<div class="panel"><div class="panel-head"><h3>机器学习证据 (B04)</h3>%s</div>'
            '<p class="muted">精简展示: evidence/ml/%s_ml.json 不可用,以下内容来自 B05 融合记录。</p>'
            '<p class="view-sub">异常得分: %s (阈值 0,越大越异常)</p>'
            '<p class="panel-basis"><span class="muted">ML 判定依据: </span>%s</p></div>'
        ) % (chip_html("结论: " + concl, level), run_id, esc(fmt_num(score)),
             esc(str(ml_proj.get("basis", "—"))))
    else:
        anom = bool(ml_rec.get("is_anomaly"))
        concl = "异常" if anom else "正常"
        level = "serious" if anom else "good"
        score = ml_rec.get("anomaly_score")
        score_html = ""
        if isinstance(score, (int, float)):
            domain_lo = min(score, 0.0)
            domain_hi = max(score, 1.0)
            if domain_hi > domain_lo:
                zero_pos = (0.0 - domain_lo) / (domain_hi - domain_lo) * 100.0
                fill_w = abs(score) / (domain_hi - domain_lo) * 100.0
                fill_l = zero_pos if score >= 0 else zero_pos - fill_w
                score_html = (
                    '<div class="bar-track score-track">'
                    '<div class="score-zero" style="left:%.1f%%"></div>'
                    '<div class="bar-fill" style="left:%.1f%%;width:%.1f%%"></div>'
                    '</div>'
                    '<p class="muted">异常得分 %.6g(阈值 0,越大越异常;竖线为 0 阈值位置)</p>'
                ) % (zero_pos, fill_l, fill_w, score)
        params = ", ".join("%s=%s" % (k, v) for k, v in (ml_rec.get("model_params") or {}).items())
        feats = "".join(
            '<tr><td>%s</td><td class="num">%s %s</td></tr>'
            % (esc(PARAM_NAMES.get(k, k)), esc(fmt_num(v)), esc(FEATURE_UNITS.get(k, "")))
            for k, v in (ml_rec.get("features") or {}).items()
        )
        skipped = ml_rec.get("skipped_files") or []
        skip_html = esc(", ".join(str(s) for s in skipped)) if skipped else '<span class="muted">无</span>'
        ml_panel = (
            '<div class="panel"><div class="panel-head"><h3>机器学习证据 (B04)</h3>%s</div>'
            '<p class="view-sub">模型: %s | %s</p>'
            '<p class="view-sub">训练集: %s 份历史文件;已跳过格式异常文件: %s</p>'
            '%s'
            '<table class="grid"><thead><tr><th>特征</th><th>数值</th></tr></thead>'
            '<tbody>%s</tbody></table>'
            '<p class="panel-basis"><span class="muted">ML 判定依据: </span>%s</p></div>'
        ) % (chip_html("结论: " + concl, level),
             esc(str(ml_rec.get("model", "—"))), esc(params),
             esc(fmt_num(ml_rec.get("training_size"))), skip_html,
             score_html, feats, esc(str(ml_rec.get("basis", "—"))))

    return '<h2>③ 规则与机器学习证据视图</h2>' + rule_panel + ml_panel


def view_conclusion(final):
    """④ 综合结论视图: hero banner(永不占位,final 为必需输入)。"""
    concl = str(final.get("final_conclusion", "—"))
    if concl.startswith("【证据缺失】"):
        concl_level = "serious"
    elif concl.startswith("正常"):
        concl_level = "good"
    else:
        concl_level = "warn"
    trust = final.get("trust_level", "—")
    status = final.get("evidence_status", {})
    rule_status = str(status.get("rule", "完整"))
    ml_status = str(status.get("ml", "完整"))
    rule_chip = chip_html("规则证据 " + rule_status, "good" if rule_status == "完整" else "serious")
    ml_chip = chip_html("机器学习证据 " + ml_status, "good" if ml_status == "完整" else "serious")
    reasons = final.get("degraded_reasons") or []
    if reasons:
        reasons_html = '<ul class="reasons">' + "".join(
            "<li>⚠ %s</li>" % esc(str(r)) for r in reasons) + "</ul>"
    else:
        reasons_html = '<p class="muted">无(两路证据一致,未发生可信降级)</p>'
    return (
        '<h2>④ 综合结论视图</h2>'
        '<div class="hero">'
        '<div class="hero-row">%s <span class="hero-trust">可信度 %s %s</span></div>'
        '<div class="hero-row">%s %s</div>'
        '<div class="hero-section"><h4>可信降级原因</h4>%s</div>'
        '<div class="hero-section"><h4>融合判定依据</h4><p>%s</p></div>'
        '<p class="muted hero-time">融合时间: %s</p>'
        '</div>'
    ) % (chip_html(concl, concl_level), esc(str(trust)), stars_html(trust),
         rule_chip, ml_chip, reasons_html,
         esc(str(final.get("basis", "—"))), esc(str(final.get("fused_time", "—"))))


# ---------------- HTML 大模板 ----------------
# 注意: 模板内 CSS 含大量花括号,只能用 {{TOKEN}} + .replace() 填充,
# 严禁 f-string / .format(),避免与 CSS 花括号冲突。
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{TITLE}}</title>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7;
  --surface: #fcfcfb;
  --ink: #0b0b0b;
  --ink-2: #52514e;
  --ink-3: #898781;
  --border: rgba(11, 11, 11, 0.10);
  --grid: #e1e0d9;
  --base: #c3c2b7;
  --bar: #2a78d6;
  --good: #0ca30c;
  --warn: #fab219;
  --serious: #ec835a;
  --crit: #d03b3b;
  --ok-text: #006300;
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --page: #0d0d0d;
    --surface: #1a1a19;
    --ink: #ffffff;
    --ink-2: #c3c2b7;
    --ink-3: #898781;
    --border: rgba(255, 255, 255, 0.10);
    --grid: #2c2c2a;
    --base: #383835;
    --bar: #3987e5;
    --ok-text: #0ca30c;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--page);
  color: var(--ink);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  line-height: 1.6;
}
.wrap { max-width: 960px; margin: 0 auto; padding: 24px 20px 48px; }
header { border-bottom: 1px solid var(--border); padding-bottom: 16px; }
h1 { font-size: 24px; margin: 0 0 8px; }
.sub { color: var(--ink-2); font-size: 13px; margin: 2px 0; }
.badge {
  display: inline-block; margin-top: 8px; padding: 2px 10px;
  border: 1px solid var(--border); border-radius: 999px;
  color: var(--ink-2); font-size: 12px;
}
nav.tabs { display: flex; gap: 8px; flex-wrap: wrap; margin: 20px 0; }
.tab {
  padding: 8px 16px; border: 1px solid var(--border); border-radius: 8px;
  background: var(--surface); color: var(--ink-2); cursor: pointer; font-size: 14px;
}
.tab.active { color: var(--ink); border-bottom: 3px solid var(--base); font-weight: 600; }
.view { display: none; }
.view.active { display: block; }
.panel, .card, .hero {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 16px 20px; margin: 16px 0;
}
h2 { font-size: 18px; margin: 24px 0 4px; }
h3 { font-size: 15px; margin: 0 0 10px; }
h4 { font-size: 13px; margin: 12px 0 4px; color: var(--ink-2); }
.view-sub { color: var(--ink-2); font-size: 13px; margin: 0 0 12px; }
.muted { color: var(--ink-3); font-size: 13px; }
.panel-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 8px; }
.panel-head h3 { margin: 0; }
.panel-basis { font-size: 13px; margin: 10px 0 0; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
.card { margin: 0; }
.card-name { color: var(--ink-3); font-size: 13px; }
.card-value { font-size: 26px; font-weight: 600; font-variant-numeric: tabular-nums; }
.card-unit { font-size: 13px; color: var(--ink-2); font-weight: 400; margin-left: 4px; }
table.grid { width: 100%; border-collapse: collapse; font-size: 13px; }
table.grid th, table.grid td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--grid); }
table.grid th { color: var(--ink-2); font-weight: 600; }
td.num { font-variant-numeric: tabular-nums; }
table.grid tr.hit { background: var(--grid); }
td.basis { color: var(--ink-2); }
.chip {
  display: inline-block; padding: 2px 10px; border-radius: 999px;
  font-size: 12px; border: 1px solid transparent; white-space: nowrap;
}
.chip-good { color: var(--ok-text); border-color: var(--good); background: transparent; }
.chip-warn { color: #332000; border-color: var(--warn); background: var(--warn); }
.chip-serious { color: #33160a; border-color: var(--serious); background: var(--serious); }
.chip-crit { color: #ffffff; border-color: var(--crit); background: var(--crit); }
.bar-row { margin: 14px 0; }
.bar-head { display: flex; justify-content: space-between; font-size: 13px; margin-bottom: 4px; }
.bar-label { color: var(--ink-2); }
.bar-value { font-variant-numeric: tabular-nums; font-weight: 600; }
.oob { color: var(--serious); font-size: 12px; font-weight: 400; }
.bar-track { position: relative; height: 8px; background: var(--grid); border-radius: 4px; }
.bar-fill { position: absolute; top: 0; height: 8px; background: var(--bar); border-radius: 4px; }
.bar-ticks { display: flex; justify-content: space-between; color: var(--ink-3); font-size: 11px; margin-top: 2px; }
.score-track { height: 10px; }
.score-zero { position: absolute; top: -3px; bottom: -3px; width: 2px; background: var(--base); }
.placeholder {
  background: var(--surface); border: 1px solid var(--border);
  border-left: 4px solid var(--serious); border-radius: 10px;
  padding: 14px 18px; margin: 16px 0;
}
.ph-head { margin-bottom: 6px; }
.placeholder p { margin: 0; color: var(--ink-2); font-size: 13px; }
.hero { padding: 24px 28px; }
.hero-row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin: 10px 0; }
.hero-trust { color: var(--ink-2); font-size: 14px; }
.stars { letter-spacing: 2px; }
.hero-section h4 { margin-top: 14px; }
.hero-section p { margin: 4px 0; font-size: 14px; }
.reasons { margin: 6px 0; padding-left: 20px; color: var(--ink-2); font-size: 13px; }
.hero-time { margin-top: 14px; }
footer {
  margin-top: 32px; padding-top: 12px; border-top: 1px solid var(--border);
  color: var(--ink-3); font-size: 12px;
}
</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>番茄大棚环境监测报告</h1>
  <p class="sub">run_id: {{RUN_ID}}</p>
  <p class="sub">采集时间: {{COLLECT_TIME}}{{COLLECT_SRC}}</p>
  <p class="sub">报告生成时间: {{GEN_TIME}}</p>
  <span class="badge">模块链路 B01 → B02 → B03/B04 → B05 → B06</span>
</header>
<nav class="tabs">
  <button class="tab active" data-view="view-raw" aria-selected="true">① 原始数据视图</button>
  <button class="tab" data-view="view-parsed" aria-selected="false">② 指标解析视图</button>
  <button class="tab" data-view="view-evidence" aria-selected="false">③ 规则与机器学习证据视图</button>
  <button class="tab" data-view="view-conclusion" aria-selected="false">④ 综合结论视图</button>
</nav>
<main>
<section id="view-raw" class="view active">{{VIEW_RAW}}</section>
<section id="view-parsed" class="view">{{VIEW_PARSED}}</section>
<section id="view-evidence" class="view">{{VIEW_EVIDENCE}}</section>
<section id="view-conclusion" class="view">{{VIEW_CONCLUSION}}</section>
</main>
<footer>本报告由 b06_report.py 自动生成,自包含、无外部依赖;数据全部来源于 evidence/ 证据文件,请勿手动修改。</footer>
</div>
<script>
function showView(name) {
  var tabs = document.querySelectorAll(".tab");
  var views = document.querySelectorAll(".view");
  for (var i = 0; i < tabs.length; i++) {
    var on = tabs[i].getAttribute("data-view") === name;
    tabs[i].classList.toggle("active", on);
    tabs[i].setAttribute("aria-selected", on ? "true" : "false");
  }
  for (var j = 0; j < views.length; j++) {
    views[j].classList.toggle("active", views[j].id === name);
  }
}
(function () {
  var tabs = document.querySelectorAll(".tab");
  for (var i = 0; i < tabs.length; i++) {
    tabs[i].addEventListener("click", function () {
      showView(this.getAttribute("data-view"));
    });
  }
})();
</script>
</body>
</html>
"""


# ---------------- 组装与保存 ----------------
def render_report(final, raw_rec, parsed_rec, rule_rec, ml_rec, run_id, gen_time, collect_time, collect_src):
    """组装四个视图并填充大模板(仅用 .replace,避免与 CSS 花括号冲突)。"""
    view_raw_html = view_raw(raw_rec, run_id)
    view_parsed_html = view_parsed(parsed_rec, run_id)
    view_evidence_html = view_evidence(final, rule_rec, ml_rec, run_id)
    view_conclusion_html = view_conclusion(final)
    title = "番茄大棚环境监测报告 — %s" % run_id
    return (HTML_TEMPLATE
            .replace("{{TITLE}}", esc(title))
            .replace("{{RUN_ID}}", esc(run_id))
            .replace("{{COLLECT_TIME}}", esc(str(collect_time)))
            .replace("{{COLLECT_SRC}}", esc(collect_src))
            .replace("{{GEN_TIME}}", esc(gen_time))
            .replace("{{VIEW_RAW}}", view_raw_html)
            .replace("{{VIEW_PARSED}}", view_parsed_html)
            .replace("{{VIEW_EVIDENCE}}", view_evidence_html)
            .replace("{{VIEW_CONCLUSION}}", view_conclusion_html))


def save_report(run_id, html_text):
    """保存报告到 report/{run_id}_report.html(自动创建目录,已存在则覆盖,
    幂等重复生成);写盘失败仅友好提示,程序不崩溃。"""
    try:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        file_path = REPORT_DIR / f"{run_id}_report.html"
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(html_text)
    except OSError as e:
        print("[错误] 报告写入失败: %s。" % e)
        return False
    print("✓ 报告已生成: %s" % file_path.relative_to(BASE_DIR).as_posix())
    return True


def print_summary(run_id, final, warnings):
    """控制台汇总: 结论、可信度与子证据警告。"""
    print("\n---------------- 报告生成汇总 ----------------")
    print("run_id: %s" % run_id)
    print("最终结论: %s (可信度: %s)" % (final.get("final_conclusion", "—"), final.get("trust_level", "—")))
    for w in warnings:
        print("[警告] %s" % w)


def print_usage():
    """打印用法说明。"""
    print("用法: py b06_report.py [run_id]")
    print("  直接生成: py b06_report.py run_20261022_220400_485a96  (可带 .json 后缀)")
    print("  不带参数: 进入交互模式,列出可生成报告的 run_id 供选择")


def generate_run(run_id):
    """生成单个 run_id 的报告: final 必需;子证据按需加载,缺失仅占位提示。"""
    final, reason = load_final(run_id)
    if final is None:
        print(reason)
        return False
    warnings = []
    status = final.get("evidence_status", {})

    raw_rec, raw_reason = load_json_or_none(RAW_DIR / f"{run_id}.json", required=["data"])
    if raw_rec is None:
        warnings.append("evidence/raw/%s.json: %s,视图内以占位提示展示。" % (run_id, raw_reason))

    parsed_rec, parsed_reason = load_json_or_none(PARSED_DIR / f"{run_id}_parsed.json", required=["indicators"])
    if parsed_rec is None:
        warnings.append("evidence/parsed/%s_parsed.json: %s,视图内以占位提示展示。" % (run_id, parsed_reason))

    # rule/ml 以 final 的 evidence_status 为权威: 标记缺失时不读取磁盘文件
    rule_rec = None
    if status.get("rule") == "完整":
        rule_rec, rule_reason = load_json_or_none(RULE_DIR / f"{run_id}_rule.json", required=["rule_results", "conclusion"])
        if rule_rec is None:
            warnings.append("evidence/rule/%s_rule.json: %s,视图内以占位提示展示。" % (run_id, rule_reason))

    ml_rec = None
    if status.get("ml") == "完整":
        ml_rec, ml_reason = load_json_or_none(ML_DIR / f"{run_id}_ml.json", required=["is_anomaly", "anomaly_score"])
        if ml_rec is None:
            warnings.append("evidence/ml/%s_ml.json: %s,视图内以占位提示展示。" % (run_id, ml_reason))

    gen_time = datetime.now().astimezone().isoformat(timespec="seconds")
    collect_time, collect_src = "", ""
    if raw_rec is not None and isinstance(raw_rec.get("collect_time"), str):
        collect_time = raw_rec["collect_time"]
    else:
        collect_time = final.get("fused_time", "—")
        collect_src = " (取自 B05 fused_time)"

    html_text = render_report(final, raw_rec, parsed_rec, rule_rec, ml_rec, run_id, gen_time, collect_time, collect_src)
    if not save_report(run_id, html_text):
        return False
    print_summary(run_id, final, warnings)
    return True


def main():
    """程序入口: 带参数直接生成报告,不带参数交互选择;Ctrl+C/EOF 均友好退出。"""
    setup_console()
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        print_usage()
        return
    if len(args) > 1:
        print("参数无效,最多接受 1 个 run_id。")
        print_usage()
        return
    try:
        if args:
            # 允许带 .json 后缀(大小写不敏感),统一去掉后缀再校验
            arg = args[0]
            run_id = arg[:-5] if arg.lower().endswith(".json") else arg
            if not is_valid_run_id(run_id):
                print("run_id 格式不正确: %s (应为 run_YYYYmmdd_HHMMSS_<6位hex>)" % arg)
                return
            if not generate_run(run_id):
                files = find_final_files()  # 失败时附带可用列表,便于修正
                if files:
                    print("\n可用的最终结论文件列表:")
                    print_file_list(files)
            return
        files = find_final_files()
        if not files:
            print("evidence/final 下暂无可用的最终结论文件,请先运行 B01~B05 完整链路。")
            return
        while True:
            run_id = interactive_pick_run_id(files)
            if run_id is None:
                return
            if generate_run(run_id):
                return  # 成功生成一次后退出;失败则回到列表重新选择
    except KeyboardInterrupt:
        print("\n检测到 Ctrl+C,程序已安全退出。")
        sys.exit(0)
    except EOFError:
        print("\n输入已结束 (EOF),程序退出。")
        sys.exit(0)


if __name__ == "__main__":
    main()
