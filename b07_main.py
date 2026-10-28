#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 GreenMate B07 — 主程序组装模块(统一入口流水线)
=====================================================================

【任务目标】
  把 B01~B06 六个模块串联成完整流水线,统一入口一键跑完:
  采集输入 → 原始数据存储 → 解析提取证据 → 专家规则判断 →
  ML异常检测 → 证据融合可信降级 → 生成HTML多视图报告。
  整条流程全程使用同一个 run_id;所有证据自动保存到对应的
  evidence 子文件夹,报告自动输出到 report 文件夹。

【运行方式】
  py b07_main.py                    # 完整流水线(先交互采集,再自动跑 B02~B06)
  py b07_main.py run_xxx            # 对已有 run_id 从解析阶段开始重跑(幂等)
  py b07_main.py --help             # 查看用法说明

【降级策略】
  阶段 2~6 任一阶段失败(如首次运行历史数据不足、规则库缺失)时打印
  警告并继续后续阶段;B05 会自然地将缺失证据标注【证据缺失】并降级
  可信度,B06 在对应视图渲染占位,整条流水线仍完整走通并产出报告。
  阶段 1 取消或保存失败则终止流水线。
=====================================================================
"""

import json
import sys
from pathlib import Path

# ---------------- 导入六个模块的公开函数(不调任何模块的 main()) ----------------
import b01_cli
import b02_parse
import b03_rule
import b04_ml
import b05_fusion
import b06_report

# ---------------- 配置 ----------------
BASE_DIR = Path(__file__).resolve().parent   # 脚本所在目录,数据路径不受启动位置影响
RAW_DIR = b01_cli.RAW_DIR                    # 复用 B01 的原始数据目录
REPORT_DIR = BASE_DIR / "report"             # B06 报告目录

# 六个阶段的产物路径模板(用于流水线总结的存在性检查)
PRODUCTS = [
    ("原始数据", RAW_DIR / "{run_id}.json"),
    ("解析证据", BASE_DIR / "evidence" / "parsed" / "{run_id}_parsed.json"),
    ("规则证据", BASE_DIR / "evidence" / "rule" / "{run_id}_rule.json"),
    ("ML证据", BASE_DIR / "evidence" / "ml" / "{run_id}_ml.json"),
    ("融合结论", BASE_DIR / "evidence" / "final" / "{run_id}_final.json"),
    ("HTML报告", REPORT_DIR / "{run_id}_report.html"),
]


def print_banner():
    """打印主程序横幅。"""
    print("===== GreenMate B07 — 主程序组装模块(统一入口流水线) =====")
    print("流程: 采集输入 → 原始数据存储 → 解析提取证据 → 专家规则判断")
    print("      → ML异常检测 → 证据融合可信降级 → 生成HTML多视图报告")
    print("全程使用同一个 run_id;任一阶段失败按降级策略继续。")
    print()


def print_stage_header(n, name):
    """打印阶段标题行。"""
    print("\n---------------- [%d/6] %s ----------------" % (n, name))


def run_stage(n, name, func):
    """执行一个阶段: 打标题 → 调零参函数;失败打印降级警告并返回 False。"""
    print_stage_header(n, name)
    ok = func()
    if not ok:
        print("⚠ 阶段 %d/6 「%s」未成功,按降级策略继续后续阶段。" % (n, name))
    return ok


def stage_rule(run_id):
    """阶段 3: 加载规则库并执行专家规则判断(load_rules 失败时自行打印原因)。"""
    rules = b03_rule.load_rules()
    return rules is not None and b03_rule.judge_run(run_id, rules)


def pipeline_summary(run_id, statuses):
    """打印流水线总结: 阶段状态表 + 最终结论 + 产物清单(按文件存在性判断)。"""
    print("\n================ 流水线总结 ================")
    print("run_id: %s" % run_id)
    for name, state in statuses:
        print("  [%s] %s" % (state, name))
    # 最终结论(防御性加载: 任何读取问题都不崩溃)
    final_path = BASE_DIR / "evidence" / "final" / ("%s_final.json" % run_id)
    try:
        if final_path.exists():
            with open(final_path, encoding="utf-8") as f:
                final = json.load(f)
            print("最终结论: %s (可信度: %s)"
                  % (final.get("final_conclusion", "—"), final.get("trust_level", "—")))
        else:
            print("最终结论: 未生成")
    except (OSError, json.JSONDecodeError):
        print("最终结论: 读取失败")
    print("产物清单:")
    for label, template in PRODUCTS:
        path = Path(str(template).replace("{run_id}", run_id))
        mark = "✓" if path.exists() else "缺失"
        print("  %s %s" % (mark, path.relative_to(BASE_DIR).as_posix()))
    print("提示: 可在浏览器打开 report/%s_report.html 查看多视图报告。" % run_id)


def print_usage():
    """打印用法说明。"""
    print("用法:")
    print("  py b07_main.py           # 完整流水线(交互采集后自动执行 B02~B06)")
    print("  py b07_main.py run_xxx   # 对已有 run_id 从解析阶段开始重跑(跳过采集)")
    print("  py b07_main.py --help    # 查看本说明")


def run_pipeline(run_id=None):
    """执行流水线: run_id 为 None 时从阶段 1 采集开始,否则跳过阶段 1。
    返回进程退出码(0 正常/取消/降级完成;1 阶段 1 保存失败)。"""
    statuses = []

    if run_id is None:
        # ---- 阶段 1: 采集输入 + 原始数据存储 ----
        print_stage_header(1, "采集输入")
        record = b01_cli.collect_one_run()
        if record is None:
            print("\n已取消本次采集,流水线终止(未保存任何数据)。")
            return 0
        run_id = record["run_id"]  # run_id 仅在此生成一次,后续阶段全部沿用
        if not b01_cli.save_record(record):
            print("\n原始数据保存失败,流水线终止。")
            return 1
        statuses.append(("1/6 采集输入", "✓"))
    else:
        print("使用已有 run_id: %s (跳过阶段 1)" % run_id)
        statuses.append(("1/6 采集输入", "跳过"))

    # ---- 阶段 2~6: 依次执行,任一失败降级继续 ----
    stages = [
        (2, "解析提取证据", lambda: b02_parse.parse_run(run_id)),
        (3, "专家规则判断", lambda: stage_rule(run_id)),
        (4, "ML异常检测", lambda: b04_ml.predict_run(run_id)),
        (5, "证据融合可信降级", lambda: b05_fusion.fuse_run(run_id)),
        (6, "生成HTML报告", lambda: b06_report.generate_run(run_id)),
    ]
    for n, name, func in stages:
        ok = run_stage(n, name, func)
        statuses.append(("%d/6 %s" % (n, name), "✓" if ok else "⚠"))

    pipeline_summary(run_id, statuses)
    return 0


def main():
    """统一入口: 参数分发 + 外层安全兜底(只捕获 Ctrl+C/EOF,不掩盖真实错误)。"""
    b01_cli.setup_console()  # 导入模块的 main 不会替 b07 配置控制台,必须自行调用
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        print_usage()
        return 0
    if len(args) > 1:
        print("参数无效,最多接受 1 个 run_id。")
        print_usage()
        return 0
    run_id = None
    if args:
        arg = args[0]
        run_id = arg[:-5] if arg.lower().endswith(".json") else arg
        if not b03_rule.is_valid_run_id(run_id):
            print("run_id 格式不正确: %s (应为 run_YYYYmmdd_HHMMSS_<6位hex>)" % arg)
            print_usage()
            return 0
        if not (RAW_DIR / ("%s.json" % run_id)).exists():
            print("未找到原始数据文件: evidence/raw/%s.json,无法执行流水线。" % run_id)
            return 0
    print_banner()
    try:
        return run_pipeline(run_id)
    except KeyboardInterrupt:
        print("\n检测到 Ctrl+C,流水线已终止。")
        return 0
    except EOFError:
        print("\n输入已结束 (EOF),流水线终止。")
        return 0


if __name__ == "__main__":
    sys.exit(main())
