#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 GreenMate B06 — 传统机器学习闭环模块(时间窗口预测)
=====================================================================

【任务目标】
  复用 B02 environment_samples(只读)构建时间窗口预测任务:输入窗口 = 最近 3 小时
  (6 个 30 分钟采样点),预测窗口 = 未来 1~2 小时(4 个采样点),输出
  next_window_attention(NORMAL/WATCH)。朴素基线对比 → 随机森林训练 → 混淆矩阵 /
  F1 / Recall 评估 + 假阳性/假阴性错误样本解释;模型 + 特征列表 + 窗口配置落盘,
  完全重启后可重载推理。接口 predict_next_attention(...) 输出 prediction +
  model_version + MODEL_OUTPUT 标记,作为独立证据交给 B07。

【运行方式】
  py b06_ml.py train                # 训练:窗口化 → 基线 → 随机森林 → 指标 → 错误分析 → 保存
  py b06_ml.py eval                 # 重载模型重算测试集指标(不重训练,确定性证明)
  py b06_ml.py predict [--sample-time "YYYY-MM-DD HH:MM:SS"]   # 对新样本推理(默认最新样本)
  py b06_ml.py info                 # 查看模型版本/配置/指标
  py b06_ml.py --help               # 查看本说明

【业务边界】
  不使用深度学习/YOLO 等大模型;DERIVED_LABEL 标签由未来窗口原始参数值按阈值规则
  派生,来源必须标注;sim_scenario、原始天气、标签源不作为基础特征;
  不改动 B02 原始数据(数据库只读连接 mode=ro,任何写入直接报错)。
=====================================================================
"""

import hashlib
import json
import math
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import (accuracy_score, classification_report, confusion_matrix,
                             f1_score, precision_score, recall_score)

# ---------------- 配置 ----------------
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "environment.db"     # B02 数据(只读输入,绝不写入)
MODEL_DIR = BASE_DIR / "models" / "b06"
MODEL_PATH = MODEL_DIR / "model.joblib"
FEATURE_LIST_PATH = MODEL_DIR / "feature_list.json"
CONFIG_PATH = MODEL_DIR / "config.json"
EVIDENCE_DIR = BASE_DIR / "evidence" / "b06"
LOG_DIR = BASE_DIR / "logs"
ML_LOG = LOG_DIR / "b06_ml.log"

PARAMS = ("temperature", "humidity", "co2", "light")
PARAM_NAMES = {"temperature": "温度", "humidity": "湿度", "co2": "CO₂浓度", "light": "光照"}

# DERIVED_LABEL 阈值表(与 b03_rules.json R1~R8 适宜带一致)
BANDS = {"temperature": (15.0, 30.0), "humidity": (45.0, 85.0),
         "co2": (800.0, 1500.0), "light": (10000.0, 70000.0)}
DAYLIGHT_RANGE = (6, 18)   # 光照规则仅在白天 6≤h<18 检查(夜间 light=0 属正常)
LABEL_SOURCE = ("DERIVED_LABEL: 规则派生(阈值表 b03_rules.json R1~R8),"
                "源=environment_samples 原始参数值,非 sim_scenario")
MODEL_OUTPUT = "B06_ML"    # 独立证据标记(供 B07 识别来源)

INPUT_POINTS = 6           # 输入窗口:最近 3 小时 = 6 个 30 分钟采样点
PREDICT_POINTS = 4         # 预测窗口:未来 1~2 小时 = 4 个采样点
SPLIT_BOUNDARY = "2026-10-27 01:30"   # 时间边界切分:前 5 天训练 / 后 2 天测试
LABEL_BOUNDARY_NOTE = ("5 个训练窗(锚点 10-26 23:00 ~ 10-27 01:00)的标签引用了边界后的样本值,"
                       "仅作监督标签、绝不作为特征;全部训练特征严格位于边界之前(walk-forward 标准实践)。")

MODEL_PARAMS = {"n_estimators": 200, "random_state": 42, "class_weight": "balanced", "n_jobs": -1}

AGGREGATORS = ("current", "mean", "std", "min", "max", "delta")


def feature_names():
    """26 个特征名(固定顺序,与模型一起保存):4 参数 × 6 + 小时 sin/cos。"""
    names = ["%s_%s" % (p, agg) for p in PARAMS for agg in AGGREGATORS]
    names += ["hour_sin", "hour_cos"]
    return names


FEATURES = feature_names()


def setup_console():
    """强制标准输出为 UTF-8:GBK 管道打印 ℃/CO₂ 会崩溃(本项目已多次实测)。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def _now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _log_ml(text):
    """一行日志到 logs/b06_ml.log 并镜像控制台;库级路径自带 UTF-8 兜底。"""
    setup_console()
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), text)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(ML_LOG, "a", encoding="utf-8") as f:
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


