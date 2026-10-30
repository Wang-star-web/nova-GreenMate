#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 GreenMate B09 — 整合门户(b01~s5 全部网页与功能的统一首页)
=====================================================================

【任务目标】
  把项目内全部网页入口整合到一个首页(门户页面/index.html):
  · B01 智能采集 Web(8123:采集表单 / LLM 问答 / A-B 验证 / 历史记录)
  · B02 数据记忆可视化 Web(8223:SQL 查询 + Chart.js 时序曲线)
  · B07 全部 HTML 报告(S1~S5 场景报告 + 6 组实验报告,file:// 直开)
  · B03/B04/B05/B06/B08 无网页模块:功能说明 + 终端命令卡片
  首页静态自包含(仅本地相对路径,零运行时网络依赖);报告列表由本脚本
  扫描 证据/b07/ 存档生成,新 run 后重新执行即可刷新。

【运行方式】
  py b09_portal.py              # (重新)生成整合首页 门户页面/index.html
  py b09_portal.py serve        # 一键同起三服务:门户 8300 + B01 8123 + B02 8223
                                # (B01/B02 已在运行则跳过;portal 同时可访问 报告/ 与 前端资源/)
  py b09_portal.py serve --portal-only   # 只起门户,不带 B01/B02
  py b09_portal.py serve --port 8300     # 自定义门户端口
  py b09_portal.py --help       # 查看用法说明

【说明】
  首页不调用任何 API/LLM;iframe 预览仅在对应服务已启动时显示内容,
  未启动时显示空白/浏览器错误提示,卡片上给出启动命令。
=====================================================================
"""

import html
import json
import os
import socket
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------- 配置 ----------------
BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "门户页面"
REPORT_DIR = BASE_DIR / "报告"
EVIDENCE_B07_DIR = BASE_DIR / "证据" / "b07"
ASSETS_DIR = BASE_DIR / "前端资源"
PORTAL_FILE = WEB_DIR / "index.html"

B01_URL = "http://127.0.0.1:8123"
B02_URL = "http://127.0.0.1:8223"
try:
    B09_WEB_PORT = int(os.environ.get("B09_WEB_PORT", "8300"))
except ValueError:
    B09_WEB_PORT = 8300

EXPERIMENT_ORDER = ("agree", "conflict", "missing_weather",
                    "current_only", "with_history", "with_history_prediction")
EXP_NAMES = {"agree": "证据一致", "conflict": "证据分歧(历史升温 vs 天气降温)",
             "missing_weather": "天气证据缺失", "current_only": "仅当前值",
             "with_history": "当前+3h历史", "with_history_prediction": "当前+3h历史+B06预测"}


def setup_console():
    """强制标准输出为 UTF-8:GBK 管道打印中文会崩溃(本项目已多次实测)。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def _esc(text):
    return html.escape(str(text), quote=True)


# ---------------- 报告扫描(只读存档)----------------
def scan_reports():
    """扫描 证据/b07/*,返回:
    scenarios: {scenario_id: [runs desc]}(仅无实验类型的场景 run)
    experiments: {experiment_type: [runs desc]}
    recent: 全部 run 按 run_id 倒序前 10。"""
    scenario_runs, experiment_runs, all_runs = {}, {}, []
    if not EVIDENCE_B07_DIR.exists():
        return scenario_runs, experiment_runs, all_runs
    for sub in EVIDENCE_B07_DIR.iterdir():
        if not sub.is_dir():
            continue
        try:
            j = json.loads((sub / "structured_judgment.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        row = {"run_id": sub.name, "scenario_id": j.get("scenario_id", "?"),
               "name": j.get("scenario_name", "?"), "conclusion": j.get("conclusion", "—"),
               "trust": j.get("trust_level", "—"), "attention": j.get("attention_level", "—"),
               "experiment_type": j.get("experiment_type")}
        all_runs.append(row)
        if row["experiment_type"]:
            experiment_runs.setdefault(row["experiment_type"], []).append(row)
        else:
            scenario_runs.setdefault(row["scenario_id"], []).append(row)
    for rows in scenario_runs.values():
        rows.sort(key=lambda r: r["run_id"], reverse=True)
    for rows in experiment_runs.values():
        rows.sort(key=lambda r: r["run_id"], reverse=True)
    all_runs.sort(key=lambda r: r["run_id"], reverse=True)
    return scenario_runs, experiment_runs, all_runs[:10]


def _report_link(run_id, label):
    return ('<a class="rep-link" href="../报告/%s_report.html" target="_blank">%s</a>'
            % (_esc(run_id), _esc(label)))


def _conclusion_chip(text):
    """结论状态 chip:实测结论仅 正常/需要关注/需要预警 三值,有界枚举映射;未知回退裸文本。"""
    cls = {"正常": "st-ok", "需要关注": "st-warn", "需要预警": "st-crit"}.get(str(text))
    if cls:
        return '<span class="status-chip %s">● %s</span>' % (cls, _esc(text))
    return _esc(text)


def _report_sections():
    scenario_runs, experiment_runs, recent = scan_reports()
    parts = []
    # ① 场景报告 S1~S5(每个场景取最新 run)
    scenario_order = sorted(scenario_runs.keys())
    if scenario_order:
        cards = []
        for sid in scenario_order:
            r = scenario_runs[sid][0]
            cards.append(
                '<div class="rep-card">'
                '<h4>%s S%s %s</h4>'
                '<div class="rep-meta">%s · 信任 %s</div>'
                '%s %s'
                "</div>" % (_esc("📄"), _esc(sid), _esc(r["name"]), _esc(r["run_id"]),
                            _esc(r["trust"]), _conclusion_chip(r["conclusion"]),
                            _report_link(r["run_id"], "打开报告 ↗")))
        parts.append('<div class="section"><h3>① 场景报告 S1~S5(每场景最新一次)</h3>'
                     '<div class="rep-grid">%s</div></div>' % "".join(cards))
    # ② 实验报告 6 组
    exp_cards = []
    for t in EXPERIMENT_ORDER:
        rows = experiment_runs.get(t) or []
        if not rows:
            exp_cards.append('<div class="rep-card"><h4>🧪 %s</h4><div class="rep-meta muted">(未运行,'
                             '执行 py b07_main.py exp %s 生成)</div></div>' % (EXP_NAMES[t], t))
            continue
        r = rows[0]
        exp_cards.append(
            '<div class="rep-card"><h4>🧪 %s</h4>'
            '<div class="rep-meta">%s · 信任 %s</div>'
            '%s %s'
            "</div>" % (_esc(EXP_NAMES[t]), _esc(r["run_id"]), _esc(r["trust"]),
                        _conclusion_chip(r["conclusion"]), _report_link(r["run_id"], "打开报告 ↗")))
    parts.append('<div class="section"><h3>② 实验报告(案例A 第二遍·第三遍,可复现实验)</h3>'
                 '<div class="rep-grid">%s</div></div>' % "".join(exp_cards))
    # ③ 最近历史
    if recent:
        rows = "".join(
            '<tr><td class="mono">%s</td><td>%s%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>'
            % (_esc(r["run_id"]), _esc(r["scenario_id"]), _esc("·实验·" if r["experiment_type"] else ""),
               _esc(r["name"]), _conclusion_chip(r["conclusion"]), _esc(r["trust"]),
               _report_link(r["run_id"], "报告 ↗"))
            for r in recent)
        parts.append('<div class="section"><h3>③ 最近运行(倒序,全部历史见 py b07_main.py list)</h3>'
                     '<table class="tbl"><tr><th>run_id</th><th>场景</th><th>名称</th><th>结论</th>'
                     '<th>信任</th><th>报告</th></tr>%s</table></div>' % rows)
    return "".join(parts) if parts else ('<div class="section"><h3>报告中心</h3>'
                                         '<div class="muted">暂无运行记录——执行 py b07_main.py run S1 '
                                         '或 py b07_main.py exp-all 后,重新运行 py b09_portal.py 刷新本页。</div></div>')


# ---------------- 门户页面模板 ----------------
_PORTAL_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GreenMate 番茄大棚环境监测系统 — 整合门户</title>
<style>
:root {
  color-scheme: light;
  --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink-2:#52514e; --ink-3:#898781;
  --border:rgba(11,11,11,0.10); --grid:#e1e0d9; --accent:#2a78d6;
  --good:#0ca30c; --warn:#fab219; --serious:#ec835a; --crit:#d03b3b;
  --ok-text:#006300; --warn-text:#8a5b00;
  /* 数字孪生变量 */
  --sky-a:#eaf4fb; --sky-b:#f2f7ec;
  --glass-a:rgba(165,214,255,0.30); --glass-b:rgba(210,235,255,0.14);
  --glass-edge:rgba(120,170,220,0.35);
  --roof-a:rgba(140,200,240,0.55); --roof-b:rgba(200,232,252,0.30);
  --ground-a:#d8e4d0; --ground-b:#b9c9ae;
  --sun:#fab219; --fruit:#d8402e; --cloud:rgba(190,205,215,0.9); --cloud-warn:rgba(150,165,160,0.95);
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --page:#0d0d0d; --surface:#1a1a19; --ink:#ffffff; --ink-2:#c3c2b7; --ink-3:#898781;
    --border:rgba(255,255,255,0.10); --grid:#2c2c2a; --accent:#3987e5;
    --ok-text:#0ca30c; --warn-text:#fab219;
    --sky-a:#12171c; --sky-b:#0f1512;
    --glass-a:rgba(90,145,200,0.20); --glass-b:rgba(70,110,160,0.10);
    --glass-edge:rgba(140,180,220,0.22);
    --roof-a:rgba(70,120,170,0.45); --roof-b:rgba(50,90,140,0.25);
    --ground-a:#232a24; --ground-b:#161c17;
    --sun:#ffb347; --fruit:#e05a45; --cloud:rgba(120,135,150,0.75); --cloud-warn:rgba(130,140,135,0.85);
  }
}
* { box-sizing:border-box; }
body { margin:0; font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
       background:var(--page); color:var(--ink); line-height:1.6; }
main { max-width:1200px; margin:0 auto; padding:20px 24px 70px; }
/* ---------- TWIN:CSS-START(三页同源复制块)---------- */
.twin { position: relative; perspective: 900px; perspective-origin: 50% 20%; height: 300px;
        overflow: hidden; border-radius: 16px; border: 1px solid var(--border);
        background: linear-gradient(180deg, var(--sky-a), var(--sky-b)); }
.twin-scene { position: absolute; left: 50%; top: 0; width: 460px; height: 320px; margin-left: -230px;
              transform-style: preserve-3d; }
.twin-sun { position: absolute; top: 26px; right: 36px; width: 64px; height: 64px;
            animation: sun-pulse 6s ease-in-out infinite; }
/* SVG 呈现属性不支持 var(),颜色一律经 CSS 规则注入 */
.twin-sun circle { fill: var(--sun); }
.twin-sun g { stroke: var(--sun); }
.twin-clouds svg path { fill: var(--cloud); }
.crops circle { fill: var(--fruit); }
.twin-clouds { position: absolute; top: 8px; left: 0; right: 0; height: 46px; opacity: .25; }
.twin-clouds svg { position: absolute; width: 96px; height: 40px; animation: cloud-drift 14s ease-in-out infinite alternate; }
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
           background: linear-gradient(180deg, var(--roof-a), var(--roof-b)); }
