#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 GreenMate B08 — 整体交付验证脚本
=====================================================================

【任务目标】
  校验整套项目交付完整性: 输入 run_id,自动检查 raw、parsed、rule、
  ml、final 五个证据文件与 HTML 报告是否存在且内容有效,输出逐项
  校验清单(哪些 OK / 缺失 / 异常),并计算确定性「结果指纹」——
  重复运行同一 run_id 指纹一致即确认校验结果可复现、证据未被改动。

【运行方式】
  py b08_verify.py run_20261022_220400_485a96   # 直接校验指定 run_id
  py b08_verify.py                              # 不带参数:列出可校验的 run_id 交互选择
  py b08_verify.py --help                       # 查看用法说明

【校验规则】
  六个产物逐一检查,三态结论:
  ✓ OK   文件存在且内容有效(JSON 可解析且 run_id 字段一致;HTML 非空)
  ✗ 缺失 文件不存在
  ⚠ 异常 文件存在但内容无效(JSON 损坏 / run_id 不一致 / HTML 为空)
=====================================================================
"""

import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

# ---------------- 配置 ----------------
BASE_DIR = Path(__file__).resolve().parent     # 脚本所在目录,数据路径不受启动位置影响

# 六个交付产物(标签, 相对路径模板, 是否 JSON): 与 B01~B07 的输出契约一致
CHECK_ITEMS = [
    ("原始数据", "evidence/raw/{run_id}.json", True),
    ("解析证据", "evidence/parsed/{run_id}_parsed.json", True),
    ("规则证据", "evidence/rule/{run_id}_rule.json", True),
    ("ML证据",   "evidence/ml/{run_id}_ml.json", True),
    ("融合结论", "evidence/final/{run_id}_final.json", True),
    ("HTML报告", "report/{run_id}_report.html", False),
]

# run_id 格式: run_YYYYmmdd_HHMMSS_<6位小写hex>
RUN_ID_RE = re.compile(r"^run_\d{8}_\d{6}_[0-9a-f]{6}$")

# 产物文件名后缀(用于从文件名还原 run_id,顺序任意: 各后缀互不重叠)
STEM_SUFFIXES = ("_final", "_rule", "_ml", "_parsed", "_report")


def setup_console():
    """强制标准输出为 UTF-8:输出被管道/重定向时避免 Windows 默认 GBK 导致乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass  # 环境不支持时保持默认,不影响程序运行


def is_valid_run_id(run_id):
    """校验 run_id 格式,防止拼接出非法文件路径。"""
    return bool(RUN_ID_RE.match(run_id))


def file_status(path, run_id, is_json):
    """检查单个产物文件,返回 (状态, 内容摘要|None)。状态: OK / 缺失 / 异常。"""
    if not path.exists():
        return "缺失", None
    try:
        content = path.read_bytes()
    except OSError:
        return "异常", None
    if is_json:
        try:
            record = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return "异常", None
        if not isinstance(record, dict) or record.get("run_id") != run_id:
            return "异常", None
    else:
        if not content.strip():
            return "异常", None
    return "OK", hashlib.sha256(content).hexdigest()[:12]


def fingerprint(rows):
    """结果指纹: 对每行 rel|status|digest 计算 sha256。确定性,不含校验时间;
    任一文件缺失、内容变化或状态变化都会改变指纹。"""
    h = hashlib.sha256()
    for r in rows:
        h.update(("%s|%s|%s\n" % (r["rel"], r["status"], r["digest"] or "-")).encode("utf-8"))
    return h.hexdigest()


def verify_run(run_id):
    """逐一检查 6 个产物文件,返回 (行列表, 结果指纹)。"""
    rows = []
    for label, template, is_json in CHECK_ITEMS:
        rel = template.format(run_id=run_id)
        status, digest = file_status(BASE_DIR / rel, run_id, is_json)
        rows.append({"label": label, "rel": rel, "status": status, "digest": digest})
    return rows, fingerprint(rows)


