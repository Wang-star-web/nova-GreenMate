#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 GreenMate B04 — 机器学习异常检测模块(孤立森林)
=====================================================================

【任务目标】
  基于孤立森林(IsolationForest)实现环境异常检测:
  读取 evidence/raw 下的历史环境数据训练模型(训练集排除待预测的目标
  run_id,random_state 固定保证结果可复现),再读取指定 run_id 的原始
  环境数据进行预测,输出「是否异常 + 异常得分」,结果保存到
  evidence/ml/{run_id}_ml.json 作为证据。
  本模块独立运行,直接读取 B01 原始数据,不依赖 B02/B03。

【运行方式】
  py b04_ml.py run_20261022_220400_485a96   # 直接预测指定 run_id
  py b04_ml.py                              # 不带参数:列出可用原始数据交互选择
  py b04_ml.py --help                       # 查看用法说明

【模型参数(固定,保证结果可复现)】
  IsolationForest(n_estimators=100, contamination=0.05, random_state=42)
  异常得分 = -decision_function,数值越大越异常,阈值为 0;
  是否异常直接取 predict() 结果(-1 表示异常)。
=====================================================================
"""

import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path

# 机器学习依赖缺失时模块仍可运行并给出友好提示(独立运行、不崩溃)
try:
    import numpy as np
    from sklearn.ensemble import IsolationForest
except ImportError:
    np = None
    IsolationForest = None

# ---------------- 配置 ----------------
BASE_DIR = Path(__file__).resolve().parent     # 脚本所在目录,数据路径不受启动位置影响
RAW_DIR = BASE_DIR / "evidence" / "raw"        # B01 原始数据目录(训练与预测的数据源)
ML_DIR = BASE_DIR / "evidence" / "ml"          # B04 预测结果目录(运行时自动创建)

# 本模块不 import b01/b02/b03(独立运行);特征 key 与 B01 的 PARAM_SPECS 保持一致。
FEATURE_KEYS = ("temperature", "humidity", "co2", "light")  # 特征顺序固定
FEATURE_NAMES = {"temperature": "温度", "humidity": "湿度", "co2": "CO₂浓度", "light": "光照"}
FEATURE_UNITS = {"temperature": "℃", "humidity": "%RH", "co2": "ppm", "light": "lux"}

# 模型参数固定:random_state=42 保证每次训练与预测结果完全可复现
MODEL_PARAMS = {"n_estimators": 100, "contamination": 0.05, "random_state": 42, "max_samples": "auto"}

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
    """校验 run_id 格式,防止拼接出非法文件路径(ml 写路径由其拼出)。"""
    return bool(RUN_ID_RE.match(run_id))


def find_raw_files():
    """列出 evidence/raw 下的原始数据文件,按文件名倒序(时间戳定宽,字典序即时间序)。"""
    return sorted(RAW_DIR.glob("run_*.json"), reverse=True)


def print_file_list(files):
    """打印可预测的原始数据文件列表(编号 + 采集时间,按时间倒序)。"""
    print("\n---------------- 可预测的原始数据列表 ----------------")
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
    """交互选择要预测的 run_id:输入序号或完整 run_id(可带 .json 后缀);
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


