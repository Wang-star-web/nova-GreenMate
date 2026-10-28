#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 GreenMate B05 — 证据融合与可信降级模块
=====================================================================

【任务目标】
  融合 B03 规则证据 evidence/rule/{run_id}_rule.json 与 B04 机器学习证据
  evidence/ml/{run_id}_ml.json,生成大棚最终评估结论,保存到
  evidence/final/{run_id}_final.json。
  核心要求: 可信降级 —— 两路证据一致时可信度高,不一致时降级;
  任意一份证据缺失时明确标注【证据缺失】,不硬补结果。

【运行方式】
  py b05_fusion.py run_20261022_220400_485a96   # 直接融合指定 run_id
  py b05_fusion.py                              # 不带参数:列出可融合数据交互选择
  py b05_fusion.py --help                       # 查看用法说明

【融合策略(规则优先)】
  规则证据为最终结论依据,机器学习证据作为佐证影响可信度:
  正常+正常 → 正常(高)        正常+异常 → 正常,建议人工关注(中)
  预警+异常 → 需要预警(高)    预警+正常 → 需要预警,ML未检出(中)
  任一缺失 → 标注【证据缺失】,结论仅基于现有证据,可信度低
  两者均缺失 → 【证据缺失】无法融合判定,可信度低
=====================================================================
"""

import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path

# ---------------- 配置 ----------------
BASE_DIR = Path(__file__).resolve().parent     # 脚本所在目录,数据路径不受启动位置影响
RULE_DIR = BASE_DIR / "evidence" / "rule"      # B03 规则证据目录
ML_DIR = BASE_DIR / "evidence" / "ml"          # B04 机器学习证据目录
FINAL_DIR = BASE_DIR / "evidence" / "final"    # B05 最终融合结果目录(运行时自动创建)

# 本模块不 import b03/b04(独立运行便于评分),只读取两者输出的证据 JSON。
# run_id 格式: run_YYYYmmdd_HHMMSS_<6位小写hex>
RUN_ID_RE = re.compile(r"^run_\d{8}_\d{6}_[0-9a-f]{6}$")

MISSING = "【证据缺失】"  # 需求要求明确标注的字面量


def setup_console():
    """强制标准输出为 UTF-8:输出被管道/重定向时避免 Windows 默认 GBK 导致乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass  # 环境不支持时保持默认,不影响程序运行


def is_valid_run_id(run_id):
    """校验 run_id 格式,防止拼接出非法文件路径(final 写路径由其拼出)。"""
    return bool(RUN_ID_RE.match(run_id))


def find_union_files():
    """收集规则与机器学习证据的并集:返回 [{run_id, has_rule, has_ml}],按 run_id 倒序。"""
    items = {}
    for path in RULE_DIR.glob("run_*_rule.json"):
        if path.stem.endswith("_rule") and is_valid_run_id(path.stem[:-5]):
            items.setdefault(path.stem[:-5], {"run_id": path.stem[:-5], "has_rule": False, "has_ml": False})["has_rule"] = True
    for path in ML_DIR.glob("run_*_ml.json"):
        if path.stem.endswith("_ml") and is_valid_run_id(path.stem[:-3]):
            items.setdefault(path.stem[:-3], {"run_id": path.stem[:-3], "has_rule": False, "has_ml": False})["has_ml"] = True
    return sorted(items.values(), key=lambda x: x["run_id"], reverse=True)