.gh-roof-a { transform: rotateX(-35deg); }
.gh-roof-b { transform: rotateX(35deg); }
.crops { position: absolute; left: 30px; bottom: 0; width: 200px; height: 96px; transform-style: preserve-3d;
         animation: crop-sway 8s ease-in-out infinite alternate; }
.crop-row { position: absolute; bottom: 0; width: 200px; height: 96px; }
.crop-row.r1 { transform: translateZ(-60px); }
.crop-row.r2 { transform: translateZ(0); }
.crop-row.r3 { transform: translateZ(60px); }
.mist { position: absolute; left: 0; right: 0; top: 12px; height: 120px; opacity: .15;
        animation: mist-drift 7s ease-in-out infinite alternate; }
.mist-drop { position: absolute; width: 26px; height: 14px; border-radius: 50%;
             background: radial-gradient(ellipse at center, rgba(255,255,255,.85), rgba(255,255,255,0));
             animation: drop-rise 3.2s linear infinite; animation-play-state: paused; }
.mist-drop:nth-child(1) { left: 12%; top: 78%; }
.mist-drop:nth-child(2) { left: 30%; top: 60%; animation-delay: .45s; }
.mist-drop:nth-child(3) { left: 48%; top: 84%; animation-delay: .9s; }
.mist-drop:nth-child(4) { left: 64%; top: 56%; animation-delay: 1.35s; }
.mist-drop:nth-child(5) { left: 80%; top: 80%; animation-delay: 1.8s; }
.mist-drop:nth-child(6) { left: 88%; top: 48%; animation-delay: 2.25s; }
.fan { position: absolute; right: 0; top: 52px; width: 44px; height: 44px;
       transform: rotateY(90deg) translateZ(130px); }
