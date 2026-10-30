#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 GreenMate B08 — 契约校验脚本(run_result.json 五项校验 + sha256 稳定性)
=====================================================================

【任务目标】
  校验 B07 产出的 run_result.json 是否符合契约:逐项检查
  ① raw 原始数据 ② parsed 解析后数据 ③ rule 规则输出 ④ ml 模型输出
  ⑤ final 最终结论;输出校验结果报告与该文件的 sha256 哈希值
  (稳定性校验:重复运行同一文件,哈希与清单一致即可确认结果稳定可复现)。

【运行方式】
  python b08_verify.py --json-path 证据/b07/<run_id>/run_result.json
  python b08_verify.py --json-path=<路径>        # 等号形态等价
  python b08_verify.py                           # 不带参数:打印用法并列出可用文件
  python b08_verify.py --help                    # 查看用法说明

【校验规则】
  五项逐一检查,四态结论:
  ✓ OK         存在且内容符合契约
  ◌ 本轮预期缺失  rule/ml/final 键不存在 / null / 空对象——按行为约定
                 标记为「本轮预期缺失」,不判定为错误(本轮 B07 未生成
                 这三类模块输出,显式 null 占位)
  ✗ 缺失        raw / parsed 不存在或为 null(不在豁免名单,必须存在)
  ⚠ 异常        存在但内容无效(形状错误 / 声明与内容矛盾 / run_id 不一致)
  另输出 run_result.json 全文 sha256(64 hex + 短 12)与确定性「校验指纹」
  (不含校验时间),两者共同构成稳定性锚点。脚本只读,不写任何文件;
  校验清单结论不等于脚本错误,退出码恒为 0。
=====================================================================
"""

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

# ---------------- 配置 ----------------
BASE_DIR = Path(__file__).resolve().parent
EVIDENCE_B07_DIR = BASE_DIR / "证据" / "b07"

PARAM_KEYS = ("temperature", "humidity", "co2", "light")
# 预期缺失豁免名单:(键名, 清单显示名)——本轮 B07 未生成这三类模块输出
OPTIONAL_SECTIONS = (("rule", "规则输出"), ("ml", "模型输出"), ("final", "最终结论"))
GRID_TIME_FMT = "%Y-%m-%d %H:%M:%S"   # B02 网格样本时间格式(本地,无时区)

# 四态标记(控制台可能字体不支持 ◌ 时整体替换为 ○ 即可)
MARKS = {"ok": "✓ OK", "expected_missing": "◌ 本轮预期缺失", "missing": "✗ 缺失", "bad": "⚠ 异常"}


def setup_console():
    """强制标准输出为 UTF-8:输出被管道/重定向时避免 Windows 默认 GBK 导致乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def parse_args(argv):
    """解析命令行:支持 --json-path x 与 --json-path=x(重复取最后一次)。
    返回 {help, path, error}。"""
    result = {"help": False, "path": None, "error": None}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            result["help"] = True
        elif arg == "--json-path":
            if i + 1 >= len(argv):
                result["error"] = "参数 --json-path 缺少路径值"
                return result
            result["path"] = argv[i + 1]
            i += 1
        elif arg.startswith("--json-path="):
            value = arg[len("--json-path="):]
            if not value:
                result["error"] = "参数 --json-path= 缺少路径值"
                return result
            result["path"] = value
        elif arg.startswith("-"):
            result["error"] = "未知参数: %s(仅支持 --json-path,详见 --help)" % arg
            return result
        else:
            result["error"] = ("不接受位置参数: %s(请使用 --json-path <run_result.json文件路径>,"
                               "详见 --help)" % arg)
            return result
        i += 1
    return result


def load_json(path_text):
    """读文件:先读 bytes(供 sha256)再解析。返回 (obj|None, bytes|None, err|None)。"""
    path = Path(path_text)
    if not path.exists():
        return None, None, "文件不存在: %s" % path
    if path.is_dir():
        return None, None, "该路径是目录,请指定 run_result.json 文件: %s" % path
    try:
        data = path.read_bytes()
    except OSError as e:
        return None, None, "文件不可读 %s: %s" % (path, e)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, None, "不是 UTF-8 文本(无法解析 JSON): %s" % path
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        return None, None, "JSON 损坏: %s" % e
    if not isinstance(obj, dict):
        return None, None, "JSON 顶层不是对象(应为 run_result.json 契约结构): %s" % path
    return obj, data, None