def print_union_list(items):
    """打印可融合评估的数据列表,标注每份 run_id 已有的证据类型。"""
    print("\n---------------- 可融合评估的数据列表 ----------------")
    print(f"共 {len(items)} 条 (按时间倒序):")
    for i, item in enumerate(items, start=1):
        time_text = datetime.strptime(item["run_id"][4:19], "%Y%m%d_%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
        rule_mark = "规则✓" if item["has_rule"] else "规则—"
        ml_mark = "ML✓" if item["has_ml"] else "ML—"
        print(f"  {i}. {item['run_id']}  ({time_text})  {rule_mark} {ml_mark}")


def interactive_pick_run_id(items):
    """交互选择要融合的 run_id:输入序号或完整 run_id(可带 .json / _rule.json /
    _ml.json 后缀);返回 run_id;输入 q / Ctrl+C / EOF 时返回 None。"""
    union = {item["run_id"] for item in items}
    while True:
        print_union_list(items)
        try:
            choice = input("请选择: 输入序号或完整 run_id (可带 .json / _rule.json / _ml.json),输入 q 退出: ").strip()
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
        # 允许带 .json / _rule / _ml / _rule.json / _ml.json 后缀(大小写不敏感),依次剥掉
        run_id = choice
        if run_id.lower().endswith(".json"):
            run_id = run_id[:-5]
        if run_id.lower().endswith("_rule"):
            run_id = run_id[:-5]
        if run_id.lower().endswith("_ml"):
            run_id = run_id[:-3]
        if is_valid_run_id(run_id) and run_id in union:
            return run_id
        print("未匹配到该序号或 run_id,请重试。")


def load_rule_evidence(run_id):
    """读取 B03 规则证据,返回 (状态, 记录|None, 原因);状态为 "完整" 或 "【证据缺失】"。
    文件存在但内容无效时按缺失处理,不硬补结果。"""
    path = RULE_DIR / f"{run_id}_rule.json"
    if not path.exists():
        return MISSING, None, "规则证据文件不存在"
    try:
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
    except json.JSONDecodeError:
        print(f"规则证据文件内容无效,按缺失处理: {path.name} — 文件内容不是合法 JSON")
        return MISSING, None, "规则证据文件内容无效,按缺失处理"
    except OSError as e:
        print(f"规则证据文件内容无效,按缺失处理: {path.name} — 读取失败: {e}")
        return MISSING, None, "规则证据文件内容无效,按缺失处理"
    problem = None
    if not isinstance(record, dict):
        problem = "顶层应为 JSON 对象"
    elif record.get("run_id") != run_id:
        problem = "run_id 与文件名不一致"
    elif record.get("conclusion") not in ("正常", "需要预警"):
        problem = f"conclusion 无效 ({record.get('conclusion')!r})"
    elif not isinstance(record.get("triggered_rules"), list):
        problem = "triggered_rules 不是数组"
    elif not isinstance(record.get("conclusion_basis"), str):
        problem = "缺少 conclusion_basis 字段"
    if problem:
        print(f"规则证据文件内容无效,按缺失处理: {path.name} — {problem}")
        return MISSING, None, "规则证据文件内容无效,按缺失处理"
    return "完整", record, ""


def load_ml_evidence(run_id):
    """读取 B04 机器学习证据,返回 (状态, 记录|None, 原因);状态为 "完整" 或 "【证据缺失】"。
    文件存在但内容无效时按缺失处理,不硬补结果。"""
    path = ML_DIR / f"{run_id}_ml.json"
    if not path.exists():
        return MISSING, None, "机器学习证据文件不存在"
    try:
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
    except json.JSONDecodeError:
        print(f"机器学习证据文件内容无效,按缺失处理: {path.name} — 文件内容不是合法 JSON")
        return MISSING, None, "机器学习证据文件内容无效,按缺失处理"
    except OSError as e:
        print(f"机器学习证据文件内容无效,按缺失处理: {path.name} — 读取失败: {e}")
        return MISSING, None, "机器学习证据文件内容无效,按缺失处理"
    problem = None
    if not isinstance(record, dict):
        problem = "顶层应为 JSON 对象"
    elif record.get("run_id") != run_id:
        problem = "run_id 与文件名不一致"
    elif not isinstance(record.get("is_anomaly"), bool):
        problem = f"is_anomaly 不是布尔值 ({record.get('is_anomaly')!r})"
    else:
        score = record.get("anomaly_score")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            problem = f"anomaly_score 不是有限数字 ({score!r})"
    if problem is None and not isinstance(record.get("basis"), str):
        problem = "缺少 basis 字段"
    if problem:
        print(f"机器学习证据文件内容无效,按缺失处理: {path.name} — {problem}")
        return MISSING, None, "机器学习证据文件内容无效,按缺失处理"
    return "完整", record, ""


def fuse(rule_status, rule_record, ml_status, ml_record, rule_reason="", ml_reason=""):
    """融合两路证据生成最终结论(规则优先,纯函数,无 I/O)。

    返回 {final_conclusion, trust_level(高/中/低), degraded_reasons, basis}。
    任一证据缺失时,结论与依据中均明确标注【证据缺失】,不硬补结果。
    """
    rule_ok = rule_status == "完整"
    ml_ok = ml_status == "完整"

    # ---- 两路证据齐全 ----
    if rule_ok and ml_ok:
        rule_conclusion = rule_record["conclusion"]
        ml_anomaly = ml_record["is_anomaly"]
        if rule_conclusion == "正常" and not ml_anomaly:
            return {
                "final_conclusion": "正常",
                "trust_level": "高",
                "degraded_reasons": [],
                "basis": "规则判定「正常」与机器学习判定「正常」一致,两路证据相互印证,可信度: 高。",
            }
        if rule_conclusion == "需要预警" and ml_anomaly:
            return {
                "final_conclusion": "需要预警(两路一致确认)",
                "trust_level": "高",
                "degraded_reasons": [],
                "basis": "规则判定「需要预警」与机器学习判定「异常」一致,两路证据相互印证,可信度: 高。",
            }
        if rule_conclusion == "正常" and ml_anomaly:
            return {
                "final_conclusion": "正常(ML检测到偏离历史分布,建议人工关注)",
                "trust_level": "中",
                "degraded_reasons": ["两路证据不一致"],
                "basis": "规则判定「正常」,机器学习判定「异常」,两路证据不一致。按规则优先策略采纳规则结论「正常」,但 ML 检测到偏离历史分布,建议人工复核。可信度降至「中」。",
            }
        return {
            "final_conclusion": "需要预警(ML未检出异常)",
            "trust_level": "中",
            "degraded_reasons": ["两路证据不一致"],
            "basis": "规则判定「需要预警」,机器学习判定「正常」,两路证据不一致。按规则优先策略采纳规则结论「需要预警」,但 ML 未检出异常,建议人工复核。可信度降至「中」。",
        }

    # ---- 至少一路证据缺失:明确标注【证据缺失】,不硬补结果 ----
    if not rule_ok and not ml_ok:
        return {
            "final_conclusion": "【证据缺失】无法融合判定",
            "trust_level": "低",
            "degraded_reasons": ["规则证据缺失", "机器学习证据缺失"],
            "basis": "【证据缺失】规则与机器学习证据均缺失,无法融合判定,不硬补结果。可信度降至「低」。",
        }
    if not rule_ok:
        ml_text = "异常" if ml_record["is_anomaly"] else "正常"
        reason = rule_reason or "规则证据缺失"
        return {
            "final_conclusion": f"【证据缺失】结论仅基于机器学习证据: {ml_text}",
            "trust_level": "低",
            "degraded_reasons": [reason],
            "basis": f"【证据缺失】{reason},不硬补结果,结论仅基于机器学习证据(判定「{ml_text}」)。可信度降至「低」。",
        }
    rule_text = rule_record["conclusion"]
    reason = ml_reason or "机器学习证据缺失"
    return {
        "final_conclusion": f"【证据缺失】结论仅基于规则证据: {rule_text}",
        "trust_level": "低",
        "degraded_reasons": [reason],
        "basis": f"【证据缺失】{reason},不硬补结果,结论仅基于规则证据(规则判定「{rule_text}」)。可信度降至「低」。",
    }


def build_final_record(run_id, rule_status, rule_record, ml_status, ml_record, fusion):
    """组装最终融合记录(证据文件内容);缺失一侧的投影为 null,不硬补。"""
    rule_result = None
    if rule_status == "完整":
        rule_path = RULE_DIR / f"{run_id}_rule.json"
        rule_result = {
            "source_file": rule_path.relative_to(BASE_DIR).as_posix(),
            "conclusion": rule_record["conclusion"],
            "triggered_rules": rule_record["triggered_rules"],
            "conclusion_basis": rule_record["conclusion_basis"],
        }
    ml_result = None
    if ml_status == "完整":
        ml_path = ML_DIR / f"{run_id}_ml.json"
        ml_result = {
            "source_file": ml_path.relative_to(BASE_DIR).as_posix(),
            "is_anomaly": ml_record["is_anomaly"],
            "anomaly_score": ml_record["anomaly_score"],
            "basis": ml_record["basis"],
        }
    return {
        "run_id": run_id,
        "fused_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "evidence_status": {"rule": rule_status, "ml": ml_status},
        "rule_result": rule_result,
        "ml_result": ml_result,
        "final_conclusion": fusion["final_conclusion"],
        "trust_level": fusion["trust_level"],
        "degraded_reasons": fusion["degraded_reasons"],
        "basis": fusion["basis"],
    }


def save_final(record):
    """保存最终结果到 evidence/final/{run_id}_final.json(自动创建目录,已存在则覆盖,
    幂等重复融合);保存失败仅友好提示,程序不崩溃。"""
    try:
        FINAL_DIR.mkdir(parents=True, exist_ok=True)
        file_path = FINAL_DIR / f"{record['run_id']}_final.json"
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"⚠ 最终结果保存失败: {e}。")
        return False
    print(f"✓ 最终结果已保存: {file_path}")
    return True