.fan svg { animation: fan-spin 2.4s linear infinite; }
.rays { position: absolute; left: 40px; top: 0; width: 180px; height: 150px; opacity: .85;
        transform: translateZ(60px); }
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
@keyframes twin-in { from { transform: rotateX(-4deg) rotateY(-26deg) scale(.92) translateY(26px); opacity: 0; }
                     to { transform: rotateX(-16deg) rotateY(-26deg) scale(1) translateY(0); opacity: 1; } }
@keyframes sun-pulse { 0%,100% { transform: scale(1); filter: brightness(1); } 50% { transform: scale(1.06); filter: brightness(1.08); } }
@keyframes ray-shimmer { 0%,100% { opacity: .5; } 50% { opacity: .9; } }
@keyframes mist-drift { from { transform: translateX(-5%); } to { transform: translateX(5%); } }
@keyframes drop-rise { 0% { transform: translateY(6px) scale(.7); opacity: 0; }
                       30% { opacity: .85; } 100% { transform: translateY(-10px) scale(1); opacity: 0; } }
@keyframes fan-spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
@keyframes cloud-drift { from { transform: translateX(-10%); } to { transform: translateX(10%); } }
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
.hero { display: grid; grid-template-columns: 1fr 460px; gap: 26px; align-items: center; margin: 10px 0 6px; }
@media (max-width: 900px) { .hero { grid-template-columns: 1fr; } }
.hero h1 { margin: 0 0 10px; font-size: 30px; line-height: 1.3; }
.hero .sub { color: var(--ink-2); font-size: 14px; margin: 0 0 18px; }
.section { margin: 30px 0 6px; }
.section > h3 { border-left: 4px solid var(--accent); padding-left: 10px; font-size: 17px; margin: 0 0 14px; }
.step-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 14px; }
.step-card { background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 18px 20px; }
.step-card .step-num { display: inline-flex; width: 30px; height: 30px; border-radius: 50%; background: var(--accent);
                        color: #fff; align-items: center; justify-content: center; font-weight: 700; margin-right: 8px; }
.step-card h4 { margin: 0 0 6px; font-size: 15px; display: flex; align-items: center; }
.step-card p { margin: 0 0 10px; color: var(--ink-3); font-size: 12.5px; }
.grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 16px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 18px 20px; }
.card h4 { margin: 0 0 6px; font-size: 16px; display: flex; align-items: center; gap: 8px; }
.card h4 svg { width: 20px; height: 20px; color: var(--accent); }
.card .url { color: var(--accent); font-size: 13px; word-break: break-all; }
.chips { margin: 10px 0 4px; }
.chip { display: inline-block; border-radius: 999px; padding: 3px 12px; font-size: 12px; margin: 0 6px 6px 0;
        border: 1px solid var(--border); background: var(--page); color: var(--ink-2); }
