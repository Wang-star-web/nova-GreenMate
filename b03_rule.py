#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 GreenMate B03 — 规则判断模块(专家规则推理)
=====================================================================

【任务目标】
  根据 run_id 读取 B02 的解析结果 evidence/parsed/{run_id}_parsed.json,
  加载番茄大棚专家判定规则库 b03_rules.json,综合判断大棚环境
  「正常 / 需要预警」,并完整记录本次判断用到的规则(每条规则的匹配情况),
  写入 evidence/rule/{run_id}_rule.json 作为证据。
  B03 只做规则推理,不调用机器学习。

【运行方式】
  py b03_rule.py run_20261022_220400_485a96   # 直接判定指定 run_id
  py b03_rule.py                              # 不带参数:列出可用解析数据交互选择
  py b03_rule.py --help                       # 查看用法说明

【判定逻辑】
  规则库 b03_rules.json 含 8 条原子规则(4 项指标 × 偏低/偏高),
  任一规则触发 → 需要预警;全部未触发 → 正常。
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
PARSED_DIR = BASE_DIR / "evidence" / "parsed"  # B02 解析结果目录(本模块的输入)
RULE_DIR = BASE_DIR / "evidence" / "rule"      # B03 判定结果目录(运行时自动创建)
RULES_FILE = BASE_DIR / "b03_rules.json"       # 专家规则知识库(与推理引擎分离)

# 本模块不 import b02_parse(独立运行便于评分),只读取其输出的 evidence/parsed 文件;
# 指标 key 与 B01/B02 保持一致。
KNOWN_KEYS = ("temperature", "humidity", "co2", "light")
PARSED_VERDICTS = ("偏低", "适宜", "偏高")
RULE_VERDICTS = ("偏低", "偏高")               # 规则只针对偏离适宜区间的两种情况

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
    """校验 run_id 格式,防止拼接出非法文件路径(rule 写路径由其拼出)。"""
    return bool(RUN_ID_RE.match(run_id))


def find_parsed_files():
    """列出 evidence/parsed 下可判定的解析文件,返回 (文件列表(倒序), 格式不符数量)。"""
    files, skipped = [], 0
    for path in sorted(PARSED_DIR.glob("run_*_parsed.json"), reverse=True):
        if path.stem.endswith("_parsed") and is_valid_run_id(path.stem[:-7]):
            files.append(path)
        else:
            skipped += 1
    return files, skipped