def print_summary(record):
    """控制台输出最终融合评估结果:结论横幅 + 可信度星级 + 两路证据 + 降级原因。"""
    print("\n================ 最终融合评估结果 ================")
    print(f"最终结论: {record['final_conclusion']}")
    stars = {"高": "★★★", "中": "★★", "低": "★"}[record["trust_level"]]
    print(f"可信度: {record['trust_level']} {stars}")
    if record["trust_level"] == "低":
        print("⚠ 可信度已降级,降级原因见下。")
    print(f"run_id: {record['run_id']}")
    print(f"融合时间: {record['fused_time']}")
    print(f"证据完整性: 规则证据 {record['evidence_status']['rule']} | 机器学习证据 {record['evidence_status']['ml']}")
    print("----- 规则证据 ------")
    if record["rule_result"] is None:
        print("【证据缺失】无此证据,不硬补结果。")
    else:
        r = record["rule_result"]
        triggered = "无" if not r["triggered_rules"] else ", ".join(r["triggered_rules"])
        print(f"  结论: {r['conclusion']}   触发规则: {triggered}")
        print(f"  依据: {r['conclusion_basis']}")
    print("----- 机器学习证据 ------")
    if record["ml_result"] is None:
        print("【证据缺失】无此证据,不硬补结果。")
    else:
        m = record["ml_result"]
        print(f"  异常: {'是' if m['is_anomaly'] else '否'}   异常得分: {m['anomaly_score']:g}")
        print(f"  依据: {m['basis']}")
    print("----- 可信降级原因 ------")
    if record["degraded_reasons"]:
        for reason in record["degraded_reasons"]:
            print(f"  ⚠ {reason}")
    else:
        print("  (无)")
    print(f"融合依据: {record['basis']}")