def read_raw_file(path):
    """读取并校验单个原始数据文件,返回 (记录, "") 或 (None, 中文失败原因)。
    目标文件与历史文件共用此校验,保证两处口径一致。"""
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError:
        return None, "文件内容不是合法 JSON。"
    except OSError as e:
        return None, f"读取失败: {e}。"
    if not isinstance(raw, dict):
        return None, "文件结构不正确: 顶层应为 JSON 对象。"
    if "data" not in raw:
        return None, '文件缺少 "data" 字段。'
    data = raw["data"]
    if not isinstance(data, dict):
        return None, '"data" 字段应为对象。'
    for key in FEATURE_KEYS:
        if key not in data:
            return None, f"缺少指标 \"{key}\" 的数据。"
        item = data[key]
        if not isinstance(item, dict) or "value" not in item:
            return None, f"指标 \"{FEATURE_NAMES[key]}\" 的数据结构不正确 (缺少 value)。"
        value = item["value"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return None, f"指标 \"{FEATURE_NAMES[key]}\" 的值不是有限数字 (内容: {value!r})。"
    if "collect_time" not in raw:
        return None, '文件缺少 "collect_time" 字段。'
    return raw, ""


def load_raw_sample(run_id):
    """读取并校验目标 run_id 的原始数据,失败打印具名中文提示并返回 None。"""
    path = RAW_DIR / f"{run_id}.json"
    if not path.exists():
        print(f"未找到原始数据文件: {path}")
        return None
    raw, reason = read_raw_file(path)
    if raw is None:
        print(f"目标文件校验失败: {path.name} — {reason}")
        return None
    return raw


def load_history(target_run_id):
    """读取历史训练数据:遍历 evidence/raw,排除目标 run_id 自身;
    坏文件跳过并记录(不致命),返回 (特征矩阵, 训练文件相对路径列表, 跳过文件名列表)。"""
    x_train, training_files, skipped_names = [], [], []
    for path in find_raw_files():
        if path.stem == target_run_id:
            continue  # 训练集排除待预测的目标样本
        raw, reason = read_raw_file(path)
        if raw is None:
            print(f"⚠ 跳过历史文件 {path.name}: {reason}")
            skipped_names.append(path.name)
            continue
        x_train.append([float(raw["data"][key]["value"]) for key in FEATURE_KEYS])
        training_files.append(path.relative_to(BASE_DIR).as_posix())
    return x_train, training_files, skipped_names


def train_model(x_train):
    """用历史特征矩阵训练孤立森林模型(random_state 固定,结果可复现)。"""
    return IsolationForest(**MODEL_PARAMS).fit(np.array(x_train, dtype=float))


def predict_sample(model, features):
    """对单个样本预测,返回 (是否异常, 异常得分)。

    异常得分 = -decision_function,数值越大越异常,阈值为 0;
    是否异常直接取 predict() 结果(-1 表示异常)。
    """
    pred = model.predict([features])[0]
    score = -float(model.decision_function([features])[0])
    if score == 0.0:
        score = 0.0  # 归一 -0.0,避免证据文件出现 -0.0
    # 注意: pred 是 np.int64,直接比较产生 np.bool_,JSON 无法序列化,必须转 Python bool
    return bool(pred == -1), score


def build_ml_record(run_id, collect_time, features, training_files, skipped_names, is_anomaly, anomaly_score):
    """组装模型预测记录(证据文件内容)。"""
    score_text = f"{anomaly_score:g}"
    if is_anomaly:
        basis = f"孤立森林判定: 异常得分 {score_text} > 0,判定为异常(偏离历史分布)。"
    else:
        basis = f"孤立森林判定: 异常得分 {score_text} ≤ 0,判定为正常(符合历史分布)。"
    target_path = RAW_DIR / f"{run_id}.json"
    return {
        "run_id": run_id,
        "predicted_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "model": "IsolationForest",
        "model_params": dict(MODEL_PARAMS),
        "training_files": training_files,
        "training_size": len(training_files),
        "skipped_files": skipped_names,
        "target_file": target_path.relative_to(BASE_DIR).as_posix(),
        "collect_time": collect_time,
        "features": {key: features[i] for i, key in enumerate(FEATURE_KEYS)},
        "is_anomaly": is_anomaly,
        "anomaly_score": round(anomaly_score, 6),
        "threshold": 0.0,
        "basis": basis,
    }


def save_ml(record):
    """保存预测结果到 evidence/ml/{run_id}_ml.json(自动创建目录,已存在则覆盖,
    幂等重复预测);保存失败仅友好提示,程序不崩溃。"""
    try:
        ML_DIR.mkdir(parents=True, exist_ok=True)
        file_path = ML_DIR / f"{record['run_id']}_ml.json"
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"⚠ 预测结果保存失败: {e}。")
        return False
    print(f"✓ 预测结果已保存: {file_path}")
    return True


def print_summary(record):
    """控制台输出预测结果与模型/训练集信息。"""
    print("\n---------------- 孤立森林异常检测结果 ----------------")
    if record["is_anomaly"]:
        print("结论: 异常 ⚠")
    else:
        print("结论: 正常 ✓")
    print(f"run_id: {record['run_id']}")
    print(f"采集时间: {record['collect_time']}")
    feature_text = " | ".join(
        f"{FEATURE_NAMES[key]} {record['features'][key]:g} {FEATURE_UNITS[key]}" for key in FEATURE_KEYS
    )
    print(f"特征: {feature_text}")
    print(f"异常得分: {record['anomaly_score']:g} (阈值 {record['threshold']:g},越大越异常)")
    print(f"判定依据: {record['basis']}")
    print("---------------- 模型与训练集 ----------------")
    params = ", ".join(f"{k}={v}" for k, v in record["model_params"].items())
    print(f"模型: {record['model']} | {params}")
    print(f"训练集: {record['training_size']} 份历史文件")
    for name in record["training_files"]:
        print(f"  - {name}")
    if record["skipped_files"]:
        print(f"⚠ 已跳过 {len(record['skipped_files'])} 个格式异常的历史文件: {', '.join(record['skipped_files'])}")


def print_usage():
    """打印用法说明。"""
    print("用法: py b04_ml.py [run_id]")
    print("  直接预测: py b04_ml.py run_20261022_220400_485a96  (可带 .json 后缀)")
    print("  不带参数: 进入交互模式,列出可用原始数据供选择")


def predict_run(run_id):
    """预测单个 run_id:读目标→读历史训练→训练→预测→展示→保存;返回是否成功。"""
    raw = load_raw_sample(run_id)
    if raw is None:
        return False
    features = [float(raw["data"][key]["value"]) for key in FEATURE_KEYS]
    x_train, training_files, skipped_names = load_history(run_id)
    if len(x_train) < 2:
        print(f"历史数据不足: 排除目标 run_id 后仅 {len(x_train)} 份有效原始数据 (至少需要 2 份)。"
              f"请先运行 B01 采集更多历史数据。")
        return False
    model = train_model(x_train)
    is_anomaly, score = predict_sample(model, features)
    record = build_ml_record(run_id, raw["collect_time"], features, training_files, skipped_names, is_anomaly, score)
    print_summary(record)
    save_ml(record)
    return True


def main():
    """程序入口:带参数直接预测,不带参数交互选择;Ctrl+C/EOF 均友好退出。"""
    setup_console()
    if IsolationForest is None:
        print("未安装 scikit-learn,无法进行机器学习异常检测。请先运行: py -m pip install scikit-learn")
        return
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
            if not predict_run(run_id):
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
            if predict_run(run_id):
                return  # 成功预测一次后退出;失败则回到列表重新选择
    except KeyboardInterrupt:
        print("\n检测到 Ctrl+C,程序已安全退出。")
        sys.exit(0)
    except EOFError:
        print("\n输入已结束 (EOF),程序退出。")
        sys.exit(0)


if __name__ == "__main__":
    main()