# ---------------- 基础校验工具 ----------------
def parse_time_iso(text):
    """raw.collect_time:带时区 ISO(datetime.fromisoformat)。失败返回 None。"""
    try:
        return datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None


def parse_time_grid(text):
    """parsed.*.sample_time:B02 本地网格串(无时区)。失败返回 None。"""
    try:
        return datetime.strptime(str(text), GRID_TIME_FMT)
    except (TypeError, ValueError):
        return None


def is_number(value):
    """数字且非 bool(True/False 不算数值)。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _sha_digest(obj):
    """单节内容的确定性摘要(JSON 规范化序列化后 sha256 短 12;None → "-")。"""
    if obj is None:
        return "-"
    try:
        text = json.dumps(obj, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return "-"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


# ---------------- 五项校验 ----------------
def check_raw(raw, top_run_id):
    """① raw 原始数据校验。raw 不在豁免名单:缺失 → missing,形状错误 → bad。"""
    if raw is None:
        return "missing", "raw 键不存在或为 null(raw 原始数据不在豁免名单,契约要求必须存在)", "-"
    if not isinstance(raw, dict):
        return "bad", "raw 不是对象(应为 {status, path, record})", _sha_digest(raw)
    notes = []

    status = raw.get("status")
    if status not in ("ok", "missing"):
        return "bad", "raw.status 非法(应为 ok 或 missing,实际 %s)" % status, _sha_digest(raw)
    record = raw.get("record")
    if record is None:
        if status == "missing":
            return "missing", "raw.record 为 null 且声明 status=missing(B07 锚点缺失或保存失败)", _sha_digest(raw)
        return "bad", "声明矛盾: raw.status=ok 但 raw.record 为 null", _sha_digest(raw)
    if status == "missing":
        return "bad", "声明矛盾: raw.status=missing 但 raw.record 有内容", _sha_digest(raw)
    if not isinstance(record, dict):
        return "bad", "raw.record 不是对象", _sha_digest(raw)

    # record.run_id 与顶层一致(顶层 run_id 缺失时跳过一致性比对并说明)
    rec_run_id = record.get("run_id")
    if not isinstance(rec_run_id, str) or not rec_run_id:
        return "bad", "raw.record.run_id 缺失或非字符串", _sha_digest(raw)
    if top_run_id:
        if rec_run_id != top_run_id:
            return "bad", "raw.record.run_id(%s)与顶层 run_id(%s)不一致" % (rec_run_id, top_run_id), _sha_digest(raw)
    else:
        notes.append("顶层 run_id 缺失,跳过一致性比对")

    if parse_time_iso(record.get("collect_time")) is None:
        return "bad", "raw.record.collect_time 不可解析(需带时区 ISO): %s" % record.get("collect_time"), _sha_digest(raw)
    if not isinstance(record.get("source_type"), str) or not record["source_type"]:
        return "bad", "raw.record.source_type 缺失或非字符串", _sha_digest(raw)

    data = record.get("data")
    if not isinstance(data, dict):
        return "bad", "raw.record.data 不是对象", _sha_digest(raw)
    for key in PARAM_KEYS:
        item = data.get(key)
        if not isinstance(item, dict):
            return "bad", "raw.record.data 缺少参数 %s 或该项不是对象" % key, _sha_digest(raw)
        if not is_number(item.get("value")):
            return "bad", "raw.record.data.%s.value 不是数字(实际 %s)" % (key, item.get("value")), _sha_digest(raw)
        if not isinstance(item.get("unit"), str) or not item["unit"]:
            return "bad", "raw.record.data.%s.unit 缺失或非字符串" % key, _sha_digest(raw)

    ranges = record.get("valid_ranges")
    if not isinstance(ranges, dict):
        return "bad", "raw.record.valid_ranges 不是对象", _sha_digest(raw)
    for key in PARAM_KEYS:
        spec = ranges.get(key)
        if not isinstance(spec, dict) or not is_number(spec.get("min")) or not is_number(spec.get("max")):
            return "bad", "raw.record.valid_ranges.%s 缺失或 min/max 非数字" % key, _sha_digest(raw)

    path = raw.get("path")
    if not isinstance(path, str) or not path:
        return "bad", "raw.path 缺失或非字符串", _sha_digest(raw)
    if top_run_id and path != "证据/raw/%s.json" % top_run_id:
        notes.append("raw.path(%s)与约定路径 证据/raw/%s.json 不一致(仅提示)" % (path, top_run_id))

    # 附注(不计状态):盘上 raw 文件存在性 + 与嵌入 record 一致性
    disk_path = BASE_DIR / path if not Path(path).is_absolute() else Path(path)
    if disk_path.exists():
        try:
            disk_record = json.loads(disk_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            disk_record = None
        if disk_record == record:
            notes.append("盘上 %s 存在且与嵌入 record 逐字节内容一致" % path)
        else:
            notes.append("盘上 %s 存在但与嵌入 record 内容不一致(仅提示)" % path)
    else:
        notes.append("盘上 %s 不存在(内容以嵌入 record 为准,仅提示)" % path)

    detail = ";".join(notes) if notes else "raw 原始数据形状与声明均符合契约"
    return "ok", detail, _sha_digest(raw)


def check_parsed(parsed):
    """② parsed 解析后数据校验。parsed 不在豁免名单:缺失 → missing,形状错误 → bad。"""
    if parsed is None:
        return "missing", "parsed 键不存在或为 null(解析后数据不在豁免名单,契约要求必须存在)", "-"
    if not isinstance(parsed, dict):
        return "bad", "parsed 不是对象(应为 {status, source, anchor, history_count, history, grid_step_minutes})", _sha_digest(parsed)

    status = parsed.get("status")
    if status not in ("ok", "missing"):
        return "bad", "parsed.status 非法(应为 ok 或 missing,实际 %s)" % status, _sha_digest(parsed)
    anchor = parsed.get("anchor")
    history = parsed.get("history")
    notes = []

    if status == "missing":
        if anchor is None and not history:
            return "missing", "parsed 声明 status=missing 且内容为空(锚点缺失;parsed 不在豁免名单)", _sha_digest(parsed)
        return "bad", "声明矛盾: parsed.status=missing 但 anchor/history 有数据", _sha_digest(parsed)

    # status == "ok"
    if anchor is None or not history:
        return "bad", "声明矛盾: parsed.status=ok 但 anchor 为 null 或 history 为空(锚点存在时 history 必非空)", _sha_digest(parsed)
    if not isinstance(anchor, dict):
        return "bad", "parsed.anchor 不是对象", _sha_digest(parsed)
    if parse_time_grid(anchor.get("sample_time")) is None:
        return "bad", "parsed.anchor.sample_time 不可解析(需 %s 格式): %s" % (GRID_TIME_FMT, anchor.get("sample_time")), _sha_digest(parsed)
    if not isinstance(anchor.get("source_type"), str) or not anchor["source_type"]:
        return "bad", "parsed.anchor.source_type 缺失或非字符串", _sha_digest(parsed)
    for key in PARAM_KEYS:
        if not is_number(anchor.get(key)):
            return "bad", "parsed.anchor 缺少数值参数 %s(实际 %s)" % (key, anchor.get(key)), _sha_digest(parsed)

    if not isinstance(history, list):
        return "bad", "parsed.history 不是数组", _sha_digest(parsed)
    count = parsed.get("history_count")
    if not isinstance(count, int) or isinstance(count, bool) or count != len(history):
        return "bad", "parsed.history_count(%s)与实际行数(%d)不一致" % (count, len(history)), _sha_digest(parsed)
    if not history:
        return "bad", "声明矛盾: parsed.status=ok 但 history 为空", _sha_digest(parsed)

    # 逐行形状 + 升序
    prev_dt = None
    for idx, row in enumerate(history):
        if not isinstance(row, dict):
            return "bad", "parsed.history[%d] 不是对象" % idx, _sha_digest(parsed)
        dt = parse_time_grid(row.get("sample_time"))
        if dt is None:
            return "bad", "parsed.history[%d].sample_time 不可解析: %s" % (idx, row.get("sample_time")), _sha_digest(parsed)
        if not isinstance(row.get("source_type"), str) or not row["source_type"]:
            return "bad", "parsed.history[%d].source_type 缺失或非字符串" % idx, _sha_digest(parsed)
        for key in PARAM_KEYS:
            if not is_number(row.get(key)):
                return "bad", "parsed.history[%d] 缺少数值参数 %s" % (idx, key), _sha_digest(parsed)
        if prev_dt is not None and dt <= prev_dt:
            return "bad", "parsed.history 未按 sample_time 严格升序(第 %d 行)" % idx, _sha_digest(parsed)
        prev_dt = dt

    # 末行 == 锚点行(query_range 闭区间含锚点;锚点缺失时该比对已在上方声明检查中覆盖)
    if history[-1] != anchor:
        return "bad", "parsed.history 末行与 anchor 行不一致", _sha_digest(parsed)

    # 网格步长:与 grid_step_minutes 一致性仅注记,不判错(信息字段)
    step = parsed.get("grid_step_minutes")
    if isinstance(step, int) and not isinstance(step, bool) and len(history) >= 2:
        first, second = history[0], history[1]
        diff = (parse_time_grid(second["sample_time"]) - parse_time_grid(first["sample_time"])).total_seconds()
        if diff != step * 60.0:
            notes.append("相邻样本间隔 %.0f 秒与 grid_step_minutes=%s 不一致(仅提示)" % (diff, step))
        else:
            notes.append("%s 分钟网格一致" % step)
    if not isinstance(parsed.get("source"), str) or not parsed["source"]:
        notes.append("parsed.source 缺失(信息字段,仅提示)")

    detail = ";".join(notes) if notes else "parsed 形状与声明均符合契约(history %d 行,末行==锚点)" % len(history)
    return "ok", detail, _sha_digest(parsed)


def check_optional(section, label, top_run_id):
    """③④⑤ rule / ml / final 校验。键不存在 / null / 空对象 → 本轮预期缺失
    (不判定为错误);非空 dict → 宽松通过(结构校验待后续轮次细化);其余 → bad。"""
    if section is None:
        return "expected_missing", "null 占位(本轮 B07 未生成%s)" % label, "-"
    if isinstance(section, dict) and not section:
        return "expected_missing", "空对象占位(本轮 B07 未生成%s)" % label, "-"
    if section == "":
        return "expected_missing", "空字符串占位(本轮 B07 未生成%s)" % label, "-"
    if not isinstance(section, dict):
        return "bad", "%s 非对象形状(实际类型 %s)" % (label, type(section).__name__), _sha_digest(section)
    # 非空 dict:宽松通过;若含 run_id 且与顶层不一致 → bad
    if top_run_id and section.get("run_id") is not None and section.get("run_id") != top_run_id:
        return "bad", "%s.run_id 与顶层 run_id 不一致" % label, _sha_digest(section)
    return "ok", "宽松通过: 该节已生成,结构校验待后续轮次细化", _sha_digest(section)


# ---------------- 清单组装 / 指纹 / 报告 ----------------
def run_checks(obj):
    """对已解析的 run_result.json 对象执行五项校验,返回 (rows, header_notes)。"""
    top_run_id = obj.get("run_id")
    header_notes = []
    if not isinstance(top_run_id, str) or not top_run_id:
        header_notes.append("⚠ 顶层 run_id 缺失或非字符串(raw 一致性比对已跳过)")
    if not isinstance(obj.get("scenario_id"), str) or not obj["scenario_id"]:
        header_notes.append("⚠ 顶层 scenario_id 缺失或非字符串(仅提示,不计状态)")
    if not isinstance(obj.get("generated_at"), str) or not obj["generated_at"]:
        header_notes.append("⚠ 顶层 generated_at 缺失或非字符串(仅提示,不计状态)")

    checks = [
        ("raw", "raw 原始数据", check_raw(obj.get("raw"), top_run_id)),
        ("parsed", "parsed 解析后数据", check_parsed(obj.get("parsed"))),
    ]
    for key, label in OPTIONAL_SECTIONS:
        if key not in obj:
            checks.append((key, label, ("expected_missing", "键不存在(本轮 B07 未生成%s)" % label, "-")))
        else:
            checks.append((key, label, check_optional(obj[key], label, top_run_id)))

    rows = []
    for seq, (item, label, (status, detail, digest)) in enumerate(checks, start=1):
        rows.append({"seq": seq, "item": item, "label": label,
                     "status": status, "detail": detail, "digest": digest})
    return rows, header_notes


def fingerprint(rows, file_hash):
    """确定性校验指纹: sha256 over 行摘要 + 文件哈希;不含校验时间。"""
    h = hashlib.sha256()
    for r in rows:
        h.update(("%d|%s|%s|%s\n" % (r["seq"], r["item"], r["status"], r["digest"])).encode("utf-8"))
    h.update(("file|%s\n" % file_hash).encode("utf-8"))
    full = h.hexdigest()
    return full, full[:12]


def print_report(path_text, obj, data, rows, header_notes):
    """输出校验结果报告:头部 + sha256 + 清单 + 汇总 + 指纹 + 提示。"""
    file_hash = hashlib.sha256(data).hexdigest()
    fp_full, fp_short = fingerprint(rows, file_hash)
    print("\n================ B08 契约校验清单(run_result.json) ================")
    print("文件: %s" % path_text)
    print("run_id: %s" % (obj.get("run_id") if isinstance(obj.get("run_id"), str) else "(缺失)"))
    print("文件大小: %d 字节" % len(data))
    print("校验时间: %s (仅展示,不计入校验指纹)" % datetime.now().astimezone().isoformat(timespec="seconds"))
    for note in header_notes:
        print(note)
    print("标记图例: %s / %s / %s / %s" % tuple(MARKS.values()))
    print("文件 sha256: %s" % file_hash)
    print("短哈希: %s" % file_hash[:12])
    print("--------------------------------------------------------------")
    for r in rows:
        print("  [%d/5] %-14s %s  %s" % (r["seq"], r["label"], MARKS[r["status"]], r["detail"]))
    counts = {"ok": 0, "expected_missing": 0, "missing": 0, "bad": 0}
    for r in rows:
        counts[r["status"]] += 1
    print("汇总: %d OK / %d 本轮预期缺失 / %d 缺失 / %d 异常 (共 5 项)" % (
        counts["ok"], counts["expected_missing"], counts["missing"], counts["bad"]))
    print("校验指纹: %s (短: %s)" % (fp_full, fp_short))
    print("提示: 重复运行同一文件,sha256 与校验指纹一致即确认结果稳定可复现;"
          "本脚本只读,不写任何文件。")


def scan_available():
    """扫描 证据/b07/*/run_result.json,按 run_id 倒序返回路径列表。"""
    paths = []
    if EVIDENCE_B07_DIR.exists():
        for p in sorted(EVIDENCE_B07_DIR.glob("*/run_result.json"), reverse=True):
            paths.append(str(p))
    return paths