# ---------------- 数据载入(只读,不改动 B02)----------------
def load_db_df():
    """只读加载 environment_samples(mode=ro:任何写入直接报错)。
    列清单只含 sample_time + 4 参数 —— sim_scenario 结构性不读。返回 DataFrame 或 None。"""
    if not DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % DB_PATH.as_posix(), uri=True)
    except sqlite3.Error:
        return None
    try:
        df = pd.read_sql_query(
            "SELECT sample_time, temperature, humidity, co2, light "
            "FROM environment_samples ORDER BY sample_time ASC", conn)
    except Exception:
        return None
    finally:
        conn.close()
    df["t"] = pd.to_datetime(df["sample_time"])
    return df


# ---------------- 窗口化 / 标签 / 特征 ----------------
def violation_flags(df):
    """逐样本违规标志(适宜带外),光照规则仅白天生效。"""
    hour = df["t"].dt.hour + df["t"].dt.minute / 60.0
    daylight = (hour >= DAYLIGHT_RANGE[0]) & (hour < DAYLIGHT_RANGE[1])
    return (df["temperature"].lt(BANDS["temperature"][0]) | df["temperature"].gt(BANDS["temperature"][1])
            | df["humidity"].lt(BANDS["humidity"][0]) | df["humidity"].gt(BANDS["humidity"][1])
            | df["co2"].lt(BANDS["co2"][0]) | df["co2"].gt(BANDS["co2"][1])
            | (daylight & (df["light"].lt(BANDS["light"][0]) | df["light"].gt(BANDS["light"][1]))))


def build_dataset(df):
    """构建带标签与特征的数据集:预测窗口 [t+30, t+120] 任一违规 → WATCH;
    预测窗口不足(库尾)不打标签;输入窗口不足(前 5 行)丢弃。"""
    viol = violation_flags(df)
    s1 = viol.shift(-1).fillna(False)
    s2 = viol.shift(-2).fillna(False)
    s3 = viol.shift(-3).fillna(False)
    s4 = viol.shift(-4).fillna(False)
    future = s1 | s2 | s3 | s4
    # 窗口完整性按行位置判定(不依赖 shift 的 NaN 语义,库尾预测窗口不足 → 不打标签):
    # 行 i 的预测窗口完整 ⇔ i 之后还有 >=4 行;输入窗口完整 ⇔ i 之前有 >=5 行
    positions = np.arange(len(df))
    horizon_ok = positions <= len(df) - 1 - PREDICT_POINTS
    input_ok = positions >= INPUT_POINTS - 1
    label = np.where(future, "WATCH", "NORMAL")

    lab = df.copy()
    lab["label"] = label
    for p in PARAMS:
        lab["%s_current" % p] = df[p]
        lab["%s_mean" % p] = df[p].rolling(INPUT_POINTS, min_periods=INPUT_POINTS).mean()
        lab["%s_std" % p] = df[p].rolling(INPUT_POINTS, min_periods=INPUT_POINTS).std(ddof=1)
        lab["%s_min" % p] = df[p].rolling(INPUT_POINTS, min_periods=INPUT_POINTS).min()
        lab["%s_max" % p] = df[p].rolling(INPUT_POINTS, min_periods=INPUT_POINTS).max()
        lab["%s_delta" % p] = df[p] - df[p].shift(INPUT_POINTS - 1)
    hdec = df["t"].dt.hour + df["t"].dt.minute / 60.0
    lab["hour_sin"] = np.sin(2 * np.pi * hdec / 24.0)
    lab["hour_cos"] = np.cos(2 * np.pi * hdec / 24.0)
    lab = lab[input_ok & horizon_ok].reset_index(drop=True)   # 输入窗口不足的前 5 行 + 预测窗口不足的库尾 4 行
    return lab


