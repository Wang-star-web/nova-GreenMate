#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 GreenMate B02 — 环境解析与证据提取模块
=====================================================================

【任务目标】
  读取 B01 保存的原始数据 evidence/raw/{run_id}.json,对温度、湿度、
  CO₂浓度、光照四项指标逐项判定 偏低 / 适宜 / 偏高,并将每项的
  原始数值与判定依据写入 evidence/parsed/{run_id}_parsed.json 作为证据。
  B02 只做指标解析,不做最终综合结论(由后续模块实现)。

【运行方式】
  py b02_parse.py run_20261022_220400_485a96   # 直接解析指定 run_id(可带 .json 后缀)
  py b02_parse.py                              # 不带参数:列出可用 run_id 交互选择
  py b02_parse.py --help                       # 查看用法说明

【适宜区间(农学常规带,边界值含在适宜区间内)】
  温度      15 ~ 30 ℃
  湿度      45 ~ 85 %RH
  CO₂浓度   800 ~ 1500 ppm
  光照      10000 ~ 70000 lux
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
RAW_DIR = BASE_DIR / "evidence" / "raw"        # B01 原始数据目录
PARSED_DIR = BASE_DIR / "evidence" / "parsed"  # B02 解析结果目录(运行时自动创建)

# 各指标的适宜判定区间 [low, high](农学常规带,边界值含在适宜区间内)。
# 注意: 本模块不 import b01_cli(独立运行便于评分);key/unit 与 B01 的 PARAM_SPECS
# 保持一致,但 [low, high] 是农学适宜判定区间,与 B01 的合法采集范围(5~40 等)语义不同。
THRESHOLDS = [
    {"key": "temperature", "name": "温度",    "unit": "℃",   "low": 15.0,    "high": 30.0},
    {"key": "humidity",    "name": "湿度",    "unit": "%RH", "low": 45.0,    "high": 85.0},
    {"key": "co2",         "name": "CO₂浓度", "unit": "ppm", "low": 800.0,   "high": 1500.0},
    {"key": "light",       "name": "光照",    "unit": "lux", "low": 10000.0, "high": 70000.0},
]

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
    """校验 run_id 格式,防止拼接出非法文件路径(parsed 写路径由其拼出)。"""
    return bool(RUN_ID_RE.match(run_id))


def find_raw_files():
    """列出 evidence/raw 下的原始数据文件,按文件名倒序(时间戳定宽,字典序即时间序)。"""
    return sorted(RAW_DIR.glob("run_*.json"), reverse=True)


def print_file_list(files):
    """打印可解析的原始数据文件列表(编号 + 采集时间,按时间倒序)。"""
    print("\n---------------- 可解析的原始数据列表 ----------------")
    print(f"共 {len(files)} 条 (按时间倒序):")
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