def print_checklist(run_id, rows, fp):
    """打印校验清单: 逐项状态 + 汇总 + 结果指纹。"""
    marks = {"OK": "✓", "缺失": "✗", "异常": "⚠"}
    print("\n================ 交付完整性校验清单 ================")
    print("run_id: %s" % run_id)
    print("校验时间: %s (仅展示,不计入结果指纹)"
          % datetime.now().astimezone().isoformat(timespec="seconds"))
    for i, r in enumerate(rows, start=1):
        digest_text = ("  sha256: %s" % r["digest"]) if r["digest"] else ""
        print("  [%d/6] %-42s %s %s%s" % (i, r["rel"], marks[r["status"]], r["status"], digest_text))
    ok = sum(1 for r in rows if r["status"] == "OK")
    missing = sum(1 for r in rows if r["status"] == "缺失")
    bad = sum(1 for r in rows if r["status"] == "异常")
    print("汇总: %d OK / %d 缺失 / %d 异常 (共 6 项)" % (ok, missing, bad))
    print("结果指纹: %s" % fp)
    print("提示: 重复运行同一 run_id,结果指纹一致即确认结果可复现。")


def find_all_run_ids():
    """六个目录的并集: 返回 [{run_id, count}],count 为该 run_id 已具备的产物数,按 run_id 倒序。"""
    counts = {}
    for _, template, _ in CHECK_ITEMS:
        for path in sorted(BASE_DIR.glob(template.format(run_id="*"))):
            stem = path.stem
            for suffix in STEM_SUFFIXES:
                if stem.endswith(suffix):
                    stem = stem[:-len(suffix)]
                    break
            if is_valid_run_id(stem):
                counts[stem] = counts.get(stem, 0) + 1
    return sorted(({"run_id": k, "count": v} for k, v in counts.items()),
                  key=lambda x: x["run_id"], reverse=True)


def print_union_list(items):
    """打印全部可校验的 run_id 列表(附产物完整度)。"""
    print("\n---------------- 可校验的 run_id 列表 ----------------")
    print("共 %d 条 (按时间倒序):" % len(items))
    for i, item in enumerate(items, start=1):
        time_text = datetime.strptime(item["run_id"][4:19], "%Y%m%d_%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
        print("  %d. %s  (%s)  产物 %d/6" % (i, item["run_id"], time_text, item["count"]))


def interactive_pick_run_id(items):
    """交互选择要校验的 run_id;返回 run_id;输入 q / Ctrl+C / EOF 时返回 None。"""
    valid = {item["run_id"] for item in items}
    while True:
        print_union_list(items)
        try:
            choice = input("请选择: 输入序号或完整 run_id,输入 q 退出: ").strip()
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
            if 1 <= idx <= len(items):
                return items[idx - 1]["run_id"]
        if is_valid_run_id(choice) and choice in valid:
            return choice
        print("未匹配到该序号或 run_id,请重试。")


def print_usage():
    """打印用法说明。"""
    print("用法: py b08_verify.py [run_id]")
    print("  直接校验: py b08_verify.py run_20261022_220400_485a96")
    print("  不带参数: 进入交互模式,列出可校验的 run_id 供选择")


def main():
    """程序入口: 带参数直接校验,不带参数交互选择;Ctrl+C/EOF 均友好退出。
    缺失/异常是校验清单的结论,不是脚本错误,退出码恒为 0。"""
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
            arg = args[0]
            run_id = arg[:-5] if arg.lower().endswith(".json") else arg
            if not is_valid_run_id(run_id):
                print("run_id 格式不正确: %s (应为 run_YYYYmmdd_HHMMSS_<6位hex>)" % arg)
                return
            rows, fp = verify_run(run_id)
            print_checklist(run_id, rows, fp)
            return
        items = find_all_run_ids()
        if not items:
            print("项目中暂未发现任何 run_id 产物,请先运行 B07 完整流水线。")
            return
        while True:
            run_id = interactive_pick_run_id(items)
            if run_id is None:
                return
            rows, fp = verify_run(run_id)
            print_checklist(run_id, rows, fp)
            return  # 校验一次后退出
    except KeyboardInterrupt:
        print("\n检测到 Ctrl+C,程序已安全退出。")
        sys.exit(0)
    except EOFError:
        print("\n输入已结束 (EOF),程序退出。")
        sys.exit(0)


if __name__ == "__main__":
    main()