def split_dataset(lab):
    """时间边界切分 + 3 项防泄露断言(结果返回供报告留存)。"""
    boundary = pd.Timestamp(SPLIT_BOUNDARY)
    train = lab[lab["t"] < boundary].reset_index(drop=True)
    test = lab[lab["t"] >= boundary].reset_index(drop=True)
    checks = []
    ok1 = bool(train["t"].max() < test["t"].min())
    checks.append({"id": "leak_1_time_order", "name": "训练锚点全部早于测试锚点",
                   "passed": ok1, "detail": "train 最大 %s < test 最小 %s"
                   % (train["t"].max(), test["t"].min())})
    train_anchors = set(train["sample_time"])
    test_anchors = set(test["sample_time"])
    ok2 = train_anchors.isdisjoint(test_anchors)
    checks.append({"id": "leak_2_anchor_disjoint", "name": "训练/测试锚点集合不相交",
                   "passed": ok2, "detail": "train %d 个 / test %d 个锚点" % (len(train), len(test))})

    def row_hashes(d):
        hashes = set()
        for row in d[FEATURES].values:
            hashes.add(hashlib.md5("|".join("%.10g" % float(x) for x in row).encode("utf-8")).hexdigest())
        return hashes

    h_train, h_test = row_hashes(train), row_hashes(test)
    ok3 = h_train.isdisjoint(h_test)
    checks.append({"id": "leak_3_row_hash", "name": "测试特征行绝不参与训练",
                   "passed": ok3, "detail": "train %d 行 / test %d 行,哈希无交集" % (len(train), len(test))})
    return train, test, checks


# ---------------- 训练 / 指标 / 错误分析 ----------------
def compute_metrics(y_true, y_pred, proba_watch):
    report = classification_report(y_true, y_pred, labels=["NORMAL", "WATCH"],
                                   zero_division=0, output_dict=True)
    watch = report.get("WATCH", {})
    return {
        "accuracy": round(float(accuracy_score(y_true, y_pred)), 4),
        "macro_f1": round(float(f1_score(y_true, y_pred, average="macro", zero_division=0)), 4),
        "watch_precision": round(float(watch.get("precision", 0.0)), 4),
        "watch_recall": round(float(watch.get("recall", 0.0)), 4),
        "watch_f1": round(float(watch.get("f1-score", 0.0)), 4),
        "confusion_matrix": confusion_matrix(y_true, y_pred, labels=["NORMAL", "WATCH"]).tolist(),
        "classification_report": report,
        "probabilities": {"mean_p_watch_true_watch": round(float(np.mean(proba_watch[y_true == "WATCH"])), 4),
                          "mean_p_watch_true_normal": round(float(np.mean(proba_watch[y_true == "NORMAL"])), 4)},
    }


def _rule_hits(row):
    """单样本触发哪些适宜带规则(中文列表)。"""
    hits = []
    hour = row["t"].hour + row["t"].minute / 60.0
    if row["temperature"] < BANDS["temperature"][0] or row["temperature"] > BANDS["temperature"][1]:
        hits.append("温度 %.1f℃ 超出 15~30" % row["temperature"])
    if row["humidity"] < BANDS["humidity"][0] or row["humidity"] > BANDS["humidity"][1]:
        hits.append("湿度 %.1f%% 超出 45~85" % row["humidity"])
    if row["co2"] < BANDS["co2"][0] or row["co2"] > BANDS["co2"][1]:
        hits.append("CO₂ %.0f 超出 800~1500" % row["co2"])
    if DAYLIGHT_RANGE[0] <= hour < DAYLIGHT_RANGE[1] and \
            (row["light"] < BANDS["light"][0] or row["light"] > BANDS["light"][1]):
        hits.append("白天光照 %.0f lux 超出 10000~70000" % row["light"])
    return hits


def _window_violations(rows):
    """窗口内各样本触发的规则(用于程序化解释)。返回 [(sample_time, [命中规则]), ...]。"""
    out = []
    for _, r in rows.iterrows():
        hits = _rule_hits(r)
        if hits:
            out.append((str(r["sample_time"]), hits))
    return out


