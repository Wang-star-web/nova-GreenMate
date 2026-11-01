#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 GreenMate 演示片生成 — tools/demo_video.py(全自动:旁白 + 字幕 + 实操录屏)
=====================================================================

【作用】
  参照传智杯获奖作品的演示片形态(旁白讲解 + 真实操作录屏 + 字幕,16:9),
  全自动生成约 2.5 分钟的系统演示片 前端资源/portal_video.mp4,门户首页
  (b09_portal.py)检测到该文件后自动嵌入播放。

  六段结构:开场门户 → B01 网页采集实操 → A/B 切换与如实降级 → B02 时序
  可视化 → 四视图报告 → 门户全景收尾。

【技术链(零密钥零账户零人工)】
  1. Playwright + 本机 Edge:每段按脚本真实操作页面并录屏(webm,1280×720);
  2. Windows SAPI TTS(Microsoft Huihui 中文女声):逐段合成旁白 wav;
  3. imageio-ffmpeg 完整版 ffmpeg:拼接分段视频、按实测时长对齐旁白、
     烧录中文字幕(ASS),H.264 + AAC 合成 mp4。

【运行方式】
  pip install playwright imageio-ffmpeg       # 一次性依赖
  py tools/demo_video.py                      # 全自动生成(约 3~4 分钟)
  py tools/demo_video.py --segments 1,2       # 只生成部分分段(调试用)

【说明】
  · 全部本地操作:仅访问 127.0.0.1 与本机文件;
  · B01 演示段会临时清空 LLM 密钥环境变量,确保"如实降级"画面稳定可复现
    (只影响本脚本进程,不触碰系统环境);
  · 任一环节失败给中文提示,不产出半成品。
