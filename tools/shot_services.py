#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 GreenMate 门户自动截图/录屏 — tools/shot_services.py(零密钥零账户零人工)
=====================================================================

【作用】
  用 Playwright 驱动本机 Edge(channel="msedge",免下载 Chromium)全自动完成:
  · shots: 自动启动 B01(8123)/B02(8223)服务,整页截取三张门户素材图
    → 前端资源/portal_assets/inbox/{b01_web.png, b02_web.png, portal_home.png}
    (portal_home 直接 file:// 打开门户页截取)
  · video: 自动录制 B01 页数字孪生动画约 13 秒 → 前端资源/portal_video.webm
    门户首页(b09_portal.py)检测到该文件后自动嵌入 <video> 播放。

【运行方式】
  pip install playwright        # 一次性安装(纯 Python 包,无需下载浏览器)
  py tools/shot_services.py shots
  py tools/shot_services.py video
  py tools/portal_assets.py collect   # 截图汇入门户图库(manifest.json)

【说明】
  · 全部本地操作,零密钥、零账户、零外部服务;
  · B01/B02 由本脚本 daemon 线程临时启动(已运行则复用),进程退出即停止;
  · Edge 不可用时中文提示,不崩溃;截图失败不产出半成品。
=====================================================================
"""

import shutil
import socket
import sys
import threading
import time
from pathlib import Path

# ---------------- 配置 ----------------
BASE_DIR = Path(__file__).resolve().parent.parent   # 项目根目录
sys.path.insert(0, str(BASE_DIR))                   # 便于导入 b01_cli / b02_memory
ASSETS_DIR = BASE_DIR / "前端资源"
INBOX_DIR = ASSETS_DIR / "portal_assets" / "inbox"
PORTAL_FILE = BASE_DIR / "门户页面" / "index.html"
VIDEO_PATH = ASSETS_DIR / "portal_video.webm"

B01_URL = "http://127.0.0.1:8123"
B02_URL = "http://127.0.0.1:8223"
VIEWPORT = {"width": 1280, "height": 900}
VIDEO_SECONDS = 13


def setup_console():
    """强制标准输出为 UTF-8(GBK 管道打印中文会崩溃)。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def _port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _start_service(name, module, attr, port):
    """daemon 线程起子服务(与 b09_portal.serve 同款启动方式),已运行则跳过。"""
    if _port_in_use(port):
        print("· %s 已在运行(127.0.0.1:%d),复用" % (name, port), flush=True)
        return
    print("· %s 后台启动(线程)→ http://127.0.0.1:%d" % (name, port), flush=True)
    setattr(module, attr, port)   # 函数无 port 形参:线程起前设模块属性
    threading.Thread(target=module.run_web_server, kwargs={"port": port},
                     name=name, daemon=True).start()


def _launch_edge(playwright):
    """用本机 Edge 启动浏览器(channel=msedge,免下载 Chromium)。失败给中文提示。"""
    try:
        return playwright.chromium.launch(channel="msedge", headless=True)
    except Exception as e:
        print("✗ 无法启动 Edge 浏览器: %s" % str(e)[:200])
        print("  请确认本机已安装 Microsoft Edge;或执行 py -m playwright install chromium 改用内置 Chromium。")
        return None


def _shot_page(page, url, out_path, wait_ms=2000):
    """打开 url → 等网络与动画 → 整页截图;失败返回 False。"""
    try:
        page.goto(url, wait_until="networkidle", timeout=30000)
    except Exception as e:
        print("  ✗ 打开页面失败 %s: %s" % (url, str(e)[:120]))
        return False
    page.emulate_media(reduced_motion="no-preference")   # 系统可能开了"减少动画",强制动画可播
    page.wait_for_timeout(wait_ms)
    page.screenshot(path=str(out_path), full_page=True)
    print("  ✓ %s(%d KB)" % (out_path.name, out_path.stat().st_size // 1024))
    return True


def cmd_shots():
    """三张整页截图:B01、B02(临时起服务)+ 门户(file://)。"""
    print("================ 门户素材自动截图(Playwright + Edge)================")
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    if not PORTAL_FILE.is_file():
        print("✗ 未找到门户页面: %s(先执行 py b09_portal.py)" % PORTAL_FILE)
        return 1
    import b01_cli
    import b02_memory
    _start_service("b01-web", b01_cli, "B01_WEB_PORT", 8123)
    _start_service("b02-web", b02_memory, "B02_WEB_PORT", 8223)

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = _launch_edge(p)
        if browser is None:
            return 1
        page = browser.new_page(viewport=VIEWPORT)
        print("· 截取 B01 整页…")
        _shot_page(page, B01_URL, INBOX_DIR / "b01_web.png")
        print("· 截取 B02 整页…")
        _shot_page(page, B02_URL, INBOX_DIR / "b02_web.png", wait_ms=3500)
        print("· 截取门户首页(file://)…")
        _shot_page(page, PORTAL_FILE.as_uri(), INBOX_DIR / "portal_home.png", wait_ms=1500)
        browser.close()
    print("完成: 3 张截图已就绪,运行 py tools/portal_assets.py collect 汇入门户图库。")
    return 0


def cmd_video():
    """录制数字孪生动画约 13 秒 → 前端资源/portal_video.webm。"""
    print("================ 数字孪生动画自动录制(Playwright + Edge)================")
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    import b01_cli
    _start_service("b01-web", b01_cli, "B01_WEB_PORT", 8123)

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = _launch_edge(p)
        if browser is None:
            return 1
        rec_dir = ASSETS_DIR / ".tmp_video"
        rec_dir.mkdir(parents=True, exist_ok=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 720},
            record_video_dir=str(rec_dir),
            record_video_size={"width": 1280, "height": 720},
        )
        page = context.new_page()
        print("· 录制 B01 页数字孪生动画 %d 秒…" % VIDEO_SECONDS, flush=True)
        page.goto(B01_URL, wait_until="networkidle", timeout=30000)
        page.emulate_media(reduced_motion="no-preference")
        page.wait_for_timeout(VIDEO_SECONDS * 1000)
        context.close()   # 结束录制,webm 落盘于 rec_dir
        browser.close()
    videos = sorted(rec_dir.glob("*.webm"))
    if not videos:
        print("✗ 未生成录制文件(rec_dir 为空)。")
        return 1
    shutil.copyfile(videos[-1], VIDEO_PATH)
    for v in rec_dir.glob("*"):
        v.unlink(missing_ok=True)
    print("✓ 宣传片已生成: %s(%d KB)" % (VIDEO_PATH, VIDEO_PATH.stat().st_size // 1024))
    print("  重新生成门户首页后自动嵌入播放: py b09_portal.py")
    return 0


def main():
    setup_console()
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print("用法:")
        print("  py tools/shot_services.py shots   # 自动起服务并截取三张整页截图")
        print("  py tools/shot_services.py video   # 自动录制数字孪生动画 → 前端资源/portal_video.webm")
        return 0
    if args[0] == "shots":
        return cmd_shots()
    if args[0] == "video":
        return cmd_video()
    print("未知子命令: %s" % args[0])
    return 1


if __name__ == "__main__":
    sys.exit(main())