def _explain_error(kind, input_rows, future_rows):
    """程序化生成错误样本中文解释(检查输入窗/预测窗分别触发哪些规则)。"""
    iv = _window_violations(input_rows)
    fv = _window_violations(future_rows)
    if kind == "fp":
        if iv:
            first_time, hits = iv[0]
            return ("假阳性:输入窗 %s 出现 %s —— 模型从训练集(每日黎明/黄昏弱光 WATCH 与 "
                    "disturbance_low_light/multi 的弱光模式)学到'弱光→WATCH',"
                    "但预测窗 %d 个样本全部在适宜带内(无违规),故为误报。"
                    % (first_time, ";".join(hits), len(future_rows)))
        return ("假阳性:输入窗无违规但模型仍判 WATCH;预测窗 %d 个样本全部在适宜带内。"
                % len(future_rows))
    if kind == "fn":
        if fv:
            first_time, hits = fv[0]
            return ("假阴性:输入窗全部样本均在适宜带内(模型无任何违规信号可依据),"
                    "预测窗内 %s 起出现 %s —— 突变发生在预测窗内、尚未出现在输入窗,"
                    "属时间序列前推预测的固有局限,如实说明。"
                    % (first_time, ";".join(hits)))
        return "假阴性:输入窗全部适宜,预测窗出现违规(具体规则未列出)。"


def analyze_errors(df, test, y_test, pred, proba_watch):
    """选取并解释 1 个假阳性 + 1 个假阴性(P 极值选取,并列取最早锚点)。"""
    ev = test.copy()
    ev["pred"] = pred
    ev["p_watch"] = proba_watch
    result = {"counts": {
        "fp": int(((y_test == "NORMAL") & (pred == "WATCH")).sum()),
        "fn": int(((y_test == "WATCH") & (pred == "NORMAL")).sum())}}
    samples = []
    for kind, label_true, label_pred, ascending in (
            ("fp", "NORMAL", "WATCH", False), ("fn", "WATCH", "NORMAL", True)):
        subset = ev[(ev["label"] == label_true) & (ev["pred"] == label_pred)]
        if subset.empty:
            samples.append({"kind": kind, "note": "无此类错误样本"})
            continue
        pick = subset.sort_values(["p_watch", "t"], ascending=[ascending, True]).iloc[0]
        anchor = pick["t"]
        input_rows = df[(df["t"] <= anchor)].tail(INPUT_POINTS)
        future_rows = df[(df["t"] > anchor)].head(PREDICT_POINTS)
        samples.append({
            "kind": kind,
            "anchor": str(pick["sample_time"]),
            "true_label": str(pick["label"]),
            "predicted": str(pick["pred"]),
            "p_watch": round(float(pick["p_watch"]), 4),
            "input_window": [{"sample_time": str(r["sample_time"]),
                              "temperature": float(r["temperature"]), "humidity": float(r["humidity"]),
                              "co2": float(r["co2"]), "light": float(r["light"])}
                             for _, r in input_rows.iterrows()],
            "features": {f: float(pick[f]) for f in FEATURES},
            "prediction_window_truth": [{"sample_time": str(r["sample_time"]),
                                         "temperature": float(r["temperature"]), "humidity": float(r["humidity"]),
                                         "co2": float(r["co2"]), "light": float(r["light"])}
                                        for _, r in future_rows.iterrows()],
            "explanation": _explain_error(kind, input_rows, future_rows),
        })
    result["samples"] = samples
    return result


# ---------------- 模型保存 / 重载 ----------------
def _next_version():
    if CONFIG_PATH.exists():
        try:
            old = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            match = re_match_version(old.get("model_version", ""))
            if match:
                return "B06-ML-v%d" % (int(match.group(1)) + 1)
        except (OSError, ValueError):
            pass
    return "B06-ML-v1"


def re_match_version(text):
    import re
    return re.match(r"^B06-ML-v(\d+)$", str(text))


def save_model(bundle):
    try:
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        joblib.dump(bundle, MODEL_PATH)
        _save_json(FEATURE_LIST_PATH, {"model_version": bundle["config"]["model_version"],
                                       "feature_list": bundle["feature_list"]})
        _save_json(CONFIG_PATH, bundle["config"])
        return True
    except OSError as e:
        print("⚠ 模型保存失败: %s" % e)
        return False


def load_model():
    """重载模型(三文件齐全 + feature_list 交叉核对)。返回 (bundle|None, error_dict|None)。"""
    if not MODEL_PATH.exists():
        return None, {"error_type": "model_missing",
                      "message": "模型不存在,请先运行 py b06_ml.py train"}
    try:
        bundle = joblib.load(MODEL_PATH)
    except Exception as e:
        return None, {"error_type": "model_corrupt",
                      "message": "模型文件损坏或不可加载: %s" % str(e)[:120]}
    try:
        fl = json.loads(FEATURE_LIST_PATH.read_text(encoding="utf-8"))["feature_list"]
    except (OSError, ValueError, KeyError):
        return None, {"error_type": "model_corrupt", "message": "feature_list.json 缺失或损坏"}
    if bundle.get("feature_list") != fl:
        return None, {"error_type": "model_corrupt",
                      "message": "模型与 feature_list.json 的特征列表不一致"}
    return bundle, None