def print_usage():
    """打印用法说明。"""
    print("用法: py b05_fusion.py [run_id]")
    print("  直接融合: py b05_fusion.py run_20261022_220400_485a96  (可带 .json / _rule.json / _ml.json 后缀)")
    print("  不带参数: 进入交互模式,列出可融合的数据供选择")


def fuse_run(run_id):
    """融合单个 run_id 的两路证据:证据缺失/损坏也是合法可保存的结果(可信降级);
    仅在保存失败时返回 False。"""
    rule_status, rule_record, rule_reason = load_rule_evidence(run_id)
    ml_status, ml_record, ml_reason = load_ml_evidence(run_id)
    fusion = fuse(rule_status, rule_record, ml_status, ml_record, rule_reason, ml_reason)
    record = build_final_record(run_id, rule_status, rule_record, ml_status, ml_record, fusion)
    print_summary(record)
    return save_final(record)


def main():
    """程序入口:带参数直接融合,不带参数交互选择;Ctrl+C/EOF 均友好退出。"""
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
            # 允许带 .json / _rule / _ml / _rule.json / _ml.json 后缀(大小写不敏感),依次剥掉
            arg = args[0]
            run_id = arg
            if run_id.lower().endswith(".json"):
                run_id = run_id[:-5]
            if run_id.lower().endswith("_rule"):
                run_id = run_id[:-5]
            if run_id.lower().endswith("_ml"):
                run_id = run_id[:-3]
            if not is_valid_run_id(run_id):
                print(f"run_id 格式不正确: {arg} (应为 run_YYYYmmdd_HHMMSS_<6位hex>)")
                return
            fuse_run(run_id)  # 零证据 run_id 也走融合(两者皆缺失 → 可信降级),仍保存结果
            return
        items = find_union_files()
        if not items:
            print("暂无证据文件(evidence/rule 与 evidence/ml 均为空),请先运行 B03 与 B04 生成证据。")
            return
        while True:
            run_id = interactive_pick_run_id(items)
            if run_id is None:
                return
            if fuse_run(run_id):
                return  # 成功保存一次后退出;保存失败则回到列表重新选择
    except KeyboardInterrupt:
        print("\n检测到 Ctrl+C,程序已安全退出。")
        sys.exit(0)
    except EOFError:
        print("\n输入已结束 (EOF),程序退出。")
        sys.exit(0)


if __name__ == "__main__":
    main()