def interactive_pick_run_id(files):
    """交互选择要解析的 run_id:输入序号或完整 run_id(可带 .json 后缀);
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
                return files[idx - 1].stem
        # 允许带 .json 后缀(大小写不敏感),统一去掉后缀再校验
        run_id = choice[:-5] if choice.lower().endswith(".json") else choice
        if is_valid_run_id(run_id) and (RAW_DIR / f"{run_id}.json").exists():
            return run_id
        print("未匹配到该序号或 run_id,请重试。")


def classify(spec, value):
    """按适宜区间 [low, high] 判定单项指标,返回 (判定结果, 判定依据)。"""
    low, high, unit = spec["low"], spec["high"], spec["unit"]
    if value < low:
        verdict = "偏低"
        basis = f"{value:g} {unit} 低于适宜下限 {low:g} {unit},判定为{verdict}"
    elif value > high:
        verdict = "偏高"
        basis = f"{value:g} {unit} 高于适宜上限 {high:g} {unit},判定为{verdict}"
    else:
        verdict = "适宜"
        basis = f"{value:g} {unit} 位于适宜区间 [{low:g}, {high:g}] {unit},判定为{verdict}"
    return verdict, basis


def load_raw(run_id):
    """读取并校验原始数据文件,失败打印具名中文提示并返回 None。"""
    path = RAW_DIR / f"{run_id}.json"
    if not path.exists():
        print(f"未找到原始数据文件: {path}")
        return None
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError:
        print(f"解析失败: {path.name} 文件内容不是合法 JSON。")
        return None
    except OSError as e:
        print(f"读取失败: {e}。")
        return None
    if not isinstance(raw, dict):
        print("文件结构不正确: 顶层应为 JSON 对象。")
        return None
    if "data" not in raw:
        print('文件缺少 "data" 字段。')
        return None
    data = raw["data"]
    if not isinstance(data, dict):
        print('"data" 字段应为对象。')
        return None
    for spec in THRESHOLDS:
        if spec["key"] not in data:
            print(f"缺少指标 \"{spec['key']}\" 的数据。")
            return None
        item = data[spec["key"]]
        if not isinstance(item, dict) or "value" not in item:
            print(f"指标 \"{spec['name']}\" 的数据结构不正确 (缺少 value)。")
            return None
        value = item["value"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            print(f"指标 \"{spec['name']}\" 的值不是有限数字 (内容: {value!r})。")
            return None
    if "collect_time" not in raw:
        print('文件缺少 "collect_time" 字段。')
        return None
    return raw


def build_parsed_record(run_id, raw):
    """对 4 项指标逐一判定并组装解析记录(假定 raw 已通过 load_raw 校验)。"""
    data = raw["data"]
    indicators = []
    for spec in THRESHOLDS:
        value = data[spec["key"]]["value"]
        verdict, basis = classify(spec, value)
        indicators.append({
            "key": spec["key"],
            "name": spec["name"],
            "unit": spec["unit"],
            "value": value,        # 原始数值,原样保留
            "verdict": verdict,
            "basis": basis,        # 判定依据
        })
    source_path = RAW_DIR / f"{run_id}.json"
    return {
        "run_id": run_id,
        "parsed_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_file": source_path.relative_to(BASE_DIR).as_posix(),
        "collect_time": raw["collect_time"],
        "rules": {  # 本次判定采用的阈值,证据文件自带判定规则
            spec["key"]: {"low": spec["low"], "high": spec["high"], "unit": spec["unit"]}
            for spec in THRESHOLDS
        },
        "indicators": indicators,
    }


def save_parsed(record):
    """保存解析结果到 evidence/parsed/{run_id}_parsed.json(自动创建目录,
    已存在则覆盖,幂等重解析);保存失败仅友好提示,程序不崩溃。"""
    try:
        PARSED_DIR.mkdir(parents=True, exist_ok=True)
        file_path = PARSED_DIR / f"{record['run_id']}_parsed.json"
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"⚠ 解析结果保存失败: {e}。")
        return False
    print(f"✓ 解析结果已保存: {file_path}")
    return True


def print_summary(record):
    """控制台输出逐项判定结果(仅逐项展示,不做任何综合结论)。"""
    print("\n---------------- 指标解析结果 ----------------")
    print(f"run_id: {record['run_id']}")
    print(f"采集时间: {record['collect_time']}")
    print(f"{'指标':<8}{'原始数值':<14}{'判定':<6}判定依据")
    for item in record["indicators"]:
        value_text = f"{item['value']:g} {item['unit']}"
        print(f"{item['name']:<8}{value_text:<14}{item['verdict']:<6}{item['basis']}")


def print_usage():
    """打印用法说明。"""
    print("用法: py b02_parse.py [run_id]")
    print("  直接解析: py b02_parse.py run_20261022_220400_485a96  (可带 .json 后缀)")
    print("  不带参数: 进入交互模式,列出可用 run_id 供选择")


def parse_run(run_id):
    """解析单个 run_id:读取→逐项判定→控制台展示→保存;返回是否成功。"""
    raw = load_raw(run_id)
    if raw is None:
        return False
    record = build_parsed_record(run_id, raw)
    print_summary(record)
    save_parsed(record)
    return True


def main():
    """程序入口:带参数直接解析,不带参数进入交互选择;Ctrl+C/EOF 均友好退出。"""
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
                print(f"run_id 格式不正确: {arg} (应为 run_YYYYmmdd_HHMMSS_<6位hex>)")
                return
            if not parse_run(run_id):
                files = find_raw_files()  # 失败时附带可用列表,便于修正
                if files:
                    print_file_list(files)
            return
        files = find_raw_files()
        if not files:
            print("暂无原始数据文件,请先运行 B01 采集。")
            return
        while True:
            run_id = interactive_pick_run_id(files)
            if run_id is None:
                return
            if parse_run(run_id):
                return  # 成功解析一次后退出;失败则回到列表重新选择
    except KeyboardInterrupt:
        print("\n检测到 Ctrl+C,程序已安全退出。")
        sys.exit(0)
    except EOFError:
        print("\n输入已结束 (EOF),程序退出。")
        sys.exit(0)


if __name__ == "__main__":
    main()