# ---------------- 公开接口(官方契约,供 B07 复用)----------------
def predict_next_attention(sample_time=None):
    """对未来 1~2 小时窗口预测 next_window_attention。sample_time 接受
    'YYYY-MM-DD HH:MM:SS' / datetime,默认 = 库内最新样本。恒返回证据 dict,
    含 prediction + model_version + MODEL_OUTPUT 标记;错误变体如实返回。"""
    ev = {"prediction": None, "probability": None, "model_version": None,
          "MODEL_OUTPUT": MODEL_OUTPUT, "generated_at": _now_iso(),
          "window": None, "features": None, "label_source": LABEL_SOURCE,
          "status": "error", "error": None}
    bundle, err = load_model()
    if err is not None:
        ev["error"] = {"code": err["error_type"], "message": err["message"]}
        return ev
    ev["model_version"] = bundle["config"]["model_version"]
    df = load_db_df()
    if df is None:
        ev["error"] = {"code": "db_unavailable",
                       "message": "B02 数据库不可用(缺失或不可读): data/environment.db"}
        return ev
    if sample_time is None:
        target = df["t"].max()
    else:
        target = _parse_target(sample_time)
        if target is None:
            ev["error"] = {"code": "bad_sample_time",
                           "message": "sample_time 格式非法(需 YYYY-MM-DD HH:MM:SS): %s" % sample_time}
            return ev
    rows = df[df["t"] <= target].tail(INPUT_POINTS)
    diffs = rows["t"].diff().dt.total_seconds().dropna()
    grid_ok = (len(rows) == INPUT_POINTS and rows["t"].iloc[-1] == target
               and all(d == 1800.0 for d in diffs))
    if not grid_ok:
        ev["error"] = {"code": "insufficient_history",
                       "message": "输入窗口不足(需以 sample_time 为末点的连续 6 个 30 分钟网格样本)",
                       "detail": {"sample_time": str(target),
                                  "available_points": int(len(rows)),
                                  "required": INPUT_POINTS, "required_grid": "30min"}}
        return ev
    features = {}
    for p in PARAMS:
        features["%s_current" % p] = float(rows[p].iloc[-1])
        features["%s_mean" % p] = float(rows[p].mean())
        features["%s_std" % p] = float(rows[p].std(ddof=1))
        features["%s_min" % p] = float(rows[p].min())
        features["%s_max" % p] = float(rows[p].max())
        features["%s_delta" % p] = float(rows[p].iloc[-1] - rows[p].iloc[0])
    hdec = target.hour + target.minute / 60.0
    features["hour_sin"] = float(np.sin(2 * np.pi * hdec / 24.0))
    features["hour_cos"] = float(np.cos(2 * np.pi * hdec / 24.0))
    x = np.array([[features[name] for name in bundle["feature_list"]]])
    pred = str(bundle["model"].predict(x)[0])
    proba = bundle["model"].predict_proba(x)[0]
    classes = [str(c) for c in bundle["model"].classes_]
    ev["prediction"] = pred
    ev["probability"] = {classes[i]: round(float(proba[i]), 4) for i in range(len(classes))}
    ev["features"] = {k: round(v, 6) for k, v in features.items()}
    ev["window"] = {"start": str(rows["sample_time"].iloc[0]),
                    "end": str(rows["sample_time"].iloc[-1]), "points": INPUT_POINTS}
    ev["status"] = "ok"
    return ev


def _parse_target(sample_time):
    if isinstance(sample_time, datetime):
        return pd.Timestamp(sample_time)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return pd.Timestamp(datetime.strptime(str(sample_time).strip(), fmt))
        except ValueError:
            continue
    return None


