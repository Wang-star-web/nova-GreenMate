#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
=====================================================================
 GreenMate 门户素材爬虫 — tools/portal_assets.py(简单爬虫,零密钥零外网)
=====================================================================

【作用】
  为整合门户(8300)汇集图片素材:巡检三服务状态、扫描项目内已有截图、
  生成 前端资源/portal_assets/ 图库与 manifest.json;门户首页自动读取该
  manifest 渲染「📷 系统截图」图库,并把 b01_web.png / b02_web.png 用作
  在线服务大图卡片。

【运行方式】
  py tools/portal_assets.py status     # 巡检三服务(门户 8300 / B01 8123 / B02 8223)HTTP 可达性
  py tools/portal_assets.py collect    # 汇集截图 → 前端资源/portal_assets/(含 manifest.json)

【截图来源】
  · tools/shot_services.py(Playwright + 本机 Edge)全自动截取三张整页截图
    放入 inbox/;本脚本 collect 时优先采纳 inbox 内按约定名命名的截图
    (b01_web.png / b02_web.png / portal_home.png)。
  · 其余项目内证据截图自动汇入图库,来源如实写入 manifest。

【说明】
  仅访问 localhost;无密钥、无外网请求;目录缺失自动创建;任何单项失败
  只跳过并提示,程序不崩溃。