def print_file_list(files, skipped=0):
    """打印可判定的解析数据文件列表(编号 + 采集时间,按时间倒序)。"""
    print("\n---------------- 可判定的解析数据列表 ----------------")
    print(f"共 {len(files)} 条 (按时间倒序):")
    for i, path in enumerate(files, start=1):
        time_text = datetime.strptime(path.stem[4:19], "%Y%m%d_%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
        print(f"  {i}. {path.stem}  ({time_text})")
    if skipped:
        print(f"(另有 {skipped} 个文件名格式不符的文件被忽略)")


def interactive_pick_run_id(files, skipped=0):
    """交互选择要判定的 run_id:输入序号或完整 run_id(可带 .json / _parsed.json
    后缀);返回 run_id;输入 q / Ctrl+C / EOF 时返回 None。"""
    while True:
        print_file_list(files, skipped)
        try:
            choice = input("请选择: 输入序号或完整 run_id (可带 .json 或 _parsed.json),输入 q 退出: ").strip()
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
                return files[idx - 1].stem[:-7]  # 去掉 "_parsed" 后缀
        # 允许带 .json / _parsed / _parsed.json 后缀(大小写不敏感),统一剥掉再校验
        run_id = choice
        if run_id.lower().endswith(".json"):
            run_id = run_id[:-5]
        if run_id.lower().endswith("_parsed"):
            run_id = run_id[:-7]
        if is_valid_run_id(run_id) and (PARSED_DIR / f"{run_id}_parsed.json").exists():
            return run_id
        print("未匹配到该序号或 run_id,请重试。")


def load_rules():
    """加载并校验专家规则库 b03_rules.json,返回规则列表;失败打印提示并返回 None。"""
    if not RULES_FILE.exists():
        print(f"未找到规则库文件 {RULES_FILE.name},无法进行判定。")
        return None
    try:
        with open(RULES_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        print(f"规则库文件内容不是合法 JSON: {RULES_FILE.name}。")
        return None
    except OSError as e:
        print(f"读取规则库失败: {e}。")
        return None
    if not isinstance(data, dict) or "rules" not in data:
        print('规则库文件结构不正确,缺少 "rules" 字段。')
        return None
    rules = data["rules"]
    if not isinstance(rules, list) or not rules:
        print('"rules" 字段应为非空数组。')
        return None
    seen_ids = set()
    for i, rule in enumerate(rules, start=1):
        if not isinstance(rule, dict):
            print(f"第 {i} 条规则应为对象。")
            return None
        for field in ("id", "name", "indicator", "verdict"):
            if field not in rule:
                print(f"第 {i} 条规则缺少字段 \"{field}\"。")
                return None
        if rule["id"] in seen_ids:
            print(f"规则 id 重复: \"{rule['id']}\"。")
            return None
        seen_ids.add(rule["id"])
        if rule["indicator"] not in KNOWN_KEYS:
            print(f"规则 \"{rule['id']}\" 的 indicator 无效: \"{rule['indicator']}\"。")
            return None
        if rule["verdict"] not in RULE_VERDICTS:
            print(f"规则 \"{rule['id']}\" 的 verdict 无效: \"{rule['verdict']}\" (仅支持 \"偏低\"/\"偏高\")。")
            return None
    return rules


def load_parsed(run_id):
    """读取并校验 B02 的解析结果文件,失败打印具名中文提示并返回 None。"""
    path = PARSED_DIR / f"{run_id}_parsed.json"
    if not path.exists():
        print(f"未找到解析文件: {path} (请先运行 B02 解析)。")
        return None
    try:
        with open(path, encoding="utf-8") as f:
            parsed = json.load(f)
    except json.JSONDecodeError:
        print(f"解析失败: {path.name} 文件内容不是合法 JSON。")
        return None
    except OSError as e:
        print(f"读取失败: {e}。")
        return None
    if not isinstance(parsed, dict):
        print("文件结构不正确: 顶层应为 JSON 对象。")
        return None
    for field in ("indicators", "run_id", "collect_time"):
        if field not in parsed:
            print(f"文件缺少 \"{field}\" 字段。")
            return None
    indicators = parsed["indicators"]
    if not isinstance(indicators, list):
        print('"indicators" 字段应为数组。')
        return None
    if len(indicators) != 4:
        print(f'"indicators" 应为 4 项,实际 {len(indicators)} 项。')
        return None
    seen_keys = set()
    for i, item in enumerate(indicators, start=1):
        if not isinstance(item, dict):
            print(f"第 {i} 项指标应为对象。")
            return None
        if "key" not in item or item["key"] not in KNOWN_KEYS:
            print(f"第 {i} 项指标缺少或含无效 \"key\" (内容: {item.get('key')!r})。")
            return None
        if item["key"] in seen_keys:
            print(f"指标 key 重复: \"{item['key']}\"。")
            return None
        seen_keys.add(item["key"])
        value = item.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            print(f"指标 \"{item.get('name', item['key'])}\" 的值不是有限数字 (内容: {value!r})。")
            return None
        verdict = item.get("verdict")
        if verdict not in PARSED_VERDICTS:
            print(f"指标 \"{item.get('name', item['key'])}\" 的判定值无效: \"{verdict}\" (应为 偏低/适宜/偏高)。")
            return None
    return parsed


def evaluate_rules(rules, indicators):
    """逐条评估规则:指标判定与规则要求的判定一致即触发。
    纯函数(无 I/O),返回 (带匹配结果的规则列表, 触发的规则 id 列表)。"""
    by_key = {item["key"]: item for item in indicators}
    rule_results = []
    triggered_ids = []
    for rule in rules:
        item = by_key[rule["indicator"]]
        matched = item["verdict"] == rule["verdict"]
        rule_results.append({
            "id": rule["id"],
            "name": rule["name"],
            "indicator": rule["indicator"],
            "verdict": rule["verdict"],
            "matched": matched,
            "value": item["value"],
            "unit": item["unit"],
            "description": rule.get("description", ""),
            "advice": rule.get("advice", ""),
        })
        if matched:
            triggered_ids.append(rule["id"])
    return rule_results, triggered_ids


def build_rule_record(run_id, parsed, rules, rule_results, triggered_ids):
    """组装规则判定记录(证据文件内容)。"""
    by_key = {item["key"]: item for item in parsed["indicators"]}
    if triggered_ids:
        conclusion = "需要预警"
        parts, advices = [], []
        for r in rule_results:
            if r["matched"]:
                item = by_key[r["indicator"]]
                parts.append(f"{r['id']} {r['name']} ({item['name']} {r['value']:g} {r['unit']} {item['verdict']})")
                if r["advice"]:
                    advices.append(r["advice"])
        basis = "触发规则: " + "、".join(parts) + "。"
        if advices:
            basis += "建议: " + "；".join(advices)
    else:
        conclusion = "正常"
        basis = f"{len(rules)} 条规则均未触发,各项指标均在适宜区间。"
    source_path = PARSED_DIR / f"{run_id}_parsed.json"
    return {
        "run_id": run_id,
        "judged_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_file": source_path.relative_to(BASE_DIR).as_posix(),
        "collect_time": parsed["collect_time"],
        "rule_source": RULES_FILE.name,
        "rules_loaded": len(rules),
        "rule_results": rule_results,
        "triggered_rules": triggered_ids,
        "conclusion": conclusion,
        "conclusion_basis": basis,
    }


def save_rule(record):
    """保存判定结果到 evidence/rule/{run_id}_rule.json(自动创建目录,
    已存在则覆盖,幂等重复判定);保存失败仅友好提示,程序不崩溃。"""
    try:
        RULE_DIR.mkdir(parents=True, exist_ok=True)
        file_path = RULE_DIR / f"{record['run_id']}_rule.json"
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"⚠ 判定结果保存失败: {e}。")
        return False
    print(f"✓ 判定结果已保存: {file_path}")
    return True


def print_summary(record):
    """控制台输出规则判定结果:结论横幅 + 逐条规则匹配情况。"""
    print("\n---------------- 规则判定结果 ----------------")
    if record["conclusion"] == "需要预警":
        print("结论: 需要预警 ⚠")
    else:
        print("结论: 正常 ✓")
    print(f"run_id: {record['run_id']}")
    print(f"采集时间: {record['collect_time']}")
    print(f"{'规则':<16}{'实测值':<14}{'指标判定':<8}触发")
    for r in record["rule_results"]:
        label = f"{r['id']} {r['name']}"
        value_text = f"{r['value']:g} {r['unit']}"
        matched_text = "触发" if r["matched"] else "—"
        print(f"{label:<16}{value_text:<14}{r['verdict']:<8}{matched_text}")
    print(f"判定依据: {record['conclusion_basis']}")


def print_usage():
    """打印用法说明。"""
    print("用法: py b03_rule.py [run_id]")
    print("  直接判定: py b03_rule.py run_20261022_220400_485a96  (可带 .json / _parsed.json 后缀)")
    print("  不带参数: 进入交互模式,列出可判定的解析数据供选择")


def judge_run(run_id, rules):
    """判定单个 run_id:读取解析结果→评估规则→控制台展示→保存;返回是否成功。"""
    parsed = load_parsed(run_id)
    if parsed is None:
        return False
    rule_results, triggered_ids = evaluate_rules(rules, parsed["indicators"])
    record = build_rule_record(run_id, parsed, rules, rule_results, triggered_ids)
    print_summary(record)
    save_rule(record)
    return True


def main():
    """程序入口:加载规则库后,带参数直接判定,不带参数交互选择;Ctrl+C/EOF 友好退出。"""
    setup_console()
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        print_usage()
        return
    if len(args) > 1:
        print("参数无效,最多接受 1 个 run_id。")
        print_usage()
        return
    rules = load_rules()  # 先加载规则库,缺失/损坏时无法判定
    if rules is None:
        return
    try:
        if args:
            # 允许带 .json / _parsed / _parsed.json 后缀(大小写不敏感),统一剥掉再校验
            arg = args[0]
            run_id = arg
            if run_id.lower().endswith(".json"):
                run_id = run_id[:-5]
            if run_id.lower().endswith("_parsed"):
                run_id = run_id[:-7]
            if not is_valid_run_id(run_id):
                print(f"run_id 格式不正确: {arg} (应为 run_YYYYmmdd_HHMMSS_<6位hex>)")
                return
            if not judge_run(run_id, rules):
                files, skipped = find_parsed_files()  # 失败时附带可用列表,便于修正
                if files:
                    print_file_list(files, skipped)
            return
        files, skipped = find_parsed_files()
        if not files:
            print("暂无解析数据文件,请先运行 B02 解析。")
            return
        while True:
            run_id = interactive_pick_run_id(files, skipped)
            if run_id is None:
                return
            if judge_run(run_id, rules):
                return  # 成功判定一次后退出;失败则回到列表重新选择
    except KeyboardInterrupt:
        print("\n检测到 Ctrl+C,程序已安全退出。")
        sys.exit(0)
    except EOFError:
        print("\n输入已结束 (EOF),程序退出。")
        sys.exit(0)


if __name__ == "__main__":
    main()