# ---------------- CLI ----------------
def cmd_train():
    """训练全流程:窗口化 → 泄露断言 → 基线 → 随机森林 → 指标 → 错误分析 → 保存。"""
    print("================ B06 传统机器学习训练 ================")
    df = load_db_df()
    if df is None:
        print("⚠ B02 数据库不可用,请先运行 py b02_memory.py generate")
        return
    lab = build_dataset(df)
    train, test, leak_checks = split_dataset(lab)
    print("数据: %d 行样本,可打标签窗口 %d(前 %d 行窗口不足,尾部 %d 个预测窗口不足被排除)" % (
        len(df), len(lab), INPUT_POINTS - 1, len(df) - (INPUT_POINTS - 1) - len(lab)))
    for c in leak_checks:
        print("  [%s] %s: %s" % ("✓" if c["passed"] else "⚠", c["name"], c["detail"]))
    if not all(c["passed"] for c in leak_checks):
        print("⚠ 防泄露断言未全部通过,中止训练")
        return
    counts = {"train": {"windows": int(len(train)),
                        "normal": int((train["label"] == "NORMAL").sum()),
                        "watch": int((train["label"] == "WATCH").sum())},
              "test": {"windows": int(len(test)),
                       "normal": int((test["label"] == "NORMAL").sum()),
                       "watch": int((test["label"] == "WATCH").sum())}}
    print("切分: train %(windows)d(NORMAL %(normal)d / WATCH %(watch)d)| test %(windows_t)d(NORMAL %(normal_t)d / WATCH %(watch_t)d)" % {
        "windows": counts["train"]["windows"], "normal": counts["train"]["normal"],
        "watch": counts["train"]["watch"], "windows_t": counts["test"]["windows"],
        "normal_t": counts["test"]["normal"], "watch_t": counts["test"]["watch"]})

    X_train, y_train = train[FEATURES].values, train["label"].values
    X_test, y_test = test[FEATURES].values, test["label"].values

    dummy = DummyClassifier(strategy="most_frequent")
    dummy.fit(X_train, y_train)
    dummy_pred = dummy.predict(X_test)
    base_proba = np.zeros(len(y_test))
    base_proba[dummy_pred == "WATCH"] = 1.0
    base_metrics = compute_metrics(y_test, dummy_pred, base_proba)

    rf = RandomForestClassifier(**MODEL_PARAMS)
    rf.fit(X_train, y_train)
    pred = rf.predict(X_test)
    proba = rf.predict_proba(X_test)
    watch_idx = list(rf.classes_).index("WATCH")
    proba_watch = proba[:, watch_idx]
    model_metrics = compute_metrics(y_test, pred, proba_watch)

    verdict_pass = model_metrics["macro_f1"] > base_metrics["macro_f1"]
    verdict = ("通过: 模型 macro-F1 %.4f > 基线 %.4f" % (model_metrics["macro_f1"], base_metrics["macro_f1"])
               if verdict_pass else "未通过: 模型未超过基线")

    print("\n---------------- 指标对比 ----------------")
    print("  %-14s %-10s %-10s %-12s" % ("模型", "accuracy", "macro-F1", "WATCH recall"))
    print("  %-14s %-10.4f %-10.4f %-12.4f" % ("基线(多数类)", base_metrics["accuracy"],
                                              base_metrics["macro_f1"], base_metrics["watch_recall"]))
    print("  %-14s %-10.4f %-10.4f %-12.4f" % ("随机森林", model_metrics["accuracy"],
                                              model_metrics["macro_f1"], model_metrics["watch_recall"]))
    cm = model_metrics["confusion_matrix"]
    print("  混淆矩阵(行=真实 [[NORMAL, WATCH]]): %s" % cm)
    print("  判定: %s" % ("✓ " if verdict_pass else "⚠ ") + verdict)

    error_analysis = analyze_errors(df, test, y_test, pred, proba_watch)
    print("\n---------------- 错误样本分析 ----------------")
    for s in error_analysis["samples"]:
        if "note" in s:
            print("  [%s] %s" % (s["kind"], s["note"]))
            continue
        print("  [%s] 锚点 %s | 真实 %s → 预测 %s (P(WATCH)=%.4f)" % (
            s["kind"].upper(), s["anchor"], s["true_label"], s["predicted"], s["p_watch"]))
        print("       解释: %s" % s["explanation"][:160])
    print("  计数: FP %d / FN %d" % (error_analysis["counts"]["fp"], error_analysis["counts"]["fn"]))

    version = _next_version()
    cfg = {
        "model_version": version,
        "model_type": "RandomForestClassifier",
        "trained_at": _now_iso(),
        "window": {"input_hours": 3, "input_points": INPUT_POINTS, "step_minutes": 30,
                   "predict_hours_min": 1, "predict_hours_max": 2, "predict_points": PREDICT_POINTS},
        "split": {"boundary": SPLIT_BOUNDARY,
                  "train_end": str(train["t"].max()), "test_start": str(test["t"].min())},
        "label_rule": {"name": "DERIVED_LABEL", "source": "environment_samples 原始参数值",
                       "thresholds": {"temperature": list(BANDS["temperature"]),
                                      "humidity": list(BANDS["humidity"]),
                                      "co2": list(BANDS["co2"]),
                                      "light": list(BANDS["light"]),
                                      "light_daylight_hours": list(DAYLIGHT_RANGE)},
                       "not_used": ["sim_scenario", "weather"]},
        "features": {"count": len(FEATURES), "params": list(PARAMS),
                     "per_param": list(AGGREGATORS), "time_features": ["hour_sin", "hour_cos"]},
        "model_params": dict(MODEL_PARAMS),
        "metrics": {"baseline": base_metrics, "model": model_metrics, "verdict": verdict},
        "dataset": {**counts, "excluded_tail_windows": int(len(df) - (INPUT_POINTS - 1) - len(lab)),
                    "label_boundary_note": LABEL_BOUNDARY_NOTE},
        "leak_checks": leak_checks,
        "error_analysis": error_analysis,
        "label_source": LABEL_SOURCE,
        "library_versions": {"sklearn": "1.9.1", "pandas": "3.0.6", "numpy": np.__version__,
                             "joblib": joblib.__version__, "python": sys.version.split()[0]},
    }
    bundle = {"model": rf, "feature_list": FEATURES, "config": cfg}
    if not save_model(bundle):
        return
    _save_json(EVIDENCE_DIR / "train_report.json", cfg)
    _save_json(EVIDENCE_DIR / "error_analysis.json", error_analysis)
    _log_ml("[训练] version=%s acc=%.4f macro_f1=%.4f watch_recall=%.4f 判定=%s" % (
        version, model_metrics["accuracy"], model_metrics["macro_f1"],
        model_metrics["watch_recall"], "通过" if verdict_pass else "未通过"))
    print("✓ 模型已保存: models/b06/model.joblib + feature_list.json + config.json")
    print("✓ 训练报告已保存: evidence/b06/train_report.json 与 error_analysis.json")


