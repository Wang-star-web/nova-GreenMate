#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 GreenMate B02 — 数据记忆模块(SQLite 时序持久化 + 查询统计 + 可视化)
=====================================================================

【任务目标】
  将番茄大棚 4 项环境参数(温度、湿度、CO₂浓度、光照)的时序样本持久化到 SQLite
  数据库,程序重启后历史数据保留,支持按时间窗口查询、统计与趋势分析。
  数据由内置模拟器生成:固定 30 分钟一个采样点,连续 7 天共 336 个时序点,
  包含正常基线片段与 5 段扰动片段(满足 4~6 段要求),并叠加少量合理噪声。
  sim_scenario 仅为模拟器内部元数据,不能当作标签、不能作为基础特征。

【运行方式】
  py b02_memory.py generate          # 生成 7 天模拟数据(幂等:已有数据不覆盖)
  py b02_memory.py generate --force  # 强制重建(沿用旧锚点,数据逐字节一致)
  py b02_memory.py current           # 查看当前环境状态(库内最新一条样本)
  py b02_memory.py history --hours N # 查看最近 N 小时历史时序数据(默认 24)
  py b02_memory.py stats [--hours N] # 统计指定窗口(默认全窗口,并落盘样本统计)
  py b02_memory.py check             # 质量校验(空值/重复/异常值/连续性/扰动分布)
  py b02_memory.py web [--port N]    # 启动 Web 时间序列可视化页面(Chart.js)
  py b02_memory.py --help            # 查看本说明

【数据规则】
  · 30 分钟固定采样网格,7 天 336 点(跨度 167.5 小时,覆盖 7 个日历日)
  · 基线:昼夜周期公式(全部落在 B03 规则适宜带内部)+ 少量均匀噪声
  · 扰动:5 段(4~6 段要求内),各占一个完整日历日,时长 2~4 小时,
    配方对齐 B03 规则 R1 高温 / R3 高湿 / R6 CO₂偏高 / R7 弱光 / R1+R3+R7 组合
  · 扰动片段均匀分布:相邻段起始间隔 ≤40 小时,首末段起始跨度 ≥96 小时
  · 生成确定性:固定种子 42,幂等写入(INSERT OR IGNORE),重启不重生成

【公开接口(供 B06、B07 复用)】
  get_current_environment()      -> dict|None  当前环境状态(不含 sim_scenario)
  get_environment_history(hours) -> list[dict] 最近 N 小时时序数据(升序)

【边界约束】
  B02 不实现机器学习、RAG、真实传感器;仅做持久化存储与时间序列查询。
  本模块不 import 兄弟模块,独立运行便于评分。
=====================================================================
"""

import hashlib
import html
import json
import math
import os
import random
import re
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ---------------- 配置 ----------------
BASE_DIR = Path(__file__).resolve().parent        # 脚本所在目录,数据路径不受启动位置影响
DB_PATH = BASE_DIR / "data" / "environment.db"    # SQLite 数据库文件(B02 交付物)
ASSETS_DIR = BASE_DIR / "b02_assets"              # 本地静态资源(Chart.js 本地文件,离线可用)
CHART_JS_PATH = ASSETS_DIR / "chart.umd.js"       # Chart.js 4.4.9 UMD(npm 官方包,内容已压缩)
EVIDENCE_DIR = BASE_DIR / "evidence" / "b02"      # B02 证据产物目录(运行时自动创建)

# 4 项环境参数的规格:一次定义,驱动生成、校验、展示(本模块独立副本,不 import 兄弟模块)
PARAM_KEYS = ("temperature", "humidity", "co2", "light")
PARAM_SPECS = [
    {"key": "temperature", "name": "温度",    "unit": "℃",   "min": 5.0,     "max": 40.0},
    {"key": "humidity",    "name": "湿度",    "unit": "%RH", "min": 30.0,    "max": 90.0},
    {"key": "co2",         "name": "CO₂浓度", "unit": "ppm", "min": 300.0,   "max": 2000.0},
    {"key": "light",       "name": "光照",    "unit": "lux", "min": 0.0,     "max": 100000.0},
]
PARAM_SPEC_MAP = {spec["key"]: spec for spec in PARAM_SPECS}

# ---------------- 模拟器常量 ----------------
SEED = 42                        # 固定随机种子:生成结果确定、可复现
GRID_STEP_MINUTES = 30           # 固定 30 分钟一个采样点
TOTAL_POINTS = 336               # 连续 7 天共 336 个时序点
GENERATOR_VERSION = "1.0"
SCHEMA_VERSION = "1"
SIM_SOURCE_TYPE = "SIMULATED"    # 与 B01 的 source_type 约定一致

# 扰动片段计划:5 段(4~6 段要求内)。day_rank = 网格中第几个完整日历日(从 1 起);
# start_hour = 当日起始时刻;duration_hours = 时长(2~4 小时);targets 仅作用于受影响参数。
# 配方对齐 B03 规则: R1 高温(>30℃)/ R3 高湿(>85%RH)/ R6 CO₂偏高(>1500ppm)/ R7 弱光(<10000lux)。
DISTURBANCE_PLAN = [
    {"scenario": "disturbance_high_temp",     "day_rank": 1, "start_hour": 10.0, "duration_hours": 3.0,
     "targets": {"temperature": 33.0}, "rules": "R1 高温"},
    {"scenario": "disturbance_high_humidity", "day_rank": 2, "start_hour": 14.0, "duration_hours": 2.5,
     "targets": {"humidity": 88.0}, "rules": "R3 高湿"},
    {"scenario": "disturbance_high_co2",      "day_rank": 3, "start_hour": 8.5,  "duration_hours": 3.0,
     "targets": {"co2": 1600.0}, "rules": "R6 CO₂偏高"},
    {"scenario": "disturbance_low_light",     "day_rank": 4, "start_hour": 11.0, "duration_hours": 2.0,
     "targets": {"light": 8000.0}, "rules": "R7 弱光"},
    {"scenario": "disturbance_multi",         "day_rank": 5, "start_hour": 13.0, "duration_hours": 4.0,
     "targets": {"temperature": 33.0, "humidity": 88.0, "light": 8000.0}, "rules": "R1+R3+R7"},
]

# 均匀分布判定阈值(生成自校验 + 质量检查 C9 共同使用)
DIST_MAX_GAP_HOURS = 40.0        # 相邻扰动段起始时刻最大间隔
DIST_MIN_SPAN_HOURS = 96.0       # 首末扰动段起始时刻最小跨度
DIST_DURATION_RANGE = (2.0, 4.0)  # 单段时长合法区间

# 噪声幅度(均匀分布,叠加于基线/目标值之上)
NOISE_RANGES = {"temperature": 0.4, "humidity": 1.5, "co2": 15.0, "light": 300.0}

# Web 服务配置(环境变量可覆盖)
B02_WEB_HOST = "127.0.0.1"       # 只绑本机回环,不触发防火墙弹窗
try:
    B02_WEB_PORT = int(os.environ.get("B02_WEB_PORT", "8223"))
except ValueError:
    B02_WEB_PORT = 8223

# 建表 SQL(sim_scenario 注释在 docs/b02_schema.md 中详述)
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS environment_samples (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_time  TEXT NOT NULL UNIQUE,          -- 采样时间 'YYYY-MM-DD HH:MM:SS'(本地时间,固定 30 分钟网格)
    temperature  REAL NOT NULL,                 -- 温度 ℃,合法 5~40
    humidity     REAL NOT NULL,                 -- 湿度 %RH,合法 30~90
    co2          REAL NOT NULL,                 -- CO₂浓度 ppm,合法 300~2000
    light        REAL NOT NULL,                 -- 光照 lux,合法 0~100000
    source_type  TEXT NOT NULL DEFAULT 'SIMULATED',  -- 数据来源标记(与 B01 约定一致)
    sim_scenario TEXT NOT NULL,                 -- 模拟器场景元数据:仅生成追溯用,严禁作为标签/特征
    created_at   TEXT NOT NULL                  -- 行写入时间,ISO8601 带时区
);
CREATE TABLE IF NOT EXISTS b02_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# 公开 API 的显式列清单:结构性隔离 sim_scenario,防止下游误用(见【公开接口】)
PUBLIC_COLUMNS = "sample_time, source_type, temperature, humidity, co2, light"


def setup_console():
    """强制标准输出为 UTF-8:输出被管道/重定向时避免 Windows 默认 GBK 导致乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass  # 环境不支持时保持默认,不影响程序运行


# ---------------- 数据库基础设施 ----------------
def connect_db():
    """打开一个独立连接(每次调用新建,线程安全:Web 服务每请求各用各的连接)。"""
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn):
    """建表(IF NOT EXISTS,幂等)。"""
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def db_min_time():
    """库内最早采样时间;库/表不存在或空库返回 None。"""
    if not DB_PATH.exists():
        return None
    conn = connect_db()
    try:
        row = conn.execute("SELECT MIN(sample_time) AS t FROM environment_samples").fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return row["t"] if row and row["t"] is not None else None