.cmd { background: var(--page); border: 1px solid var(--border); border-radius: 8px; padding: 6px 12px;
       font-family: Consolas, "Courier New", monospace; font-size: 12.5px; display: inline-block; margin: 4px 6px 4px 0; }
.note { color: var(--ink-3); font-size: 12px; margin-top: 8px; }
iframe { width: 100%; height: 340px; border: 1px solid var(--border); border-radius: 12px;
         background: var(--surface); margin-top: 12px; }
.rep-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; }
.rep-card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px; }
.rep-card h4 { margin: 0 0 6px; font-size: 14px; }
.rep-meta { color: var(--ink-3); font-size: 12px; margin-bottom: 8px; }
.rep-link { color: var(--accent); font-size: 13px; text-decoration: none; }
.rep-link:hover { text-decoration: underline; }
.status-chip { display: inline-block; border-radius: 999px; padding: 2px 10px; font-size: 12px; margin-right: 8px;
               background: var(--page); border: 1px solid var(--border); color: var(--ink-2); }
.status-chip.st-ok { color: var(--ok-text); border-color: var(--good); }
.status-chip.st-warn { color: var(--warn-text); border-color: var(--warn); }
.status-chip.st-crit { color: var(--crit); border-color: var(--crit); }
.tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.tbl th,.tbl td { border: 1px solid var(--border); padding: 7px 10px; text-align: left; }
.tbl th { background: var(--page); color: var(--ink-3); font-weight: 600; }
.mono { font-family: Consolas, "Courier New", monospace; font-size: 12px; }
.muted { color: var(--ink-3); }
.badge { display: inline-block; border-radius: 999px; padding: 2px 10px; font-size: 12px; margin-left: 8px;
         background: rgba(46,158,91,.12); color: var(--ok-text); border: 1px solid rgba(46,158,91,.35); }