=====================================================================
"""

import json
import shutil
import sys
import urllib.request
from pathlib import Path

# ---------------- 配置 ----------------
BASE_DIR = Path(__file__).resolve().parent.parent   # 项目根目录
ASSETS_DIR = BASE_DIR / "前端资源"
OUT_DIR = ASSETS_DIR / "portal_assets"
INBOX_DIR = OUT_DIR / "inbox"
GALLERY_DIR = OUT_DIR / "gallery"
MANIFEST = OUT_DIR / "manifest.json"

# 门户服务卡与视频素材的固定文件名约定(WorkBuddy 截图与收集结果共用)
FIXED_NAMES = {
    "b01_web.png":     ("B01 智能采集 Web(8123)",  "在线服务大图卡片"),
    "b02_web.png":     ("B02 数据记忆可视化(8223)", "在线服务大图卡片"),
    "portal_home.png": ("整合门户首页(8300)",      "在线服务大图卡片 / LiBTV 素材"),
}

# 三个本地服务(host/port 与各模块环境变量默认值一致)
SERVICES = [("门户", 8300), ("B01", 8123), ("B02", 8223)]

# 图库扫描目录(相对项目根;只取 PNG,源路径如实写入 manifest 的 source 字段)
SCAN_DIRS = [
    BASE_DIR / "证据" / "B01",
    BASE_DIR / "证据" / "b02",
    BASE_DIR / "证据" / "b03",
    BASE_DIR / "证据" / "b04",
    BASE_DIR / "证据" / "b05",
    BASE_DIR / "证据" / "b06",
    BASE_DIR / "证据" / "b07",
    BASE_DIR / "文档" / "screenshots",
]

# 在线服务大图卡片的默认截图来源(按文件名精确挑选;不存在则留空由 WorkBuddy 补拍)
FIXED_PICKS = {
    "b01_web.png":     [BASE_DIR / "证据" / "B01" / "web网页界面.png"],
    "b02_web.png":     [BASE_DIR / "证据" / "b02" / "png" / "Web可视化时序图表.png"],
    "portal_home.png": [],
}


def setup_console():
    """强制标准输出为 UTF-8(GBK 管道打印中文会崩溃,与各模块同款处理)。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def http_ok(url, timeout=2):
    """HTTP GET 探测服务可达性(爬虫巡检),返回 (ok, 状态描述)。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return True, "HTTP %d" % resp.status
    except Exception as e:
        return False, str(e)[:60]


def cmd_status():
    """巡检三服务:逐项打印可达性;全部不可达也不崩溃。"""
    print("================ 三服务巡检 ================")
    for name, port in SERVICES:
        ok, detail = http_ok("http://127.0.0.1:%d/" % port)
        mark = "✓" if ok else "✗"
        print("  %s %s(127.0.0.1:%d)%s" % (mark, name, port, "" if ok else "  不可达: %s" % detail))
    print("说明: 先运行 py b09_portal.py serve 可一键同起三服务。")


def _safe_name(text, fallback, ext=".png"):
    """清洗标题为安全文件名(仅保留中文/字母/数字/下划线/连字符)。"""
    keep = []
    for ch in str(text):
        if ch.isalnum() or ch in "_-" or "\u4e00" <= ch <= "\u9fff":
            keep.append(ch)
    name = "".join(keep).strip("._-") or fallback
    return name[:40] + ext


def _thumbnail(src, dst, max_w=800):
    """用 pillow 生成缩略图(未装 pillow 时直接复制原图)。"""
    try:
        from PIL import Image  # pillow: requirements 外可选依赖,缺失时降级为原图复制
        with Image.open(src) as im:
            if im.width > max_w:
                h = round(im.height * max_w / im.width)
                im = im.convert("RGB").resize((max_w, h), Image.LANCZOS)
            im.save(dst)
        return True
    except Exception:
        try:
            shutil.copyfile(src, dst)
            return True
        except OSError:
            return False


def _scan_pngs(dirs):
    """扫描目录树中的 PNG,按路径排序返回稳定顺序。"""
    found = []
    for d in dirs:
        if not d.is_dir():
            continue
        found.extend(sorted(d.rglob("*.png")))
    return sorted(found)


def cmd_collect():
    """汇集截图:固定名图片(inbox 采纳 + 证据目录挑选)→ 图库(缩略图)→ manifest.json。"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    GALLERY_DIR.mkdir(parents=True, exist_ok=True)
    items, adopted, used_srcs = [], 0, set()

    # ① 固定名图片:优先采纳 inbox 自动截图(shot_services.py),其次按 FIXED_PICKS 挑选
    for fname, (title, caption) in FIXED_NAMES.items():
        src = None
        if (INBOX_DIR / fname).is_file():
            src = INBOX_DIR / fname
            source = "自动截图(Playwright)"
            adopted += 1
        else:
            for cand in FIXED_PICKS.get(fname, []):
                if cand.is_file():
                    src, source = cand, "项目证据截图: %s" % cand.relative_to(BASE_DIR)
                    break
        if src is None:
            print("  - %s: 暂无素材(可用 WorkBuddy 补拍后放入 inbox/)" % fname)
            continue
        if _thumbnail(src, OUT_DIR / fname):
            items.append({"file": fname, "title": title, "caption": caption, "source": source})
            used_srcs.add(src)
            print("  ✓ %s  ← %s" % (fname, source))

    # ② inbox 里其余 PNG(超出固定名约定)也并入图库
    for src in sorted(INBOX_DIR.rglob("*.png")):
        if src.name in FIXED_NAMES:
            continue
        dst = GALLERY_DIR / _safe_name(src.stem, "auto-shot")
        if _thumbnail(src, dst):
            items.append({"file": "gallery/%s" % dst.name, "title": src.stem,
                          "caption": "自动截图", "source": "自动截图(Playwright)"})
            print("  ✓ 图库并入: %s" % src.name)

    # ③ 扫描项目内证据截图汇入图库(排除已用作固定名卡片的源文件,避免重复)
    for src in _scan_pngs(SCAN_DIRS):
        if src in used_srcs or not src.is_file():
            continue
        dst = GALLERY_DIR / _safe_name(src.stem, "img")
        if not dst.exists() and not _thumbnail(src, dst):
            print("  - 图库跳过(生成失败): %s" % src.name)
            continue
        items.append({"file": "gallery/%s" % dst.name, "title": src.stem,
                      "caption": "项目截图", "source": src.relative_to(BASE_DIR).as_posix()})

    # ④ manifest.json(UTF-8,不依赖生成时间——重跑结果确定)
    MANIFEST.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n✓ manifest 已生成: %s(共 %d 项,采纳自动截图 %d 张)" % (MANIFEST, len(items), adopted))
    print("  门户页面的「📷 系统截图」图库与服务大图卡片将自动读取这些素材。")


def main():
    setup_console()
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print("用法:")
        print("  py tools/portal_assets.py status    # 巡检三服务(门户 8300 / B01 8123 / B02 8223)")
        print("  py tools/portal_assets.py collect   # 汇集截图 → 前端资源/portal_assets/(含 manifest.json)")
        print("WorkBuddy 补拍提示词见本文件 docstring 顶部。")
        return
    if args[0] == "status":
        cmd_status()
    elif args[0] == "collect":
        cmd_collect()
    else:
        print("未知子命令: %s" % args[0])


if __name__ == "__main__":
    main()