def db_max_time():
    """库内最新采样时间;库/表不存在或空库返回 None。"""
    if not DB_PATH.exists():
        return None
    conn = connect_db()
    try:
        row = conn.execute("SELECT MAX(sample_time) AS t FROM environment_samples").fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return row["t"] if row and row["t"] is not None else None


# ---------------- 时间网格 ----------------
def compute_anchor(now=None):
    """最近一个 30 分钟边界(本地时间):7 天窗口的右端点。"""
    now = now or datetime.now()
    minute = 30 if now.minute >= 30 else 0
    return now.replace(minute=minute, second=0, microsecond=0)


def build_grid(anchor):
    """由锚点向前生成 336 个网格点(升序):anchor 本身是最后一点。"""
    step = timedelta(minutes=GRID_STEP_MINUTES)
    return [anchor - step * (TOTAL_POINTS - 1 - i) for i in range(TOTAL_POINTS)]


def fmt_dt(dt):
    """datetime → 'YYYY-MM-DD HH:MM:SS'(库内统一格式)。"""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def parse_dt(text):
    """严格解析 'YYYY-MM-DD HH:MM:SS';失败抛 ValueError。"""
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")


def parse_dt_web(text):
    """Web 端时间参数解析:兼容 datetime-local 的 'YYYY-MM-DDTHH:MM[:SS]' 与空格分隔;失败抛 ValueError。"""
    value = text.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError("时间格式非法: %s" % text)


# ---------------- 模拟器:基线 / 扰动 / 生成 ----------------
def baseline_value(key, h):
    """基线昼夜公式,h = 小时 + 分钟/60 ∈ [0,24)。全部落在 B03 规则适宜带内部:
    温度 15~30、湿度 45~85、CO₂ 800~1500、光照 10000~70000(白天)。"""
    if key == "temperature":
        return 23.0 + 5.5 * math.cos(2 * math.pi * (h - 14) / 24)
    if key == "humidity":
        return 62.0 - 9.5 * math.cos(2 * math.pi * (h - 14) / 24)  # 与温度反相
    if key == "co2":
        return 1150.0 - 250.0 * math.cos(2 * math.pi * (h - 14) / 24)  # 白天光合作用低谷
    if key == "light":
        if 6 <= h <= 18:
            return 60000.0 * math.sin(math.pi * (h - 6) / 12) ** 1.5  # 白天光照曲线,正午峰值
        return 0.0
    raise ValueError("未知参数: %s" % key)


def clamp_round(key, value):
    """钳制合法范围 → 舍入(温度/湿度 1 位小数,CO₂/光照取整)→ 再钳制(舍入可能推出边界)。"""
    spec = PARAM_SPEC_MAP[key]
    value = max(spec["min"], min(spec["max"], value))
    if key in ("temperature", "humidity"):
        value = round(value, 1)
    else:
        value = round(value)
    return max(spec["min"], min(spec["max"], value))


def full_days_in_grid(grid):
    """返回网格内拥有全部 48 个采样点的日历日(升序)。7 天网格恒有 ≥6 个完整日历日。"""
    counts = {}
    for dt in grid:
        counts[dt.date()] = counts.get(dt.date(), 0) + 1
    return sorted(d for d, c in counts.items() if c == 48)


def build_disturbance_segments(grid):
    """按 DISTURBANCE_PLAN 在网格内定位 5 段扰动,返回段列表;自校验失败返回 None。
    校验项:完整日历日足够、每段点数 == 时长*2+1、段落在网格内且不重叠、
    相邻段起始间隔 ≤40h、首末段起始跨度 ≥96h(均匀分布规则)。"""
    full = full_days_in_grid(grid)
    if len(full) < len(DISTURBANCE_PLAN):
        print("⚠ 完整日历日不足 %d 个,无法按计划安放扰动片段" % len(DISTURBANCE_PLAN))
        return None
    segments = []
    for item in DISTURBANCE_PLAN:
        day = full[item["day_rank"] - 1]
        start = datetime.combine(day, datetime.min.time()) + timedelta(hours=item["start_hour"])
        end = start + timedelta(hours=item["duration_hours"])
        points = [dt for dt in grid if start <= dt <= end]
        segments.append({
            "scenario": item["scenario"],
            "start": start,
            "end": end,
            "points": points,
            "targets": item["targets"],
            "rules": item["rules"],
            "duration_hours": item["duration_hours"],
        })
    # 自校验:点数、网格内、互不重叠、时长区间
    for seg in segments:
        if len(seg["points"]) != int(seg["duration_hours"] * 2) + 1:
            print("⚠ 扰动段 %s 点数与时长不符(%d 点)" % (seg["scenario"], len(seg["points"])))
            return None
        if not (DIST_DURATION_RANGE[0] <= seg["duration_hours"] <= DIST_DURATION_RANGE[1]):
            print("⚠ 扰动段 %s 时长 %.1fh 超出 %.1f~%.1fh" %
                  (seg["scenario"], seg["duration_hours"], *DIST_DURATION_RANGE))
            return None
    # 均匀分布校验(与质量检查 C9 同规则)
    starts = [seg["start"] for seg in segments]
    if len(set(s.date() for s in starts)) != len(starts):
        print("⚠ 扰动段未分布在互不相同的日历日")
        return None
    gaps = [(starts[i + 1] - starts[i]).total_seconds() / 3600.0 for i in range(len(starts) - 1)]
    span = (starts[-1] - starts[0]).total_seconds() / 3600.0
    if any(g > DIST_MAX_GAP_HOURS for g in gaps) or span < DIST_MIN_SPAN_HOURS:
        print("⚠ 扰动段分布不均:间隔 %s h,首末跨度 %.1fh" % (gaps, span))
        return None
    return segments


def generate_points(grid, segments):
    """逐网格点计算样本值:基线 + 噪声;扰动段内受影响参数取目标值 + 噪声。
    返回 (rows, manifest):rows 为 (sample_time, temp, hum, co2, light, sim_scenario) 元组列表。"""
    rng = random.Random(SEED)  # 固定种子:遍历顺序固定,结果确定
    seg_by_time = {}
    for seg in segments:
        for dt in seg["points"]:
            seg_by_time[dt] = seg
    rows = []
    for dt in grid:
        h = dt.hour + dt.minute / 60.0
        values = {}
        for key in PARAM_KEYS:
            values[key] = clamp_round(key, baseline_value(key, h)
                                      + rng.uniform(-NOISE_RANGES[key], NOISE_RANGES[key]))
        seg = seg_by_time.get(dt)
        if seg is not None:
            for key, target in seg["targets"].items():
                values[key] = clamp_round(key, target + rng.uniform(-NOISE_RANGES[key], NOISE_RANGES[key]))
            scenario = seg["scenario"]
        else:
            scenario = "baseline"
        rows.append((fmt_dt(dt), values["temperature"], values["humidity"],
                     values["co2"], values["light"], scenario))
    return rows


def write_meta(conn, anchor, segments, mode):
    """写入 b02_meta 生成器配置(可追溯:规则、种子、锚点、噪声、扰动清单)。"""
    grid = build_grid(anchor)
    manifest = {
        "generator_version": GENERATOR_VERSION,
        "seed": str(SEED),
        "anchor": fmt_dt(anchor),
        "grid": {"start": fmt_dt(grid[0]), "end": fmt_dt(grid[-1]),
                 "step_minutes": GRID_STEP_MINUTES, "points": TOTAL_POINTS},
        "disturbances": [
            {"scenario": seg["scenario"], "start": fmt_dt(seg["start"]), "end": fmt_dt(seg["end"]),
             "duration_hours": seg["duration_hours"], "points": len(seg["points"]),
             "affected_parameters": sorted(seg["targets"].keys()), "targets": seg["targets"],
             "rules": seg["rules"]}
            for seg in segments
        ],
    }
    meta = {
        "schema_version": SCHEMA_VERSION,
        "generator_version": GENERATOR_VERSION,
        "seed": str(SEED),
        "anchor": fmt_dt(anchor),
        "start_time": fmt_dt(grid[0]),
        "end_time": fmt_dt(grid[-1]),
        "expected_points": str(TOTAL_POINTS),
        "disturbance_count": str(len(segments)),
        "noise_temperature": str(NOISE_RANGES["temperature"]),
        "noise_humidity": str(NOISE_RANGES["humidity"]),
        "noise_co2": str(NOISE_RANGES["co2"]),
        "noise_light": str(NOISE_RANGES["light"]),
        "disturbances": json.dumps(manifest["disturbances"], ensure_ascii=False),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "generation_mode": mode,
    }
    conn.executemany("INSERT OR REPLACE INTO b02_meta (key, value) VALUES (?, ?)", list(meta.items()))
    return manifest