.cli-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 12px; }
.cli-card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px; }
.cli-card h4 { margin: 0 0 4px; font-size: 14px; display: flex; align-items: center; gap: 6px; }
.cli-card .note { margin: 0 0 8px; }
.chain-row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 8px; }
.chain-badge { border-radius: 999px; padding: 4px 14px; font-size: 12.5px; background: var(--surface);
               border: 1px solid var(--border); color: var(--ink-2); }
.chain-badge.hl { background: var(--accent); color: #fff; border-color: var(--accent); }
.chain-arrow { color: var(--ink-3); }
footer { text-align: center; color: var(--ink-3); font-size: 12px; margin-top: 34px; }
a { color: var(--accent); }
</style>
</head>
<body>
<main>

  <div class="hero">
    <div>
      <h1>🌱 GreenMate 番茄大棚<br>环境监测系统 · 整合门户</h1>
      <p class="sub">B01~S5 全部网页与功能统一入口 · 数字孪生 · 报告列表扫描 证据/b07/ 存档(只读,生成于 {{GENERATED_AT}})</p>
      <span class="cmd">py b09_portal.py serve</span>
      <span class="cmd">py b01_cli.py web</span>
      <span class="cmd">py b02_memory.py web</span>
      <div class="note">serve 一键同起三服务:门户 8300 + B01 8123 + B02 8223,下方预览即刻显示。</div>
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
      <div class="twin-badge" id="twin-badge">数字孪生 · GreenMate 大棚</div>
    </div>
    <!-- TWIN:HTML-END -->
  </div>

  <div class="section">
    <h3>🚀 快速开始</h3>
    <div class="step-grid">
      <div class="step-card">
        <h4><span class="step-num">1</span>一键起服务</h4>
        <p>门户 + B01 + B02 三服务一并启动(已运行自动跳过)</p>
        <span class="cmd">py b09_portal.py serve</span>
      </div>
      <div class="step-card">
        <h4><span class="step-num">2</span>生成运行</h4>
        <p>单场景或一键 6 组可复现实验</p>
        <span class="cmd">py b07_main.py run S1</span>
        <span class="cmd">py b07_main.py exp-all</span>
      </div>
      <div class="step-card">
        <h4><span class="step-num">3</span>刷新首页</h4>
        <p>报告列表扫描存档生成,新 run 后重跑一次</p>
        <span class="cmd">py b09_portal.py</span>
      </div>
    </div>
  </div>

  <div class="section">
    <h3>🌐 在线服务(iframe 预览,serve 一键托管)</h3>
    <div class="grid2">
      <div class="card">
        <h4><svg viewBox="0 0 20 20" fill="none"><circle cx="10" cy="10" r="3" stroke="currentColor" stroke-width="1.5"/><path d="M1.5 10h2M16.5 10h2M10 1.5v2M10 16.5v2M4 4l1.4 1.4M14.6 14.6 16 16M16 4l-1.4 1.4M5.4 14.6 4 16" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>B01 智能采集 Web<span class="badge">8123</span></h4>
        <div class="url">http://127.0.0.1:8123</div>
        <div class="chips">
          <span class="chip">数字孪生联动</span><span class="chip">A/B 切换</span><span class="chip">LLM 问答</span><span class="chip">新建采集</span><span class="chip">历史记录</span>
        </div>
        <span class="cmd">py b01_cli.py web</span>
        <div class="note">serve 一键托管后此处即显示页面;API Key 读 DEEPSEEK_API_KEY → ANTHROPIC_AUTH_TOKEN。</div>
        <iframe src="http://127.0.0.1:8123" loading="lazy" title="B01 采集 Web 预览"></iframe>
      </div>
      <div class="card">
        <h4><svg viewBox="0 0 20 20" fill="none"><path d="M2 14 6 9l4 4 4-6 4 7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>B02 数据记忆可视化<span class="badge">8223</span></h4>
        <div class="url">http://127.0.0.1:8223</div>
        <div class="chips">
          <span class="chip">数字孪生联动</span><span class="chip">时序曲线</span><span class="chip">时间窗查询</span><span class="chip">窗口统计</span>
        </div>
        <span class="cmd">py b02_memory.py web</span>
        <div class="note">端口可用 --port 或环境变量 B02_WEB_PORT 覆盖。</div>
        <iframe src="http://127.0.0.1:8223" loading="lazy" title="B02 可视化 Web 预览"></iframe>
      </div>
    </div>
  </div>

  <div class="section">
    <h3>📊 报告中心(B07 HTML 报告,file:// 直开,零运行时网络请求)</h3>
    {{REPORT_SECTIONS}}
  </div>

  <div class="section">
    <h3>🛠 无网页模块(B03~B06 / B08)</h3>
    <div class="cli-grid">
      <div class="cli-card">
        <h4>🌤 B03 天气证据接口</h4>
        <div class="note">真实天气 API · A/B 与失败验证</div>
        <span class="cmd">py b03_weather.py forecast 北京</span>
        <span class="cmd">py b03_weather.py abtest</span>
        <span class="cmd">py b03_weather.py failtest</span>
      </div>
      <div class="cli-card">
        <h4>📚 B04 RAG 检索增强</h4>
        <div class="note">番茄知识库 · 引用回答 · 资料不足边界</div>
        <span class="cmd">py b04_rag.py search "番茄适宜温度"</span>
        <span class="cmd">py b04_rag.py ask "灰霉病怎么防治"</span>
        <span class="cmd">py b04_rag.py test</span>
      </div>
      <div class="cli-card">
        <h4>🤖 B05 工具智能体闭环</h4>
        <div class="note">LLM tool_calls · 四工具真实执行</div>
        <span class="cmd">py b05_agent.py run "分析当前大棚环境"</span>
        <span class="cmd">py b05_agent.py failtest</span>
      </div>
      <div class="cli-card">
        <h4>🧠 B06 传统机器学习闭环</h4>
        <div class="note">3h 窗口 → 1~2h WATCH/NORMAL 预测</div>
        <span class="cmd">py b06_ml.py train</span>
        <span class="cmd">py b06_ml.py predict</span>
      </div>
      <div class="cli-card">
        <h4>✅ B08 契约校验</h4>
        <div class="note">run_result.json 五项校验 + sha256</div>
        <span class="cmd">py b08_verify.py --json-path 证据/b07/&lt;run_id&gt;/run_result.json</span>
      </div>
    </div>
  </div>

  <div class="section">
    <h3>🔗 系统链路</h3>
    <div class="card">
      <div class="chain-row">
        <span class="chain-badge">B01 采集</span><span class="chain-arrow">→</span>
        <span class="chain-badge">B02 数据记忆</span><span class="chain-arrow">→</span>
        <span class="chain-badge">B03 天气</span><span class="chain-arrow">→</span>
        <span class="chain-badge">B04 RAG</span><span class="chain-arrow">→</span>
        <span class="chain-badge">B05 智能体</span><span class="chain-arrow">→</span>
        <span class="chain-badge">B06 预测</span><span class="chain-arrow">→</span>
        <span class="chain-badge hl">B07 系统组装</span><span class="chain-arrow">→</span>
        <span class="chain-badge">B08 契约校验</span><span class="chain-arrow">→</span>
        <span class="chain-badge hl">B09 整合门户</span>
      </div>
      <div class="note" style="margin-top:10px;">常用命令:<span class="cmd">py b07_main.py run S1..S5</span><span class="cmd">py b07_main.py exp-all</span><span class="cmd">py b07_main.py trace run_conflict_001 "需要关注"</span><span class="cmd">py b07_main.py check-all</span><span class="cmd">py b07_main.py list</span>完整说明见 文档/ 与 README.md。</div>
    </div>
  </div>

  <footer>GreenMate 整合门户 · 首页与全部报告均为静态页面(只读存档,零运行时 API/LLM 调用)· 生成于 {{GENERATED_AT}}</footer>
</main>
</body>
</html>
"""


def build_portal():
    """生成 门户页面/index.html(首页 + 全部功能入口)。返回是否成功。"""
    report_sections = _report_sections()
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    page = _PORTAL_TEMPLATE.replace("{{REPORT_SECTIONS}}", report_sections)
    page = page.replace("{{GENERATED_AT}}", _esc(generated_at))
    try:
        WEB_DIR.mkdir(parents=True, exist_ok=True)
        PORTAL_FILE.write_text(page, encoding="utf-8")
    except OSError as e:
        print("⚠ 门户页面写入失败: %s" % e)
        return False
    print("✓ 整合首页已生成: %s" % PORTAL_FILE)
    print("  浏览器打开该文件即可查看首页与全部功能(也可执行 py b09_portal.py serve)。")
    return True


# ---------------- 本地门户服务(可选)----------------
_CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript",
                  ".json": "application/json; charset=utf-8", ".css": "text/css; charset=utf-8",
                  ".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml",
                  ".ico": "image/x-icon"}


class _PortalHandler(BaseHTTPRequestHandler):
    """路径映射:/ → 门户页面/index.html;/报告/* → 报告/;/前端资源/* → 前端资源/。只读静态服务。"""

    def _send_file(self, path):
        try:
            data = path.read_bytes()
        except OSError:
            self.send_error(404, "file not found")   # 错误消息只用 ASCII(latin-1 编码边界)
            return
        self.send_response(200)
        self.send_header("Content-Type", _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    protocol_version = "HTTP/1.1"   # keep-alive:_send_file 均显式 Content-Length

    def do_GET(self):
        url = self.path.split("?", 1)[0]
        if url in ("/", "/index.html"):
            self._send_file(PORTAL_FILE)
        elif url.startswith("/报告/") and ".." not in url:
            self._send_file(BASE_DIR / url.lstrip("/"))
        elif url.startswith("/前端资源/") and ".." not in url:
            self._send_file(BASE_DIR / url.lstrip("/"))
        else:
            self.send_error(404, "path not served")   # 错误消息只用 ASCII(latin-1 编码边界)

    def log_message(self, fmt, *args):
        print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), fmt % args))


def _port_in_use(port, host="127.0.0.1"):
    """connect 探测端口是否已有服务监听(只读探测,不占用端口)。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False


def _start_child(name, target, port):
    """daemon 线程启动子服务(b01_cli/b02_memory import 零副作用,已核实)。"""
    print("· %s 后台启动(线程)→ http://127.0.0.1:%d" % (name, port), flush=True)
    threading.Thread(target=target, kwargs={"port": port}, name=name, daemon=True).start()


def serve(port, with_children=True):
    """一键同起三服务:8123 B01 + 8223 B02(daemon 线程,已在运行则跳过)+ 门户(主线程 8300)。
    Ctrl+C 停止门户后子线程随进程退出。"""
    build_portal()
    if with_children:
        import b01_cli        # 延迟导入:仅 serve 需要(纯 build 路径零依赖)
        import b02_memory
        if _port_in_use(8123):
            print("· B01 已在运行(127.0.0.1:8123),跳过启动", flush=True)
        else:
            b01_cli.B01_WEB_PORT = 8123   # B01 函数无 port 形参:线程起前设模块属性
            _start_child("b01-web", b01_cli.run_web_server, 8123)
        if _port_in_use(8223):
            print("· B02 已在运行(127.0.0.1:8223),跳过启动", flush=True)
        else:
            _start_child("b02-web", b02_memory.run_web_server, 8223)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), _PortalHandler)
    except OSError as e:
        print("⚠ 门户端口 %d 启动失败(可能被占用): %s" % (port, e))
        return                     # daemon 子线程随进程退出,无泄漏
    print("✓ GreenMate 整合门户已启动: http://127.0.0.1:%s/" % port)
    if with_children:
        print("  · B01(8123)/B02(8223)由本进程一并托管,iframe 即刻显示;亦可单独启动")
    print("  Ctrl+C 停止全部服务。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n✓ 门户已停止(子服务 daemon 线程随进程退出)。")


def print_usage():
    print("""GreenMate B09 整合门户 — 用法
  py b09_portal.py              # (重新)生成整合首页 门户页面/index.html(含最新报告列表)
  py b09_portal.py serve        # 一键同起三服务:门户 8300 + B01 8123 + B02 8223
                                # (B01/B02 已在运行则跳过;iframe 即刻显示内容)
  py b09_portal.py serve --portal-only   # 只起门户,不带 B01/B02
  py b09_portal.py serve --port 8300     # 自定义门户端口(或环境变量 B09_WEB_PORT)
  py b09_portal.py --help       # 查看用法说明

首页内容:
  · 3D 数字孪生大棚 hero + B01(8123)/B02(8223)服务卡(iframe 预览,一键托管)
  · 报告中心:S1~S5 场景报告 + 6 组实验报告 + 最近运行(全部来自 证据/b07/ 存档,只读)
  · B03/B04/B05/B06/B08 无网页模块:功能图标卡 + 终端命令
新 run 生成后重新执行 py b09_portal.py 即可刷新报告列表。""")


def main():
    setup_console()
    args = sys.argv[1:]
    if not args:
        build_portal()   # 默认行为:重新生成整合首页
        return
    if args[0] in ("-h", "--help", "help"):
        print_usage()
        return
    cmd = args[0]
    if cmd == "build":
        build_portal()
    elif cmd == "serve":
        port = B09_WEB_PORT
        portal_only = "--portal-only" in args
        rest = [a for a in args[1:] if a != "--portal-only"]
        if "--port" in rest:
            i = rest.index("--port")
            if i + 1 >= len(rest):
                print("⚠ --port 需要参数,如 --port 8300")
                return
            try:
                port = int(rest[i + 1])
            except ValueError:
                print("⚠ 端口必须是整数")
                return
        serve(port, with_children=not portal_only)
    else:
        print("未知子命令: %s" % cmd)
        print_usage()


if __name__ == "__main__":
    main()