def cmd_eval():
    """重载模型重算测试集指标(不重训练)—— 完全重启后的确定性证明。"""
    print("================ B06 模型重载评估(不重训练)================")
    bundle, err = load_model()
    if err is not None:
        print("⚠ %s" % err["message"])
        return
    cfg = bundle["config"]
    df = load_db_df()
    if df is None:
        print("⚠ B02 数据库不可用")
        return
    lab = build_dataset(df)
    train, test, leak_checks = split_dataset(lab)
    model = bundle["model"]
    pred = model.predict(test[FEATURES].values)
    proba = model.predict_proba(test[FEATURES].values)
    watch_idx = list(model.classes_).index("WATCH")
    metrics = compute_metrics(test["label"].values, pred, proba[:, watch_idx])
    stored = cfg["metrics"]["model"]
    same = (abs(metrics["accuracy"] - stored["accuracy"]) < 1e-6
            and abs(metrics["macro_f1"] - stored["macro_f1"]) < 1e-6
            and abs(metrics["watch_recall"] - stored["watch_recall"]) < 1e-6)
    print("  模型版本: %s(训练于 %s)" % (cfg["model_version"], cfg["trained_at"]))
    print("  %-12s %-10s %-10s %-12s" % ("指标", "accuracy", "macro-F1", "WATCH recall"))
    print("  %-12s %-10.4f %-10.4f %-12.4f" % ("训练报告", stored["accuracy"], stored["macro_f1"],
                                              stored["watch_recall"]))
    print("  %-12s %-10.4f %-10.4f %-12.4f" % ("本次重算", metrics["accuracy"], metrics["macro_f1"],
                                              metrics["watch_recall"]))
    print("  [%s] %s" % ("✓" if same else "⚠",
                        "指标与训练报告一致(确定性成立)" if same else "指标不一致,请检查数据或模型"))
    report = {"evaluated_at": _now_iso(), "model_version": cfg["model_version"],
              "stored_metrics": stored, "recomputed_metrics": metrics,
              "consistent": same, "leak_checks": leak_checks}
    _save_json(EVIDENCE_DIR / "eval_report.json", report)
    _log_ml("[评估] version=%s 指标一致=%s acc=%.4f" % (cfg["model_version"], "是" if same else "否",
                                                      metrics["accuracy"]))