def write_disturbance_manifest(manifest):
    """扰动片段清单落盘 evidence/b02/disturbance_manifest.json(机器可读交付物)。"""
    try:
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        with open(EVIDENCE_DIR / "disturbance_manifest.json", "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        return True
    except OSError as e:
        print("⚠ 扰动清单写入失败: %s" % e)
        return False


def generate_data(force=False):
    """生成 7 天模拟数据并写入 SQLite。
    · 普通模式:幂等,INSERT OR IGNORE,已有数据绝不覆盖(重启后历史保留的证明);
    · --force:先读取旧锚点再 DROP 重建(同种子同锚点 → 数据逐字节一致)。
    导入本模块或启动 Web 服务绝不触发生成 —— 生成只由本函数显式调用。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_db()
    try:
        old_anchor = None
        if force:
            try:
                row = conn.execute("SELECT value FROM b02_meta WHERE key='anchor'").fetchone()
                if row:
                    old_anchor = parse_dt(row["value"])
            except sqlite3.OperationalError:
                pass  # 首次 --force 时表不存在,忽略
        init_schema(conn)
        anchor = old_anchor or compute_anchor()
        grid = build_grid(anchor)
        segments = build_disturbance_segments(grid)
        if segments is None:
            return False
        rows = generate_points(grid, segments)
        if force:
            conn.execute("DROP TABLE IF EXISTS environment_samples")
            init_schema(conn)
        inserted = skipped = 0
        created_at = datetime.now().astimezone().isoformat(timespec="seconds")
        for row in rows:
            cur = conn.execute(
                "INSERT OR IGNORE INTO environment_samples "
                "(sample_time, temperature, humidity, co2, light, source_type, sim_scenario, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (row[0], row[1], row[2], row[3], row[4], SIM_SOURCE_TYPE, row[5], created_at))
            if cur.rowcount > 0:
                inserted += 1
            else:
                skipped += 1
        mode = "create" if (force or skipped == 0) else "fill"
        manifest = write_meta(conn, anchor, segments, mode)
        conn.commit()
    finally:
        conn.close()
    baseline = sum(1 for r in rows if r[5] == "baseline")
    disturbed = len(rows) - baseline
    print("✓ 数据已写入 %s" % DB_PATH.relative_to(BASE_DIR))
    print("  网格: %s ~ %s (30 分钟/点,共 %d 点,跨度 167.5 小时)" % (rows[0][0], rows[-1][0], len(rows)))
    print("  本次: 插入 %d 点,跳过 %d 点(%s)" % (inserted, skipped, "强制重建" if force else "幂等补插"))
    print("  构成: 基线 %d 点 + 扰动 %d 点;种子 %d;sim_scenario 仅元数据,不作标签/特征" % (baseline, disturbed, SEED))
    write_disturbance_manifest(manifest)
    return True


# ---------------- 公开查询 API(官方契约,供 B06、B07 复用)----------------
def row_to_record(row):
    """sqlite3.Row → 公开 API 形状。显式列清单,不含 sim_scenario(结构性隔离)。"""
    return {
        "sample_time": row["sample_time"],
        "source_type": row["source_type"],
        "temperature": row["temperature"],
        "humidity": row["humidity"],
        "co2": row["co2"],
        "light": row["light"],
    }


def get_current_environment():
    """当前环境状态:库内最新一条样本;空库/库不存在返回 None。"""
    if not DB_PATH.exists():
        return None
    conn = connect_db()
    try:
        row = conn.execute(
            "SELECT %s FROM environment_samples ORDER BY sample_time DESC LIMIT 1" % PUBLIC_COLUMNS
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return row_to_record(row) if row else None


def get_environment_history(hours):
    """读取最近 N 小时历史时序数据(升序)。
    锚点 = 库内 MAX(sample_time)(而非墙钟,重启后查询窗口仍锚定数据尾部)。
    hours<=0 抛 ValueError;空库返回 []。"""
    if hours <= 0:
        raise ValueError("hours 必须为正数")
    anchor_text = db_max_time()
    if anchor_text is None:
        return []
    anchor = parse_dt(anchor_text)
    return query_range(anchor - timedelta(hours=hours), anchor)


def query_range(start, end):
    """时间窗口查询 [start, end] 闭区间,升序;start/end 接受 str 或 datetime。
    库不存在或表缺失时返回 [](不因查询新建空库文件)。"""
    if not DB_PATH.exists():
        return []
    if isinstance(start, datetime):
        start = fmt_dt(start)
    if isinstance(end, datetime):
        end = fmt_dt(end)
    conn = connect_db()
    try:
        rows = conn.execute(
            "SELECT %s FROM environment_samples WHERE sample_time >= ? AND sample_time <= ? "
            "ORDER BY sample_time ASC" % PUBLIC_COLUMNS, (start, end)
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    return [row_to_record(r) for r in rows]


# ---------------- 统计 ----------------
def compute_param_stats(values):
    """单参数窗口统计:count/min/max/mean/std/每小时斜率;n<2 时 std/slope 为 None。"""
    n = len(values)
    result = {
        "count": n,
        "min": min(values) if n else None,
        "max": max(values) if n else None,
        "mean": statistics.mean(values) if n else None,
        "std": statistics.stdev(values) if n >= 2 else None,
        "slope_per_hour": None,
    }
    if n >= 2:
        xs = [i * (GRID_STEP_MINUTES / 60.0) for i in range(n)]  # 30 分钟等距网格,横轴=窗口内小时数
        slope, _intercept = statistics.linear_regression(xs, values)
        result["slope_per_hour"] = slope
    return result


def stats_window(start, end):
    """窗口统计:窗口信息 + 各参数 min/max/mean/std/每小时斜率 + 首末样本。"""
    records = query_range(start, end)
    return {
        "window": {"start": fmt_dt(start) if isinstance(start, datetime) else start,
                   "end": fmt_dt(end) if isinstance(end, datetime) else end},
        "count": len(records),
        "first": records[0] if records else None,
        "last": records[-1] if records else None,
        "parameters": {key: compute_param_stats([r[key] for r in records]) for key in PARAM_KEYS},
    }


def full_window():
    """全窗口 [MIN(sample_time), MAX(sample_time)];空库返回 (None, None)。"""
    start_text, end_text = db_min_time(), db_max_time()
    if start_text is None or end_text is None:
        return None, None
    return parse_dt(start_text), parse_dt(end_text)


# ---------------- 质量检查(10 项,纯读)----------------
def _add_check(report, cid, name, passed, actual, expected, detail):
    report["checks"].append({"id": cid, "name": name, "passed": bool(passed),
                             "actual": actual, "expected": expected, "detail": detail})
    report["summary"]["passed" if passed else "failed"] += 1


def report_fingerprint(report):
    """稳定指纹:按序哈希 id|passed|actual(不含 checked_at,重复运行一致;b08 风格)。"""
    h = hashlib.sha256()
    for c in report["checks"]:
        h.update(("%s|%s|%s\n" % (c["id"], "1" if c["passed"] else "0", c["actual"])).encode("utf-8"))
    return h.hexdigest()[:12]


def run_quality_check():
    """10 项质量校验:空值/重复/异常值/时间连续性/扰动分布等。纯读,不改库。"""
    report = {
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "db_path": str(DB_PATH.relative_to(BASE_DIR)),
        "summary": {"total": 10, "passed": 0, "failed": 0},
        "checks": [],
    }
    if not DB_PATH.exists():
        for cid, name in (
            ("c1_schema", "表结构完整"), ("c2_row_count", "样本行数"), ("c3_nulls", "空值检查"),
            ("c4_duplicates", "重复检查"), ("c5_continuity", "时间连续性"), ("c6_legal_range", "合法范围"),
            ("c7_outliers", "离群点检查"), ("c8_segment_count", "扰动段数量"), ("c9_distribution", "扰动段均匀分布"),
            ("c10_meta", "元数据可追溯"),
        ):
            _add_check(report, cid, name, False, "数据库文件不存在", "通过", "先执行 py b02_memory.py generate")
        report["fingerprint"] = report_fingerprint(report)
        return report

    conn = connect_db()
    try:
        _check_schema(report, conn)
        _check_rows(report, conn)
    finally:
        conn.close()
    report["fingerprint"] = report_fingerprint(report)
    return report


def _check_schema(report, conn):
    """C1 表结构完整:两表存在且字段齐全。"""
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(environment_samples)").fetchall()}
        meta_cols = {r["name"] for r in conn.execute("PRAGMA table_info(b02_meta)").fetchall()}
    except sqlite3.OperationalError:
        _add_check(report, "c1_schema", "表结构完整", False, "表不存在", "environment_samples 与 b02_meta 存在", "建表失败")
        return False
    required = {"sample_time", "temperature", "humidity", "co2", "light",
                "source_type", "sim_scenario", "created_at"}
    ok = required <= cols and {"key", "value"} <= meta_cols
    _add_check(report, "c1_schema", "表结构完整", ok,
               "environment_samples(%d 列) + b02_meta(%d 列)" % (len(cols), len(meta_cols)),
               "两表存在且字段齐全", "字段齐全即可通过")
    return ok


def _check_rows(report, conn):
    """C2~C10 行级检查。"""
    # C2 行数
    count = conn.execute("SELECT COUNT(*) AS n FROM environment_samples").fetchone()["n"]
    _add_check(report, "c2_row_count", "样本行数", count == TOTAL_POINTS, "%d 行" % count,
               "== %d (30 分钟 × 7 天)" % TOTAL_POINTS, "行数恒等于 336")

    # C3 空值
    null_cols = []
    for col in ("sample_time", "temperature", "humidity", "co2", "light", "source_type", "sim_scenario", "created_at"):
        n = conn.execute("SELECT COUNT(*) AS n FROM environment_samples WHERE %s IS NULL" % col).fetchone()["n"]
        if n:
            null_cols.append("%s=%d" % (col, n))
    _add_check(report, "c3_nulls", "空值检查", not null_cols, "; ".join(null_cols) if null_cols else "0 个空值",
               "全列无 NULL", "NOT NULL 约束 + 生成器逐点写值")

    # C4 重复
    dup = conn.execute(
        "SELECT COUNT(*) AS n FROM (SELECT sample_time FROM environment_samples "
        "GROUP BY sample_time HAVING COUNT(*) > 1)").fetchone()["n"]
    _add_check(report, "c4_duplicates", "重复检查", dup == 0, "%d 个重复采样时间" % dup,
               "0 个重复", "UNIQUE(sample_time) 约束兜底")

    # C5 时间连续性
    times = [r["sample_time"] for r in conn.execute(
        "SELECT sample_time FROM environment_samples ORDER BY sample_time ASC").fetchall()]
    fmt_re = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
    bad_fmt = sum(1 for t in times if not fmt_re.match(t))
    parsed, bad_parse = [], []
    for t in times:
        try:
            parsed.append(parse_dt(t))
        except ValueError:
            bad_parse.append(t)
    bad_grid = sum(1 for dt in parsed if dt.minute not in (0, 30) or dt.second != 0)
    gaps = 0
    if len(parsed) == len(times) and parsed:
        gaps = sum(1 for a, b in zip(parsed, parsed[1:])
                   if (b - a).total_seconds() != GRID_STEP_MINUTES * 60)
    span_hours = (parsed[-1] - parsed[0]).total_seconds() / 3600.0 if len(parsed) >= 2 else 0.0
    c5_ok = (not bad_fmt and not bad_parse and bad_grid == 0 and gaps == 0
             and abs(span_hours - 167.5) < 1e-6 and len(times) == TOTAL_POINTS)
    _add_check(report, "c5_continuity", "时间连续性", c5_ok,
               "格式异常 %d,非网格时刻 %d,断点 %d,首末跨度 %.1fh" % (bad_fmt + len(bad_parse), bad_grid, gaps, span_hours),
               "全点 30 分钟等距、无断点、跨度 167.5h", "固定网格 + 严格格式")

    # C6 合法范围
    violations = []
    for key in PARAM_KEYS:
        spec = PARAM_SPEC_MAP[key]
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM environment_samples WHERE %s < ? OR %s > ?" % (key, key),
            (spec["min"], spec["max"])).fetchone()["n"]
        if n:
            violations.append("%s=%d" % (key, n))
    _add_check(report, "c6_legal_range", "合法范围", not violations,
               "; ".join(violations) if violations else "0 个越界值",
               "温度 5~40 / 湿度 30~90 / CO₂ 300~2000 / 光照 0~100000",
               "生成时钳制合法范围")

    # C7 离群点:对"相对生成器模型期望的残差"(值 − 基线/扰动目标)做 MAD 修正 z 分数,阈值 3.5。
    # 对原始值做 z 分数会误报(光照昼夜双峰),残差剔除昼夜结构后仅剩噪声,不会误报。
    meta_raw = {r["key"]: r["value"] for r in conn.execute(
        "SELECT key, value FROM b02_meta").fetchall()}  # C7/C10 共用
    seg_targets = {}  # sim_scenario -> {key: 目标值}(来自 b02_meta 扰动清单)
    try:
        meta_dist = json.loads(meta_raw.get("disturbances", "[]"))
        seg_targets = {d["scenario"]: d.get("targets", {}) for d in meta_dist if isinstance(d, dict)}
    except (ValueError, TypeError):
        seg_targets = {}
    residuals = {key: [] for key in PARAM_KEYS}
    for r in conn.execute(
            "SELECT sample_time, temperature, humidity, co2, light, sim_scenario "
            "FROM environment_samples").fetchall():
        dt = parse_dt(r["sample_time"])
        h = dt.hour + dt.minute / 60.0
        targets = seg_targets.get(r["sim_scenario"], {})
        for key in PARAM_KEYS:
            expected = targets[key] if key in targets else baseline_value(key, h)
            residuals[key].append(r[key] - expected)
    flagged, worst_z = [], 0.0
    for key in PARAM_KEYS:
        values = residuals[key]
        if len(values) < 2:
            continue
        med = statistics.median(values)
        mad = statistics.median([abs(v - med) for v in values])
        if mad > 0:
            zs = [0.6745 * (v - med) / mad for v in values]
        else:
            sd = statistics.stdev(values)
            zs = [(v - med) / sd for v in values] if sd > 0 else [0.0] * len(values)
        worst_z = max(worst_z, max(abs(z) for z in zs))
        n = sum(1 for z in zs if abs(z) > 3.5)
        if n:
            flagged.append("%s=%d" % (key, n))
    _add_check(report, "c7_outliers", "离群点检查", not flagged,
               ("%s;最大修正 z 分数 %.2f" % ("; ".join(flagged), worst_z)) if flagged
               else "0 个离群点;最大修正 z 分数 %.2f" % worst_z,
               "|修正 z| > 3.5 的样本为 0", "残差(值-模型期望)MAD 稳健检测,昼夜结构不误报")

    # C8/C9 扰动段(按 sim_scenario 连续 run 分组 —— 仅质量检查内部使用,不影响公开 API)
    rows = [(r["sample_time"], r["sim_scenario"]) for r in conn.execute(
        "SELECT sample_time, sim_scenario FROM environment_samples ORDER BY sample_time ASC").fetchall()]
    runs = []  # [(scenario, start_dt, end_dt, count)]
    for t, scenario in rows:
        dt = parse_dt(t)
        if runs and runs[-1][0] == scenario:
            runs[-1] = (scenario, runs[-1][1], dt, runs[-1][3] + 1)
        else:
            runs.append((scenario, dt, dt, 1))
    dist_runs = [r for r in runs if r[0] != "baseline"]
    seg_count = len(dist_runs)
    _add_check(report, "c8_segment_count", "扰动段数量", 4 <= seg_count <= 6, "%d 段" % seg_count,
               "4~6 段", "扰动片段连续 run 分组计数")

    ok_details = []
    all_ok = True
    if dist_runs:
        # 时长 ∈ [2.0, 4.0] h
        durations = [(r[2] - r[1]).total_seconds() / 3600.0 for r in dist_runs]
        dur_ok = all(DIST_DURATION_RANGE[0] <= d <= DIST_DURATION_RANGE[1] for d in durations)
        all_ok &= dur_ok
        ok_details.append("各段时长 %s h" % "/".join("%.1f" % d for d in durations))
        # 起始日历日互异
        dates = [r[1].date() for r in dist_runs]
        date_ok = len(set(dates)) == seg_count
        all_ok &= date_ok
        # 相邻起始间隔 ≤40h,首末跨度 ≥96h
        starts = [r[1] for r in dist_runs]
        gaps = [(starts[i + 1] - starts[i]).total_seconds() / 3600.0 for i in range(seg_count - 1)]
        span = (starts[-1] - starts[0]).total_seconds() / 3600.0
        gap_ok = (seg_count == 1) or all(g <= DIST_MAX_GAP_HOURS for g in gaps)
        span_ok = span >= DIST_MIN_SPAN_HOURS
        all_ok &= gap_ok and span_ok
        ok_details.append("起始间隔 %s h,首末跨度 %.1f h" % (gaps, span))
    else:
        all_ok = False
        ok_details.append("无扰动段")
    _add_check(report, "c9_distribution", "扰动段均匀分布", all_ok,
               ";".join(ok_details),
               "各段 2~4h、日历日互异、间隔 ≤40h、跨度 ≥96h", "与生成自校验同规则")

    # C10 元数据可追溯
    meta = meta_raw
    required_keys = ("schema_version", "generator_version", "seed", "anchor", "generated_at")
    missing = [k for k in required_keys if k not in meta]
    seed_ok = meta.get("seed") == str(SEED)
    c10_ok = not missing and seed_ok
    _add_check(report, "c10_meta", "元数据可追溯", c10_ok,
               "缺失 %s;seed=%s" % (missing or "无", meta.get("seed", "无")),
               "b02_meta 含版本/种子/锚点/生成时间,seed=42", "write_meta 落盘生成器配置")


def save_quality_report(report):
    """质量报告落盘 evidence/b02/quality_report.json;返回是否成功。"""
    try:
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        with open(EVIDENCE_DIR / "quality_report.json", "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        return True
    except OSError as e:
        print("⚠ 质量报告写入失败: %s" % e)
        return False


def print_quality_report(report):
    """控制台打印检查清单(✓/⚠ 风格)。"""
    print("\n================ B02 质量校验报告 ================")
    print("数据库: %s" % report["db_path"])
    for c in report["checks"]:
        mark = "✓" if c["passed"] else "⚠"
        print("  %s [%s] %s: %s" % (mark, c["id"], c["name"], c["detail"]))
        print("      实际: %s | 期望: %s" % (c["actual"], c["expected"]))
    print("---------------- 汇总 ----------------")
    print("  通过 %d / %d,指纹 %s" % (report["summary"]["passed"], report["summary"]["total"],
                                    report.get("fingerprint", "-")))


def dump_sample_stats():
    """全窗口 / 最近 7 天 / 3 天 / 24 小时 四窗口统计落盘 evidence/b02/sample_stats.json。"""
    start, end = full_window()
    if start is None:
        print("⚠ 数据库为空,无法生成样本统计")
        return False
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "db_path": str(DB_PATH.relative_to(BASE_DIR)),
        "windows": {
            "all": stats_window(start, end),
            "last_7d": stats_window(end - timedelta(days=7), end),
            "last_3d": stats_window(end - timedelta(days=3), end),
            "last_24h": stats_window(end - timedelta(hours=24), end),
        },
    }
    try:
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        with open(EVIDENCE_DIR / "sample_stats.json", "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return True
    except OSError as e:
        print("⚠ 样本统计写入失败: %s" % e)
        return False


# ---------------- CLI ----------------
def fmt_val(value, key):
    """CLI 数值格式化:温度/湿度 2 位小数,CO₂/光照 1 位;None → '—'。"""
    if value is None:
        return "—"
    return "%.2f" % value if key in ("temperature", "humidity") else "%.1f" % value


def print_usage():
    """打印 B02 用法说明。"""
    print("用法:")
    print("  py b02_memory.py generate          # 生成 7 天模拟数据(幂等,已有数据不覆盖)")
    print("  py b02_memory.py generate --force  # 强制重建(沿用旧锚点,数据一致)")
    print("  py b02_memory.py current           # 查看当前环境状态(库内最新一条)")
    print("  py b02_memory.py history --hours N # 查看最近 N 小时历史时序数据(默认 24)")
    print("  py b02_memory.py stats [--hours N] # 窗口统计(默认全窗口,落盘样本统计)")
    print("        或 stats --from X --to Y     #   自定义窗口,如 2026-10-27 00:00 ~ 2026-10-28 00:00")
    print("  py b02_memory.py check             # 质量校验(空值/重复/异常值/连续性/扰动分布)")
    print("  py b02_memory.py web [--port N]    # 启动 Web 时间序列可视化页面(Chart.js)")
    print("  py b02_memory.py --help            # 查看本说明")
    print("环境变量: B02_WEB_PORT(Web 端口,默认 8223)")
    print("公开接口: get_current_environment() / get_environment_history(hours),供 B06、B07 复用")


def cmd_generate(args):
    """子命令 generate:幂等生成;--force 强制重建。"""
    generate_data(force="--force" in args)


def cmd_current():
    """子命令 current:打印库内最新一条环境状态。"""
    record = get_current_environment()
    if record is None:
        print("⚠ 数据库为空或不存在,请先执行 py b02_memory.py generate")
        return
    print("\n---------------- 当前环境状态(库内最新样本)----------------")
    print("  采样时间: %s" % record["sample_time"])
    print("  数据来源: %s" % record["source_type"])
    for spec in PARAM_SPECS:
        print("  %s: %s %s" % (spec["name"], fmt_val(record[spec["key"]], spec["key"]), spec["unit"]))


def cmd_history(args):
    """子命令 history:最近 N 小时时序数据(默认 24)。"""
    hours = 24.0
    if "--hours" in args:
        i = args.index("--hours")
        if i + 1 >= len(args):
            print("⚠ --hours 需要参数,如 --hours 24")
            return
        try:
            hours = float(args[i + 1])
        except ValueError:
            print("⚠ hours 必须是数字")
            return
    if not 0 < hours <= 8760:
        print("⚠ hours 需满足 0 < hours <= 8760")
        return
    records = get_environment_history(hours)
    print("\n---------------- 最近 %g 小时历史时序数据 ----------------" % hours)
    if not records:
        print("⚠ 数据库为空,请先执行 py b02_memory.py generate")
        return
    print("窗口: %s ~ %s (锚点=库内最新样本时间),共 %d 条,升序"
          % (records[0]["sample_time"], records[-1]["sample_time"], len(records)))
    print("%-4s %-20s %-9s %-9s %-9s %-9s" % ("#", "sample_time", "温度(℃)", "湿度(%RH)", "CO₂(ppm)", "光照(lux)"))
    for i, r in enumerate(records, start=1):
        print("%-4d %-20s %-9s %-9s %-9s %-9s" % (i, r["sample_time"], fmt_val(r["temperature"], "temperature"),
                                                  fmt_val(r["humidity"], "humidity"), fmt_val(r["co2"], "co2"),
                                                  fmt_val(r["light"], "light")))


def _resolve_stats_window(args):
    """解析 stats 窗口参数:--hours N 或 --from X --to Y;默认全窗口。
    返回 (start, end) datetime 或 None(参数错误时)。"""
    if "--hours" in args:
        i = args.index("--hours")
        if i + 1 >= len(args):
            print("⚠ --hours 需要参数,如 --hours 24")
            return None
        try:
            hours = float(args[i + 1])
        except ValueError:
            print("⚠ hours 必须是数字")
            return None
        if not 0 < hours <= 8760:
            print("⚠ hours 需满足 0 < hours <= 8760")
            return None
        anchor_text = db_max_time()
        if anchor_text is None:
            return "empty"
        anchor = parse_dt(anchor_text)
        return anchor - timedelta(hours=hours), anchor
    if "--from" in args or "--to" in args:
        if "--from" not in args or "--to" not in args:
            print("⚠ --from 与 --to 必须成对出现")
            return None
        try:
            start = parse_dt_web(args[args.index("--from") + 1])
            end = parse_dt_web(args[args.index("--to") + 1])
        except (IndexError, ValueError) as e:
            print("⚠ 时间参数非法: %s(格式 YYYY-MM-DD HH:MM 或 YYYY-MM-DDTHH:MM)" % e)
            return None
        if start >= end:
            print("⚠ --from 必须早于 --to")
            return None
        return start, end
    window = full_window()
    if window == (None, None):
        return "empty"
    return window


def _print_stats(title, result):
    """打印窗口统计表。"""
    print("\n---------------- %s ----------------" % title)
    if result["count"] == 0:
        print("⚠ 该窗口内无数据")
        return
    print("窗口: %s ~ %s,共 %d 个采样点" % (result["window"]["start"], result["window"]["end"], result["count"]))
    print("%-9s %-7s %-11s %-11s %-11s %-9s %-10s" %
          ("参数", "样本数", "最小", "最大", "均值", "标准差", "斜率/小时"))
    for key in PARAM_KEYS:
        p = result["parameters"][key]
        spec = PARAM_SPEC_MAP[key]
        slope = "%+.3f" % p["slope_per_hour"] if p["slope_per_hour"] is not None else "—"
        print("%-9s %-7d %-11s %-11s %-11s %-9s %-10s" %
              ("%s(%s)" % (spec["name"], spec["unit"]), p["count"], fmt_val(p["min"], key),
               fmt_val(p["max"], key), fmt_val(p["mean"], key),
               "%.2f" % p["std"] if p["std"] is not None else "—", slope))


def cmd_stats(args):
    """子命令 stats:窗口统计;无窗口参数时统计全窗口并落盘 sample_stats.json。"""
    window = _resolve_stats_window(args)
    if window is None:
        return
    if window == "empty":
        print("⚠ 数据库为空或不存在,请先执行 py b02_memory.py generate")
        return
    start, end = window
    result = stats_window(start, end)
    _print_stats("窗口统计", result)
    if "--hours" not in args and "--from" not in args:
        if dump_sample_stats():
            print("✓ 样本统计已保存: evidence/b02/sample_stats.json(全窗口/7天/3天/24小时)")


def cmd_check():
    """子命令 check:运行 10 项质量校验,打印并落盘报告。退出码恒 0(检查清单语义)。"""
    report = run_quality_check()
    print_quality_report(report)
    if save_quality_report(report):
        print("✓ 质量报告已保存: evidence/b02/quality_report.json")


# ---------------- Web 可视化(Chart.js 本地文件,真实 SQL 时间窗查询)----------------
PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GreenMate B02 数据记忆模块 — 环境数据时间序列</title>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --ink-3: #898781;
  --border: rgba(11, 11, 11, 0.10); --grid: #e1e0d9; --axis: #c3c2b7;
  --series: #2a78d6; --trend: #52514e; --good: #0ca30c; --warn: #fab219;
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --ink-3: #898781;
    --border: rgba(255, 255, 255, 0.10); --grid: #2c2c2a; --axis: #383835;
    --series: #3987e5; --trend: #c3c2b7;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif; line-height: 1.6; }
.wrap { max-width: 980px; margin: 0 auto; padding: 24px 20px 48px; }
h1 { font-size: 22px; margin: 0; }
.sub { color: var(--ink-3); font-size: 12px; margin: 4px 0 18px; }
.panel { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px 18px; margin: 14px 0; }
.panel h2 { font-size: 15px; margin: 0 0 10px; }
#degrade-banner { margin: 14px 0; padding: 10px 14px; border: 1px solid var(--warn); color: var(--warn); border-radius: 8px; font-size: 13px; }
.hidden { display: none; }
/* 当前状态卡 */
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
.metric-card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 10px 14px; }
.metric-name { color: var(--ink-3); font-size: 12px; }
.metric-value { font-size: 22px; font-weight: 600; }
.metric-unit { font-size: 12px; color: var(--ink-2); }
.sim-badge { display: inline-block; margin-top: 6px; padding: 1px 8px; border: 1px solid var(--series); color: var(--series); border-radius: 4px; font-size: 11px; }
/* 范围筛选行:一行、置于所有图表之上 */
.filter-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; padding: 10px 14px;
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px; margin: 14px 0; }
.filter-row .sep { color: var(--ink-3); font-size: 12px; }
.filter-row label { font-size: 13px; color: var(--ink-2); }
.filter-row input[type=datetime-local] { padding: 5px 8px; border: 1px solid var(--border); border-radius: 6px;
  background: var(--surface); color: var(--ink); font-size: 13px; }
button { padding: 6px 14px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface);
  color: var(--ink); cursor: pointer; font-size: 13px; }
button:hover { border-color: var(--series); }
button.active { border-color: var(--series); color: var(--series); font-weight: 600; }
/* 图表区:4 张单轴折线图(小倍数),各自独立 Y 轴 */
.chart-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 14px; }
.chart-card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; }
.chart-card h3 { font-size: 14px; margin: 0 0 8px; font-weight: 600; }
.chart-box { position: relative; height: 240px; }
.chart-box.loading { opacity: 0.6; }
/* 统计表:图表的表格视图(无障碍等价通道) */
table.stats { width: 100%; border-collapse: collapse; font-size: 13px; }
.stats th, .stats td { text-align: right; padding: 6px 8px; border-bottom: 1px solid var(--border); font-variant-numeric: tabular-nums; }
.stats th { color: var(--ink-3); font-weight: 500; white-space: nowrap; }
.stats th:first-child, .stats td:first-child { text-align: left; }
.stats tr:last-child td { border-bottom: none; }
.window-line { font-size: 12px; color: var(--ink-3); margin-bottom: 8px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>GreenMate B02 数据记忆模块 — 环境数据时间序列</h1>
  <p class="sub">数据均为模拟采集 source_type=SIMULATED;sim_scenario 为模拟器内部元数据,不作为标签或特征,本页不展示。</p>
  <div id="degrade-banner" class="hidden">提示: Chart.js 本地文件缺失,图表不可用,统计表与当前状态不受影响。</div>
  <div class="panel" id="current-panel">
    <h2>当前环境状态(库内最新样本)</h2>
    <div class="cards" id="metrics"></div>
    <p class="sub" id="current-meta"></p>
  </div>
  <div class="filter-row" id="filter-row">
    <span class="sep">时间范围:</span>
    <button type="button" data-hours="6">最近6小时</button>
    <button type="button" data-hours="24">最近24小时</button>
    <button type="button" data-hours="72">最近3天</button>
    <button type="button" data-hours="168">最近7天</button>
    <button type="button" data-preset="all">全部</button>
    <span class="sep">|</span>
    <label for="in-from">从</label>
    <input type="datetime-local" id="in-from" step="1800">
    <label for="in-to">至</label>
    <input type="datetime-local" id="in-to" step="1800">
    <button type="button" id="btn-query">查询</button>
  </div>
  <div class="chart-grid" id="chart-grid">
    <div class="chart-card"><h3>温度 (℃)</h3><div class="chart-box"><canvas id="chart-temperature"></canvas></div></div>
    <div class="chart-card"><h3>湿度 (%RH)</h3><div class="chart-box"><canvas id="chart-humidity"></canvas></div></div>
    <div class="chart-card"><h3>CO₂浓度 (ppm)</h3><div class="chart-box"><canvas id="chart-co2"></canvas></div></div>
    <div class="chart-card"><h3>光照 (lux)</h3><div class="chart-box"><canvas id="chart-light"></canvas></div></div>
  </div>
  <div class="panel">
    <h2>窗口统计</h2>
    <p class="window-line" id="window-line"></p>
    <table class="stats">
      <thead><tr><th>参数</th><th>样本数</th><th>最小</th><th>最大</th><th>均值</th><th>标准差</th><th>斜率/小时</th><th>起始值</th><th>最新值</th></tr></thead>
      <tbody id="stats-body"></tbody>
    </table>
  </div>
</div>
<script src="/chart.umd.js"></script>
<script>
"use strict";
// ---- 主题色:从 CSS 变量读取,深/浅色随系统切换重建图表颜色 ----
function palette() {
  var cs = getComputedStyle(document.documentElement);
  return {
    series: cs.getPropertyValue("--series").trim() || "#2a78d6",
    trend: cs.getPropertyValue("--trend").trim() || "#52514e",
    ink: cs.getPropertyValue("--ink").trim() || "#0b0b0b",
    ink2: cs.getPropertyValue("--ink-2").trim() || "#52514e",
    ink3: cs.getPropertyValue("--ink-3").trim() || "#898781",
    grid: cs.getPropertyValue("--grid").trim() || "#e1e0d9",
    axis: cs.getPropertyValue("--axis").trim() || "#c3c2b7",
    surface: cs.getPropertyValue("--surface").trim() || "#fcfcfb",
    border: cs.getPropertyValue("--border").trim() || "rgba(11,11,11,0.10)"
  };
}
var PARAMS = [
  { key: "temperature", name: "温度", unit: "℃" },
  { key: "humidity", name: "湿度", unit: "%RH" },
  { key: "co2", name: "CO₂浓度", unit: "ppm" },
  { key: "light", name: "光照", unit: "lux" }
];
var charts = {};
var anchor = null;      // 库内最新样本时间(锚点),由 /api/current 提供
var rangeState = null;  // 当前展示窗口 {from, to}

function fmtValue(key, v) {
  if (v === null || v === undefined) { return "—"; }
  return (key === "temperature" || key === "humidity") ? v.toFixed(2) : v.toFixed(1);
}
function dtLocal(text) {  // 'YYYY-MM-DD HH:MM:SS' -> Date(本地时区)
  var m = /^(\\d{4})-(\\d{2})-(\\d{2}) (\\d{2}):(\\d{2}):(\\d{2})$/.exec(text);
  if (!m) { return null; }
  return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]);
}
function inputFmt(text) {  // 转为 datetime-local 输入框格式 YYYY-MM-DDTHH:MM
  return text.slice(0, 16).replace(" ", "T");
}
// 趋势线末端直接标注(单数据系列 + 趋势,直接标注取代图例)
var trendLabelPlugin = {
  id: "trendLabel",
  afterDatasetsDraw: function (chart) {
    var meta = chart.getDatasetMeta(1);
    if (!meta || !meta.data || meta.data.length === 0) { return; }
    var pts = meta.data;
    var last = null;
    for (var i = pts.length - 1; i >= 0; i--) {
      if (pts[i].y !== undefined && pts[i].y !== null) { last = pts[i]; break; }
    }
    if (!last) { return; }
    var ctx = chart.ctx;
    var p = palette();
    ctx.save();
    ctx.font = "12px system-ui, sans-serif";
    ctx.fillStyle = p.ink3;
    ctx.textAlign = "right";
    ctx.textBaseline = "bottom";
    ctx.fillText("趋势", last.x - 6, last.y - 8);
    ctx.restore();
  }
};
if (typeof Chart !== "undefined") { Chart.register(trendLabelPlugin); }  // Chart.js 缺失时整页降级,不崩溃
function makeChart(key) {
  var el = document.getElementById("chart-" + key);
  var p = palette();
  var param = PARAMS.filter(function (x) { return x.key === key; })[0];
  return new Chart(el, {
    type: "line",
    data: { labels: [], datasets: [
      { label: param.name, data: [], borderColor: p.series, borderWidth: 2,
        pointRadius: 0, borderJoinStyle: "round", tension: 0 },
      { label: "趋势", data: [], borderColor: p.trend, borderWidth: 1,
        pointRadius: 0, spanGaps: true }
    ]},
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: p.surface, borderColor: p.border, borderWidth: 1,
          titleColor: p.ink, bodyColor: p.ink2, titleFont: { size: 12 }, bodyFont: { size: 12, weight: "600" },
          callbacks: {
            title: function (items) { return items.length ? items[0].label : ""; },
            label: function (item) {
              if (item.dataset.label === "趋势") { return null; }
              return " " + fmtValue(key, item.parsed.y) + " " + param.unit;
            }
          }
        }
      },
      scales: {
        x: { type: "category", ticks: { color: p.ink3, maxTicksLimit: 8, autoSkip: true,
              font: { size: 11, family: "system-ui, sans-serif" } },
             grid: { display: false } },
        y: { beginAtZero: key === "light", ticks: { color: p.ink3, font: { size: 11 } },
             grid: { color: p.grid, lineWidth: 1, drawTicks: false },
             border: { color: p.axis, width: 1 } }
      }
    }
  });
}
function trendEndpoints(values) {
  // 最小二乘直线,取首尾两点(横轴=样本序号,等距 30 分钟)
  var n = values.length;
  if (n < 2) { return null; }
  var sx = 0, sy = 0, sxx = 0, sxy = 0;
  for (var i = 0; i < n; i++) { sx += i; sy += values[i]; sxx += i * i; sxy += i * values[i]; }
  var slope = (n * sxy - sx * sy) / (n * sxx - sx * sx);
  var intercept = (sy - slope * sx) / n;
  return [intercept, intercept + slope * (n - 1)];
}
function updateCharts(records, stats) {
  var labels = records.map(function (r) { return r.sample_time.slice(5, 16); });  // MM-DD HH:mm
  PARAMS.forEach(function (param) {
    var values = records.map(function (r) { return r[param.key]; });
    var chart = charts[param.key];
    if (!chart) { return; }  // Chart.js 缺失降级:跳过绘图,统计表照常
    chart.data.labels = labels;
    chart.data.datasets[0].data = values;
    var ends = trendEndpoints(values);
    var trend = new Array(values.length).fill(null);
    if (ends) { trend[0] = ends[0]; trend[values.length - 1] = ends[1]; }
    chart.data.datasets[1].data = trend;
    chart.update("none");
  });
  renderStats(stats);
}
function renderStats(stats) {
  var body = document.getElementById("stats-body");
  body.textContent = "";
  var line = document.getElementById("window-line");
  if (!stats || stats.count === 0) {
    line.textContent = "该时间范围内无数据";
    return;
  }
  line.textContent = "窗口: " + stats.window.start + " ~ " + stats.window.end + ",共 " + stats.count + " 个采样点";
  PARAMS.forEach(function (param) {
    var p = stats.parameters[param.key];
    var tr = document.createElement("tr");
    var cells = [param.name + " (" + param.unit + ")", String(p.count), fmtValue(param.key, p.min),
                 fmtValue(param.key, p.max), fmtValue(param.key, p.mean),
                 p.std === null ? "—" : p.std.toFixed(2),
                 p.slope_per_hour === null ? "—" : (p.slope_per_hour >= 0 ? "+" : "") + p.slope_per_hour.toFixed(3),
                 stats.first ? fmtValue(param.key, stats.first[param.key]) : "—",
                 stats.last ? fmtValue(param.key, stats.last[param.key]) : "—"];
    for (var i = 0; i < cells.length; i++) {
      var td = document.createElement("td");
      td.textContent = cells[i];   // textContent 渲染,防注入
      tr.appendChild(td);
    }
    body.appendChild(tr);
  });
}
function renderCurrent(record) {
  var box = document.getElementById("metrics");
  box.textContent = "";
  PARAMS.forEach(function (param) {
    var card = document.createElement("div");
    card.className = "metric-card";
    var name = document.createElement("div");
    name.className = "metric-name";
    name.textContent = param.name;
    var value = document.createElement("div");
    value.className = "metric-value";
    value.textContent = fmtValue(param.key, record[param.key]);
    var unit = document.createElement("span");
    unit.className = "metric-unit";
    unit.textContent = " " + param.unit;
    var badge = document.createElement("span");
    badge.className = "sim-badge";
    badge.textContent = "source_type=" + (record.source_type || "—");
    card.appendChild(name); card.appendChild(value); card.appendChild(unit); card.appendChild(badge);
    box.appendChild(card);
  });
  document.getElementById("current-meta").textContent =
    "采样时间: " + record.sample_time + " · 数据来源: " + record.source_type;
}
function setLoading(on) {
  var boxes = document.querySelectorAll(".chart-box");
  for (var i = 0; i < boxes.length; i++) { boxes[i].classList.toggle("loading", on); }
}
function fetchJson(url) { return fetch(url).then(function (r) { return r.json(); }); }
function loadRange(from, to) {
  setLoading(true);   // 重查期间保持上一帧渲染(降低不透明度),无骨架闪烁
  var q = "from=" + encodeURIComponent(from) + "&to=" + encodeURIComponent(to);
  return Promise.all([fetchJson("/api/history_range?" + q), fetchJson("/api/stats?" + q)])
    .then(function (results) {
      var hist = results[0], stats = results[1];
      if (hist.status !== "ok" || stats.status !== "ok") { throw new Error(hist.message || stats.message || "查询失败"); }
      rangeState = { from: from, to: to };
      document.getElementById("in-from").value = inputFmt(from);
      document.getElementById("in-to").value = inputFmt(to);
      updateCharts(hist.records, stats);
    })
    .catch(function (e) { showQueryError(e.message || "查询失败"); })
    .finally(setLoading.bind(null, false));
}
function showQueryError(msg) {
  var line = document.getElementById("window-line");
  line.textContent = "查询失败: " + msg;
}
function markActive(btn) {
  var buttons = document.querySelectorAll("#filter-row button[data-hours], #filter-row button[data-preset]");
  for (var i = 0; i < buttons.length; i++) { buttons[i].classList.toggle("active", buttons[i] === btn); }
}
function applyPreset(btn) {
  if (!anchor) { return; }
  markActive(btn);
  if (btn.getAttribute("data-preset") === "all") {
    return fetchJson("/api/stats").then(function (stats) {
      if (stats.status !== "ok") { throw new Error(stats.message || "查询失败"); }
      return loadRange(stats.window.start, stats.window.end);
    }).catch(function (e) { showQueryError(e.message || "查询失败"); });
  }
  var hours = parseFloat(btn.getAttribute("data-hours"));
  var from = new Date(anchor.getTime() - hours * 3600 * 1000);
  var to = anchor;
  var pad = function (n) { return (n < 10 ? "0" : "") + n; };
  var fmt = function (d) {
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) +
           "T" + pad(d.getHours()) + ":" + pad(d.getMinutes());
  };
  loadRange(fmt(from), fmt(to));
}
function bindControls() {
  var buttons = document.querySelectorAll("#filter-row button[data-hours], #filter-row button[data-preset]");
  for (var i = 0; i < buttons.length; i++) {
    buttons[i].addEventListener("click", function () { applyPreset(this); });
  }
  document.getElementById("btn-query").addEventListener("click", function () {
    var from = document.getElementById("in-from").value;
    var to = document.getElementById("in-to").value;
    if (!from || !to) { showQueryError("请填写完整的起止时间"); return; }
    if (!(from < to)) { showQueryError("起始时间必须早于结束时间"); return; }
    markActive(null);
    loadRange(from, to);
  });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
    // 深/浅色切换:按新主题重建各图颜色(系列、趋势、网格、刻度)
    var p = palette();
    PARAMS.forEach(function (param) {
      var chart = charts[param.key];
      if (!chart) { return; }
      chart.data.datasets[0].borderColor = p.series;
      chart.data.datasets[1].borderColor = p.trend;
      chart.options.plugins.tooltip.backgroundColor = p.surface;
      chart.options.plugins.tooltip.borderColor = p.border;
      chart.options.plugins.tooltip.titleColor = p.ink;
      chart.options.plugins.tooltip.bodyColor = p.ink2;
      chart.options.scales.x.ticks.color = p.ink3;
      chart.options.scales.y.ticks.color = p.ink3;
      chart.options.scales.y.grid.color = p.grid;
      chart.options.scales.y.border.color = p.axis;
      chart.update("none");
    });
  });
}
function init() {
  if (typeof Chart === "undefined") {
    document.getElementById("degrade-banner").classList.remove("hidden");  // 降级:统计表仍可用
  } else {
    PARAMS.forEach(function (param) { charts[param.key] = makeChart(param.key); });
  }
  bindControls();
  fetchJson("/api/current").then(function (d) {
    if (d.status !== "ok") { document.getElementById("current-meta").textContent = "暂无数据:请先执行 py b02_memory.py generate"; return; }
    renderCurrent(d.record);
    anchor = dtLocal(d.record.sample_time);
    // 初始视图:全部(整个 7 天窗口)
    return fetchJson("/api/stats").then(function (stats) {
      if (stats.status !== "ok") { return; }
      loadRange(stats.window.start, stats.window.end);
      var allBtn = document.querySelector("#filter-row button[data-preset=all]");
      if (allBtn) { markActive(allBtn); }
    });
  }).catch(function () { document.getElementById("current-meta").textContent = "加载当前状态失败"; });
}
document.addEventListener("DOMContentLoaded", init);
</script>
</body>
</html>
"""


def build_page():
    """返回 B02 可视化页面 HTML(当前无动态注入内容;模板保持 b06 的 .replace 填充约定)。"""
    return PAGE_HTML


def _json_response(handler, code, obj):
    """写 JSON 响应(UTF-8,长度按字节计算;b01 同款)。"""
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _get_param(params, name, default=None):
    """parse_qs 结果取值:单值参数取第一个,未提供返回 default。"""
    values = params.get(name)
    return values[0] if values else default


class B02RequestHandler(BaseHTTPRequestHandler):
    """B02 Web 服务:时间序列可视化页 + 真实 SQLite 时间窗查询接口。
    所有 API 均只读库(除 / 与 /chart.umd.min.js),异常一律 JSON 如实返回,零 traceback。"""
    server_version = "GreenMateB02/1.0"

    def log_message(self, format, *args):  # 关闭默认 stderr 噪音,与 B01 一致
        pass

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            params = parse_qs(parsed.query)
            if path == "/":
                page = build_page().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
                return
            if path == "/chart.umd.js":
                if CHART_JS_PATH.exists():
                    data = CHART_JS_PATH.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/javascript; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    _json_response(self, 404, {"status": "error", "error_type": "chart_missing",
                                               "message": "Chart.js 本地文件缺失(b02_assets/chart.umd.js)"})
                return
            if path == "/api/current":
                record = get_current_environment()
                if record is None:
                    _json_response(self, 404, {"status": "error", "error_type": "no_data",
                                               "message": "数据库为空,请先执行 py b02_memory.py generate"})
                else:
                    _json_response(self, 200, {"status": "ok", "record": record})
                return
            if path == "/api/history":
                self._api_history(params)
                return
            if path == "/api/history_range":
                self._api_history_range(params)
                return
            if path == "/api/stats":
                self._api_stats(params)
                return
            if path == "/api/check":
                report = run_quality_check()
                _json_response(self, 200, {"status": "ok", "report": report})
                return
            if path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            _json_response(self, 404, {"status": "error", "error_type": "not_found",
                                       "message": "404 未找到: %s" % path})
        except Exception as e:
            try:
                _json_response(self, 500, {"status": "error", "error_type": "server_error",
                                           "message": str(e)[:300]})
            except Exception:
                pass

    def _api_history(self, params):
        """GET /api/history?hours=N(默认 24,0<N≤8760):最近 N 小时,锚点=库内 MAX(sample_time)。"""
        raw = _get_param(params, "hours", "24")
        try:
            hours = float(raw)
        except ValueError:
            _json_response(self, 400, {"status": "error", "error_type": "bad_param",
                                       "message": "hours 必须是数字"})
            return
        if not 0 < hours <= 8760:
            _json_response(self, 400, {"status": "error", "error_type": "bad_param",
                                       "message": "hours 需满足 0 < hours <= 8760"})
            return
        anchor_text = db_max_time()
        if anchor_text is None:
            _json_response(self, 404, {"status": "error", "error_type": "no_data",
                                       "message": "数据库为空,请先执行 py b02_memory.py generate"})
            return
        anchor = parse_dt(anchor_text)
        records = query_range(anchor - timedelta(hours=hours), anchor)
        _json_response(self, 200, {"status": "ok", "anchor": anchor_text,
                                   "count": len(records), "records": records})

    def _api_history_range(self, params):
        """GET /api/history_range?from=&to=:任意时间窗口(闭区间),服务端严格校验。"""
        raw_from, raw_to = _get_param(params, "from"), _get_param(params, "to")
        if not raw_from or not raw_to:
            _json_response(self, 400, {"status": "error", "error_type": "bad_param",
                                       "message": "缺少 from/to 参数"})
            return
        try:
            start = parse_dt_web(raw_from)
            end = parse_dt_web(raw_to)
        except ValueError as e:
            _json_response(self, 400, {"status": "error", "error_type": "bad_param",
                                       "message": str(e)})
            return
        if start >= end:
            _json_response(self, 400, {"status": "error", "error_type": "bad_param",
                                       "message": "from 必须早于 to"})
            return
        records = query_range(start, end)
        _json_response(self, 200, {"status": "ok", "anchor": fmt_dt(end),
                                   "count": len(records), "records": records})

    def _api_stats(self, params):
        """GET /api/stats[?hours=N | ?from=&to=]:窗口统计;无参数 = 全窗口。"""
        raw_hours = _get_param(params, "hours")
        raw_from, raw_to = _get_param(params, "from"), _get_param(params, "to")
        if raw_hours is not None:
            try:
                hours = float(raw_hours)
            except ValueError:
                _json_response(self, 400, {"status": "error", "error_type": "bad_param",
                                           "message": "hours 必须是数字"})
                return
            if not 0 < hours <= 8760:
                _json_response(self, 400, {"status": "error", "error_type": "bad_param",
                                           "message": "hours 需满足 0 < hours <= 8760"})
                return
            anchor_text = db_max_time()
            if anchor_text is None:
                _json_response(self, 404, {"status": "error", "error_type": "no_data",
                                           "message": "数据库为空,请先执行 py b02_memory.py generate"})
                return
            anchor = parse_dt(anchor_text)
            start, end = anchor - timedelta(hours=hours), anchor
        elif raw_from is not None or raw_to is not None:
            if not raw_from or not raw_to:
                _json_response(self, 400, {"status": "error", "error_type": "bad_param",
                                           "message": "from 与 to 必须成对出现"})
                return
            try:
                start = parse_dt_web(raw_from)
                end = parse_dt_web(raw_to)
            except ValueError as e:
                _json_response(self, 400, {"status": "error", "error_type": "bad_param",
                                           "message": str(e)})
                return
            if start >= end:
                _json_response(self, 400, {"status": "error", "error_type": "bad_param",
                                           "message": "from 必须早于 to"})
                return
        else:
            window = full_window()
            if window == (None, None):
                _json_response(self, 404, {"status": "error", "error_type": "no_data",
                                           "message": "数据库为空,请先执行 py b02_memory.py generate"})
                return
            start, end = window
        _json_response(self, 200, {"status": "ok", **stats_window(start, end)})


def run_web_server(port=None):
    """启动 B02 Web 时间序列可视化服务(127.0.0.1,端口 B02_WEB_PORT/--port 覆盖)。"""
    setup_console()
    listen_port = port or B02_WEB_PORT
    print("================ GreenMate B02 数据记忆模块 — Web 可视化 ================")
    print("页面地址: http://%s:%d" % (B02_WEB_HOST, listen_port))
    print("数据来源: %s (SQLite,重启后历史数据保留)" % DB_PATH.relative_to(BASE_DIR))
    print("能力: 当前状态 / 时间范围筛选(预设 + 自定义)/ 时间窗口统计 / 趋势线 / 质量检查 API")
    print("说明: sim_scenario 仅为模拟器元数据,页面不展示,不作为标签或特征")
    print("按 Ctrl+C 停止服务。")
    try:
        server = ThreadingHTTPServer((B02_WEB_HOST, listen_port), B02RequestHandler)
        server.daemon_threads = True
    except OSError as e:
        print("⚠ 端口 %d 启动失败: %s。请设置 B02_WEB_PORT 环境变量或用 --port 换一个端口。" % (listen_port, e))
        sys.exit(1)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
    finally:
        server.server_close()
        sys.exit(0)


# ---------------- 程序入口 ----------------
def main():
    """B02 入口:子命令分发。退出码恒 0(检查清单语义;Web 端口失败除外)。"""
    setup_console()
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print_usage()
        return
    cmd, rest = args[0], args[1:]
    if cmd == "generate":
        cmd_generate(rest)
        return
    if cmd == "current":
        cmd_current()
        return
    if cmd == "history":
        cmd_history(rest)
        return
    if cmd == "stats":
        cmd_stats(rest)
        return
    if cmd == "check":
        cmd_check()
        return
    if cmd == "web":
        port = None
        if "--port" in rest:
            i = rest.index("--port")
            if i + 1 >= len(rest):
                print("⚠ --port 需要参数,如 --port 8223")
                return
            try:
                port = int(rest[i + 1])
            except ValueError:
                print("⚠ 端口必须是整数")
                return
        elif any(a.startswith("--port=") for a in rest):
            try:
                port = int([a for a in rest if a.startswith("--port=")][0].split("=", 1)[1])
            except ValueError:
                print("⚠ 端口必须是整数")
                return
        run_web_server(port)
        return
    print("未知参数: %s" % " ".join(args))
    print_usage()


if __name__ == "__main__":
    main()