def print_usage():
    print("""B08 契约校验脚本 — 用法
  python b08_verify.py --json-path <run_result.json文件路径>   # 校验 B07 产出的 run_result.json
  python b08_verify.py --json-path=<路径>                       # 等号形态等价
  python b08_verify.py                                         # 不带参数:打印用法并列出可用文件
  python b08_verify.py --help                                  # 查看用法说明

校验项: ① raw 原始数据 ② parsed 解析后数据 ③ rule 规则输出 ④ ml 模型输出
        ⑤ final 最终结论
行为约定: rule / ml / final 键不存在、为 null 或空对象时标记「本轮预期缺失」,
         不判定为错误;raw / parsed 不在豁免名单,缺失即 ✗ 缺失。
输出: 校验清单(四态: ✓ OK / ◌ 本轮预期缺失 / ✗ 缺失 / ⚠ 异常)+ 文件 sha256
      哈希与确定性校验指纹(稳定性校验)。脚本只读,退出码恒 0。""")


def main():
    """程序入口:setup_console 最先;任何输入问题都转友好中文提示,绝不 traceback;
    校验清单结论不等于脚本错误,退出码恒为 0。"""
    setup_console()
    try:
        args = parse_args(sys.argv[1:])
        if args["help"]:
            print_usage()
            return
        if args["error"]:
            print("⚠ %s" % args["error"])
            print_usage()
            return
        if args["path"] is None:
            print_usage()
            paths = scan_available()
            print("\n可用的 run_result.json 文件(证据/b07/,按 run_id 倒序):")
            if not paths:
                print("  (暂无——旧 run 无 run_result.json 时需重跑 B07 生成,"
                      "如 py b07_main.py run S1)")
            for p in paths:
                print("  %s" % p)
            return

        obj, data, err = load_json(args["path"])
        if err is not None:
            print("\n✗ %s" % err)
            paths = scan_available()
            if paths:
                print("可用的 run_result.json 文件:")
                for p in paths:
                    print("  %s" % p)
            return
        rows, header_notes = run_checks(obj)
        print_report(args["path"], obj, data, rows, header_notes)
    except KeyboardInterrupt:
        print("\n检测到 Ctrl+C,程序已安全退出。")
    except Exception as e:   # 兜底:任何意外都转友好中文,绝不 traceback
        print("⚠ 程序内部错误(请检查输入文件): %s: %s" % (type(e).__name__, e))


if __name__ == "__main__":
    main()