=====================================================================
"""

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

# ---------------- 配置 ----------------
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
ASSETS_DIR = BASE_DIR / "前端资源"
WORK_DIR = ASSETS_DIR / ".demo_work"          # 分段素材临时目录(生成后清理)
FINAL_VIDEO = ASSETS_DIR / "portal_video.mp4"
OLD_WEBM = ASSETS_DIR / "portal_video.webm"

VIEWPORT = {"width": 1280, "height": 720}
RECORD_SIZE = {"width": 1280, "height": 720}

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:
    print("✗ 缺少 imageio-ffmpeg,请执行: py -m pip install imageio-ffmpeg")
    sys.exit(1)

TTS_VOICE = "Microsoft Huihui Desktop"

# ---------------- 分段脚本(名称/页面/旁白/动作)----------------
# 旁白按约 4.5 字/秒计,分段时长须留出余量;tail 为动作后的定格时长(秒)。
SEGMENTS = [
    {
        "name": "01_opening",
        "url": "http://127.0.0.1:8300/",
        "wait": 14.0,
        "narr": "欢迎了解 GreenMate 番茄大棚环境监测系统。它把瞬时读数,升级为可追溯、"
                "可复现的多源证据判断,一条链路,全部证据留痕。",
    },
    {
        "name": "02_collect",
        "url": "http://127.0.0.1:8123",
        "wait": 2.0,
        "narr": "网页端手动输入四项环境参数,服务端与命令行同规则校验;提交后自动生成唯一"
                " run_id,原始数据落盘,每条记录自带校验证明。",
        "actions": "collect",
        "tail": 6.0,
    },
    {
        "name": "03_abtest",
        "url": "http://127.0.0.1:8123",
        "wait": 2.0,
        "narr": "A/B 双状态一键切换,数字孪生同步联动。密钥缺失时,在线问答按设计如实降级,"
                "判断与采集功能不受影响。",
        "actions": "abtest",
        "tail": 6.0,
    },
    {
        "name": "04_series",
        "url": "http://127.0.0.1:8223",
        "wait": 3.0,
        "narr": "数据记忆模块保存七天、三十分钟网格的完整时序;窗口查询与统计即时可视化,"
                "重启后历史数据依旧保留。",
        "actions": "scroll",
        "tail": 5.0,
    },
    {
        "name": "05_report",
        "url": None,                              # 运行时取最新报告 file://
        "wait": 2.0,
        "narr": "每次判断全量存档:原始数据、指标解析、规则与机器学习证据、综合结论,"
                "四视图报告离线双击即开,判断依据一目了然。",
        "actions": "scroll",
        "tail": 5.0,
    },
    {
        "name": "06_outro",
        "url": "http://127.0.0.1:8300/",
        "wait": 3.0,
        "narr": "整合门户一键启动三个服务,数字孪生实时联动。固定输入与规则版本下,"
                "结构化判断结论与预期一致——这是 GreenMate 对可复现的承诺。",
        "actions": "scroll",
        "tail": 5.0,
    },
]


def setup_console():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def run(cmd, timeout=120, cwd=None):
    """执行命令,返回 (returncode, stdout+stderr 文本)。"""
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=cwd)
        return p.returncode, (p.stdout or b"").decode("utf-8", errors="replace") + \
                             (p.stderr or b"").decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return -1, "命令超时: %s" % " ".join(map(str, cmd))


def probe_duration(path):
    """用 ffmpeg -i 解析媒体时长(秒,float)。"""
    code, out = run([FFMPEG, "-hide_banner", "-i", str(path)])
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", out)
    if not m:
        return 0.0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


# ---------------- 旁白 TTS ----------------
def tts_wav(text, wav_path):
    """Windows SAPI 中文女声合成旁白 wav。"""
    wav_str = str(wav_path)
    ps = ('Add-Type -AssemblyName System.Speech; '
          '$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; '
          "$s.SelectVoice('%s'); $s.Rate=0; $s.SetOutputToWaveFile('%s'); "
          "$s.Speak([IO.File]::ReadAllText('%s',[Text.Encoding]::UTF8)); $s.Dispose()"
          % (TTS_VOICE, wav_str, wav_str + ".txt"))
    txt = Path(wav_str + ".txt")
    txt.write_text(text, encoding="utf-8-sig")
    code, out = run(["powershell", "-NoProfile", "-Command", ps], timeout=120)
    txt.unlink(missing_ok=True)
    if code != 0:
        print("  · TTS 输出: %s" % out[-200:])
    return code == 0 and Path(wav_str).is_file()


# ---------------- 页面动作 ----------------
def do_actions(page, kind):
    """分段内真实操作录屏:采集 / A-B 切换与降级问答 / 慢速滚动。"""
    if kind == "collect":
        vals = {"in-temperature": "25.5", "in-humidity": "60", "in-co2": "800", "in-light": "30000"}
        for i in range(4):
            page.wait_for_timeout(700)
            page.fill("#" + list(vals)[i], vals[list(vals)[i]])
        page.wait_for_timeout(800)
        page.click("#btn-collect")
        page.wait_for_timeout(3000)          # 成功回显 + 历史刷新
        page.evaluate("window.scrollBy(0, 420)")
        page.wait_for_timeout(1500)
    elif kind == "abtest":
        page.wait_for_timeout(1000)
        # 单选钮有自定义外观(视觉隐藏),用 DOM 级 click 触发 change 事件,页面 JS 同样响应
        page.evaluate("""() => { const r = document.querySelectorAll('input[name="state"]');
                               if (r.length > 1) { r[1].click(); } }""")
        page.wait_for_timeout(3000)
        page.evaluate("""() => { const b = document.getElementById('btn-ask');
                               if (b) { b.click(); } }""")   # 无密钥:降级横幅 + 错误框
        page.wait_for_timeout(4500)
    elif kind == "scroll":
        page.wait_for_timeout(1200)
        for _ in range(14):
            page.evaluate("window.scrollBy(0, 320)")
            page.wait_for_timeout(500)


def record_segment(seg, out_webm):
    """录制单个分段(真实操作),返回 (成功, 实际时长秒)。"""
    from playwright.sync_api import sync_playwright
    url = seg["url"]
    if url is None:                                  # 报告段:取最新 run 报告
        reports = sorted((BASE_DIR / "报告").glob("run_*.html"), reverse=True)
        if not reports:
            print("  ✗ 无可用报告(先执行 py b07_main.py run S1)")
            return False, 0.0
        url = reports[0].as_uri()
        print("  · 报告页: %s" % reports[0].name)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport=VIEWPORT, record_video_dir=str(WORK_DIR),
                                      record_video_size=RECORD_SIZE)
        page = context.new_page()
        try:
            page.goto(url, wait_until="networkidle", timeout=30000)
        except Exception as e:
            print("  ✗ 打开页面失败 %s: %s" % (url, str(e)[:120]))
            context.close(); browser.close()
            return False, 0.0
        page.emulate_media(reduced_motion="no-preference")
        page.wait_for_timeout(int(seg.get("wait", 2) * 1000))
        if seg.get("actions"):
            do_actions(page, seg["actions"])
        page.wait_for_timeout(int(seg.get("tail", 1.0) * 1000))
        context.close()
        browser.close()
    videos = sorted(WORK_DIR.glob("*.webm"))
    if not videos:
        return False, 0.0
    shutil.move(str(videos[-1]), str(out_webm))
    return True, probe_duration(out_webm)


# ---------------- 字幕与合成 ----------------
def ass_header():
    return ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1280\nPlayResY: 720\n"
            "WrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, "
            "Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, "
            "MarginV, Encoding\n"
            "Style: Sub,Microsoft YaHei,36,&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,"
            "1,0,0,0,100,100,0,0,1,2.2,0,2,60,60,52,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
            "Effect, Text\n")


def ts(sec):
    h = int(sec // 3600); m = int(sec % 3600 // 60); s = sec % 60
    return "%d:%02d:%05.2f" % (h, m, s)


def split_caption(text, n=2):
    """按标点把旁白拆成 n 行字幕。"""
    parts = re.split(r"[,。;]", text)
    parts = [x for x in parts if x.strip()]
    if len(parts) <= n:
        return "\\N".join(parts) if parts else text
    size = max(1, len(parts) // n)
    return "\\N".join("".join(parts[i * size:(i + 1) * size]) for i in range(n))


def main():
    setup_console()
    only = []
    if "--segments" in sys.argv:
        i = sys.argv.index("--segments")
        only = [int(x) for x in sys.argv[i + 1].split(",") if x.strip()]
    print("================ GreenMate 演示片生成(旁白+字幕+实操录屏)================")
    print("· 分段数: %d%s" % (len(SEGMENTS), "(仅 %s)" % only if only else ""))

    # 0) 准备:清旧视频(门户视频块将在生成后重建)、清密钥(保证降级演示稳定)、起三服务
    OLD_WEBM.unlink(missing_ok=True)
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    raw_dir = BASE_DIR / "证据" / "raw"
    evidence_before = {p.name for p in raw_dir.glob("*.json")}   # 演示采集落盘的记录,结束后清理
    for k in ("DEEPSEEK_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        os.environ.pop(k, None)
    import b01_cli
    import b02_memory
    import b09_portal
    for name, mod, attr, port in (("b01-web", b01_cli, "B01_WEB_PORT", 8123),
                                  ("b02-web", b02_memory, "B02_WEB_PORT", 8223)):
        setattr(mod, attr, port)
        threading.Thread(target=mod.run_web_server, kwargs={"port": port},
                         name=name, daemon=True).start()
        print("· %s 后台启动 → 127.0.0.1:%d" % (name, port), flush=True)
    b09_portal.build_portal()   # 重建门户页(此时无视频块,录屏画面干净)
    threading.Thread(target=b09_portal.serve, kwargs={"port": 8300, "with_children": False},
                     name="portal", daemon=True).start()
    print("· 门户后台启动 → 127.0.0.1:8300", flush=True)
    time.sleep(2)

    # 1) 逐段录制 + 旁白
    pieces = []      # [(name, 视频时长, 旁白wav)]
    for idx, seg in enumerate(SEGMENTS, start=1):
        if only and idx not in only:
            continue
        print("\n[%d/%d] 分段 %s" % (idx, len(SEGMENTS), seg["name"]), flush=True)
        webm = WORK_DIR / ("%s.webm" % seg["name"])
        wav = WORK_DIR / ("%s.wav" % seg["name"])
        ok, dur = record_segment(seg, webm)
        if not ok or dur <= 0:
            print("✗ 分段 %s 录制失败,中止。" % seg["name"])
            return 1
        if not tts_wav(seg["narr"], wav):
            print("✗ 旁白合成失败(检查系统是否启用 Microsoft Huihui 语音)。")
            return 1
        print("  ✓ 视频 %.1f 秒 / 旁白已合成" % dur, flush=True)
        pieces.append((seg["name"], dur, wav, webm))

    # 2) 拼接视频(同参数 webm concat)+ 对齐旁白(按实测时长补齐/截断)
    with open(WORK_DIR / "vlist.txt", "w", encoding="utf-8") as f:
        for name, _, _, webm in pieces:
            f.write("file '%s'\n" % webm.as_posix())
    concat_v = WORK_DIR / "concat.webm"
    code, out = run([FFMPEG, "-y", "-f", "concat", "-safe", "0",
                     "-i", str(WORK_DIR / "vlist.txt"), "-c", "copy", str(concat_v)])
    if code != 0:
        print("✗ 视频拼接失败: %s" % out[-300:])
        return 1
    with open(WORK_DIR / "alist.txt", "w", encoding="utf-8") as f:
        for name, dur, wav, _ in pieces:
            pad = WORK_DIR / ("%s_pad.wav" % name)
            code, out = run([FFMPEG, "-y", "-i", str(wav), "-af", "apad",
                             "-t", "%.2f" % (dur + 0.4), "-ar", "44100", "-ac", "1",
                             str(pad)])
            if code != 0:
                print("✗ 旁白对齐失败: %s" % out[-300:])
                return 1
            f.write("file '%s'\n" % pad.as_posix())
    narration = WORK_DIR / "narration.wav"
    code, out = run([FFMPEG, "-y", "-f", "concat", "-safe", "0",
                     "-i", str(WORK_DIR / "alist.txt"), "-c", "copy", str(narration)])
    if code != 0:
        print("✗ 旁白拼接失败: %s" % out[-300:])
        return 1

    # 3) 字幕 ASS(按实测时长累计)
    ass = [ass_header()]
    cursor = 0.0
    for name, dur, _, _ in pieces:
        text = [s["narr"] for s in SEGMENTS if s["name"] == name][0]
        ass.append("Dialogue: 0,%s,%s,Sub,,0,0,0,,%s"
                   % (ts(cursor + 0.4), ts(cursor + dur - 0.2), split_caption(text)))
        cursor += dur
    ass_file = WORK_DIR / "subs.ass"
    ass_file.write_text("\n".join(ass), encoding="utf-8")

    # 4) 合成:H.264 + AAC + 烧录字幕 → portal_video.mp4
    # 注意:在 WORK_DIR 下用相对路径执行,规避 Windows 路径在 filtergraph 中的转义问题
    print("\n· 合成最终 mp4(烧录字幕 + 混入旁白)…", flush=True)
    code, out = run([FFMPEG, "-y", "-i", "concat.webm", "-i", "narration.wav",
                     "-vf", "ass=subs.ass",
                     "-c:v", "libx264", "-preset", "medium", "-crf", "22",
                     "-c:a", "aac", "-b:a", "128k", "-shortest",
                     "-movflags", "+faststart", str(FINAL_VIDEO)],
                    timeout=600, cwd=str(WORK_DIR))
    if code != 0 or not FINAL_VIDEO.is_file():
        print("✗ 最终合成失败: %s" % out[-400:])
        return 1
    print("✓ 演示片已生成: %s(%.1f 秒,%d KB)" %
          (FINAL_VIDEO, probe_duration(FINAL_VIDEO), FINAL_VIDEO.stat().st_size // 1024))

    # 5) 收尾:重建门户(自动嵌入新视频)并清理临时目录与演示采集记录
    b09_portal.build_portal()
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    for p in raw_dir.glob("*.json"):
        if p.name not in evidence_before:
            p.unlink(missing_ok=True)   # 只清理本脚本演示期间落盘的采集记录
    print("✓ 门户首页已重建并嵌入新视频;临时素材与演示采集记录已清理。")
    print("  查看: py b09_portal.py serve → http://127.0.0.1:8300/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