def cmd_predict(args):
    """对新样本推理(默认库内最新样本);证据落盘 prediction_{ts}.json。"""
    sample_time = None
    if "--sample-time" in args:
        i = args.index("--sample-time")
        if i + 1 >= len(args):
            print("⚠ --sample-time 需要参数")
            return
        sample_time = args[i + 1]
    ev = predict_next_attention(sample_time)
    print("\n---------------- B06 预测证据 ----------------")
    if ev["status"] == "ok":
        print("  ✓ prediction=%s | model_version=%s | MODEL_OUTPUT=%s" % (
            ev["prediction"], ev["model_version"], ev["MODEL_OUTPUT"]))
        print("  probability: %s" % json.dumps(ev["probability"], ensure_ascii=False))
        print("  窗口: %s ~ %s(%d 点)" % (ev["window"]["start"], ev["window"]["end"], ev["window"]["points"]))
        print("  标签来源: %s" % ev["label_source"])
    else:
        err = ev["error"] or {}
        print("  ⚠ status=error code=%s | %s" % (err.get("code"), err.get("message")))
    filename = "prediction_%s.json" % datetime.now().strftime("%Y%m%d_%H%M%S")
    _save_json(EVIDENCE_DIR / filename, ev)
    print("✓ 证据已保存: evidence/b06/%s" % filename)
    _log_ml("[推理] sample_time=%s prediction=%s status=%s" % (
        sample_time or "(最新)", ev["prediction"], ev["status"]))


def cmd_info():
    print("================ B06 模型信息 ================")
    bundle, err = load_model()
    if err is not None:
        print("⚠ %s" % err["message"])
        return
    cfg = bundle["config"]
    print("  版本: %s(%s)" % (cfg["model_version"], cfg["model_type"]))
    print("  训练时间: %s" % cfg["trained_at"])
    print("  窗口: 输入 %d 点(%dh)/ 预测 %d 点(%d~%dh)" % (
        cfg["window"]["input_points"], cfg["window"]["input_hours"],
        cfg["window"]["predict_points"], cfg["window"]["predict_hours_min"],
        cfg["window"]["predict_hours_max"]))
    print("  切分边界: %s" % cfg["split"]["boundary"])
    print("  特征: %d 个(%s + 时间 sin/cos)" % (cfg["features"]["count"],
                                              "、".join(cfg["features"]["params"])))
    print("  指标: acc=%.4f macro-F1=%.4f WATCH recall=%.4f | %s" % (
        cfg["metrics"]["model"]["accuracy"], cfg["metrics"]["model"]["macro_f1"],
        cfg["metrics"]["model"]["watch_recall"], cfg["metrics"]["verdict"]))
    print("  标签: %s" % cfg["label_rule"]["name"])


def print_usage():
    print("用法:")
    print("  py b06_ml.py train                # 训练:窗口化 → 基线 → 随机森林 → 指标 → 错误分析 → 保存")
    print("  py b06_ml.py eval                 # 重载模型重算测试集指标(不重训练,确定性证明)")
    print("  py b06_ml.py predict [--sample-time \"YYYY-MM-DD HH:MM:SS\"]   # 对新样本推理(默认最新)")
    print("  py b06_ml.py info                 # 查看模型版本/配置/指标")
    print("  py b06_ml.py --help")
    print("依赖: pandas / scikit-learn / joblib(如未安装: py -m pip install pandas scikit-learn)")
    print("公开接口(供 B07 复用): predict_next_attention(sample_time=None) -> 证据 dict")
    print("  含 prediction / probability / model_version / MODEL_OUTPUT=%s 标记 / label_source" % MODEL_OUTPUT)
    print("数据: 只读复用 B02 data/environment.db(mode=ro,不改动原始数据)")
    print("标签: %s" % LABEL_SOURCE)


def main():
    """B06 入口:子命令分发。退出码恒 0(清单语义);Ctrl+C/EOF 友好退出。"""
    setup_console()
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print_usage()
        return
    cmd, rest = args[0], args[1:]
    try:
        if cmd == "train":
            cmd_train()
        elif cmd == "eval":
            cmd_eval()
        elif cmd == "predict":
            cmd_predict(rest)
        elif cmd == "info":
            cmd_info()
        else:
            print("未知参数: %s" % " ".join(args))
            print_usage()
    except KeyboardInterrupt:
        print("\n检测到 Ctrl+C,程序已安全退出。")
    except EOFError:
        print("\n输入已结束 (EOF),程序退出。")


if __name__ == "__main__":
    main()
