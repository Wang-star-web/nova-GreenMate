#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 GreenMate B04 — RAG 检索增强模块(番茄专业知识库)
=====================================================================

【任务目标】
  建立番茄专业知识库:3~5 份可信资料做 source 登记与文档切片(每个 chunk 绑定
  source_id,char_range 可追溯回原文),零依赖字符向量(字符 bigram + TF-IDF +
  余弦相似度)做 Top-K 检索。流程:Query → Embedding 检索 Top-K → 检索片段送入
  LLM 上下文 → LLM 基于检索内容回答并输出引用。资料无法覆盖的问题必须输出
  【资料不足/无法确认】,禁止编造答案与引用。

【运行方式】
  py b04_rag.py build                # 重建 chunk/索引(产出 source_register.json + index.json)
  py b04_rag.py sources              # 查看 source 登记表
  py b04_rag.py search <query>       # 纯检索(先验检索人工验证工具,不接 LLM)
  py b04_rag.py ask <query>          # 完整 RAG:检索 → LLM 回答 + 引用
  py b04_rag.py calibrate            # 阈值校准(检索分数分布 → 推荐阈值)
  py b04_rag.py test                 # 先验检索测试(资料存在/缺失两类用例,无 LLM)
  py b04_rag.py --help               # 查看本说明

【业务边界】
  知识库资料为课程项目自编模拟资料(登记中如实声明),不接实时传感器;不使用
  LangChain/LangGraph 等框架,仅基础 RAG;检索零依赖纯标准库,LLM 部分惰性复用
  requests。检索可在无 API Key 环境运行;资料不足禁止编造,LLM 失败如实上报。
=====================================================================
"""

import collections
import hashlib
import json
import math
import os
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

# ---------------- 配置 ----------------
BASE_DIR = Path(__file__).resolve().parent
KNOWLEDGE_DIR = BASE_DIR / "knowledge"
SOURCES_DIR = KNOWLEDGE_DIR / "sources"
REGISTER_PATH = KNOWLEDGE_DIR / "source_register.json"
INDEX_PATH = KNOWLEDGE_DIR / "index.json"
EVIDENCE_DIR = BASE_DIR / "evidence" / "b04"
LOG_DIR = BASE_DIR / "logs"
RAG_LOG = LOG_DIR / "b04_rag.log"

# 资料登记(固定顺序决定构建确定性)
SOURCE_FILES = (
    ("s1", "s1_temperature.md", "番茄大棚温度管理"),
    ("s2", "s2_humidity_co2.md", "大棚湿度与 CO₂ 管理"),
    ("s3", "s3_light.md", "番茄光照管理"),
    ("s4", "s4_water_fertilizer.md", "番茄水肥管理"),
    ("s5", "s5_pest_harvest.md", "番茄病虫害防治与授粉采收"),
)
SOURCE_AUTHOR = "GreenMate 项目组(Claude 编写)"
SOURCE_DATE = "2026-10-29"
# 诚实来源声明:不冒用任何出版社/机构
SOURCE_ORIGIN = ("GreenMate 课程项目自编模拟资料:由项目组(Claude)依据公开番茄栽培农学知识"
                 "(农业院校教材与农技推广资料中的通用结论)整理编写,阈值对齐本项目 "
                 "b03_rules.json 规则 R1~R8;非任何出版社、期刊或官方机构的正式出版物,"
                 "不可作为真实农业生产指导依据。")

# 切片参数
CHUNK_TARGET = 180     # 目标 chunk 长度(字符)
CHUNK_MAX = 220        # 单块上限(字符)
CHUNK_MIN = 60         # 单块下限(不足则强制并入)
CHUNK_OVERLAP = 40     # 相邻块重叠(≥40 字符,优先落在句边界)

DEFAULT_K = 5
MAX_K = 20

# 检索充分性阈值:经 `calibrate` 对真实语料校准(覆盖类最低 0.2149,未覆盖类最高 0.1234,
# 分离度 0.0915,取中点 0.17);env 可覆盖
try:
    B04_SCORE_THRESHOLD = float(os.environ.get("B04_SCORE_THRESHOLD", "0.17"))
except ValueError:
    B04_SCORE_THRESHOLD = 0.17

# LLM 配置(b01 同款约定:全部可被环境变量覆盖,代码中零硬编码 API Key)
B04_LLM_BASE_URL = os.environ.get("B04_LLM_BASE_URL", "https://api.deepseek.com/chat/completions")
B04_LLM_MODEL = os.environ.get("B04_LLM_MODEL", "deepseek-chat")
try:
    B04_LLM_TIMEOUT = float(os.environ.get("B04_LLM_TIMEOUT", "30"))
except ValueError:
    B04_LLM_TIMEOUT = 30.0

# 校准与测试用例(先验检索,无 LLM)
CALIBRATION_QUERIES = (
    ("番茄适宜生长温度是多少", True), ("大棚空气湿度多少合适", True),
    ("CO₂浓度多少合适", True), ("光照不足怎么办", True),
    ("番茄灰霉病怎么防治", True), ("番茄如何授粉", True),
    ("番茄坐果期要追什么肥", True),
    ("比特币价格是多少", False), ("量子计算是什么", False), ("番茄股票代码", False),
)
TEST_CASES = (
    ("c1", "番茄适宜生长温度是多少", "s1"),
    ("c2", "大棚空气湿度多少合适", "s2"),
    ("c3", "CO₂浓度多少合适", "s2"),
    ("c4", "光照不足怎么办", "s3"),
    ("c5", "番茄灰霉病怎么防治", "s5"),
    ("c6", "番茄如何授粉", "s5"),
    ("c7", "番茄坐果期要追什么肥", "s4"),
    ("c8", "比特币价格是多少", None),
    ("c9", "量子计算是什么", None),
    ("c10", "番茄股票代码", None),
)

_index_cache = None   # 模块级索引缓存(库调用性能;build 后失效)


def setup_console():
    """强制标准输出为 UTF-8:GBK 管道打印 ℃/CO₂ 会崩溃(本项目已多次实测)。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def _now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------- 文本归一化与向量化 ----------------
def normalize(text):
    """NFKC + 小写 + 空白折叠:₂→2、²→2、℃→°C、全角→半角,两侧同用,保证 CO2 命中 CO₂。"""
    text = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"\s+", " ", text).strip()


def grams_of(text):
    """全部单字 + 相邻双字,计数为 tf(原始频次)。"""
    t = normalize(text)
    counter = collections.Counter()
    for i, ch in enumerate(t):
        counter[ch] += 1
        if i + 1 < len(t):
            counter[t[i:i + 2]] += 1
    return counter


def cosine(query_vec, chunk_wmap, query_norm, chunk_norm):
    """稀疏余弦:遍历查询非零 gram 与 chunk 权重 dict 点积。"""
    dot = 0.0
    for gidx, weight in query_vec.items():
        chunk_weight = chunk_wmap.get(gidx)
        if chunk_weight:
            dot += weight * chunk_weight
    if dot <= 0 or query_norm <= 0 or chunk_norm <= 0:
        return 0.0
    return dot / (query_norm * chunk_norm)


# ---------------- 文档切片 ----------------
def split_sentences(text):
    """按 。!?; 与换行断句(句尾保留在句内)。返回 [(句文本, start, end)]。"""
    sentences = []
    start = 0
    for i, ch in enumerate(text):
        if ch in "。!?;\n":
            sentences.append((text[start:i + 1], start, i + 1))
            start = i + 1
    if start < len(text):
        sentences.append((text[start:], start, len(text)))
    return sentences


def hard_split(s_text):
    """超长句硬切:优先在 [140, 220] 内找 。!?; 断点,否则 180 字符处硬切。
    返回 [(子串, 相对起点)]。"""
    parts = []
    pos, length = 0, len(s_text)
    while pos < length:
        if length - pos <= CHUNK_MAX:
            parts.append((s_text[pos:], pos))
            break
        cut = -1
        for i in range(pos + 140, min(pos + CHUNK_MAX, length)):
            if s_text[i] in "。!?;":
                cut = i + 1
                break
        if cut > 0:
            parts.append((s_text[pos:cut], pos))
            pos = cut
        else:
            parts.append((s_text[pos:pos + CHUNK_TARGET], pos))
            pos += CHUNK_TARGET
    return parts


def build_units(text):
    """断句 + 超长句硬切 → 全部 ≤ CHUNK_MAX 的切分单元 [(文本, 原文字符起点)]。"""
    units = []
    for t, st, _en in split_sentences(text):
        if len(t) > CHUNK_MAX:
            for pt, prs in hard_split(t):
                units.append((pt, st + prs))
        else:
            units.append((t, st))
    return units


def chunk_text(text):
    """贪心打包:目标 180、上限 220、下限 60;相邻块重叠 ≥40 字符且优先落在句边界。
    返回 [(chunk_text, start, end)],char_range 为原文字符偏移 [start, end)。"""
    units = build_units(text)
    chunks = []
    idx, n = 0, len(units)
    while idx < n:
        first_idx = idx
        cur_text, cur_start = units[idx]
        cur_end = cur_start + len(cur_text)
        idx += 1
        while idx < n:
            t2, st2 = units[idx]
            if len(cur_text) < CHUNK_TARGET and len(cur_text) + len(t2) <= CHUNK_MAX:
                cur_text += t2
                cur_end = st2 + len(t2)
                idx += 1
            elif len(cur_text) < CHUNK_MIN:
                cur_text += t2
                cur_end = st2 + len(t2)
                idx += 1
            else:
                break
        # 尾部 <30 字并入前一块
        if len(cur_text) < 30 and chunks:
            pt, ps, _pe = chunks[-1]
            chunks[-1] = (pt + cur_text, ps, cur_end)
            break
        chunks.append((cur_text, cur_start, cur_end))
        if idx >= n:
            break
        # 重叠:在当前块内(不含首单元)找起点 <= cur_end-40 的最大单元,回退到它
        cut = cur_end - CHUNK_OVERLAP
        new_idx = idx
        for k in range(idx - 1, first_idx, -1):
            if units[k][1] <= cut:
                new_idx = k
                break
        idx = max(new_idx, first_idx + 1)  # 保证前进
    return chunks


# ---------------- 构建:登记 + 索引 ----------------
def _file_sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_knowledge_base():
    """重建 source_register.json 与 index.json(除 built_at 外完全确定,
    build_fingerprint 一致即证明可重建)。返回 (register, index) 或 None。"""
    global _index_cache
    try:
        SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print("⚠ 知识库目录创建失败: %s" % e)
        return None
    register = []
    source_meta = {}   # source_id -> {title, sha256, raw}
    for source_id, filename, title in SOURCE_FILES:
        path = SOURCES_DIR / filename
        if not path.exists():
            print("⚠ 资料文件缺失: %s" % path.relative_to(BASE_DIR))
            return None
        raw = path.read_text(encoding="utf-8")
        register.append({
            "source_id": source_id, "title": title,
            "author": SOURCE_AUTHOR, "date": SOURCE_DATE,
            "origin": SOURCE_ORIGIN,
            "file": "knowledge/sources/%s" % filename,
            "encoding": "utf-8",
            "char_count": len(raw),
            "sha256": _file_sha256(path),
            "chunk_count": 0,
        })
        source_meta[source_id] = {"title": title, "sha256": register[-1]["sha256"], "raw": raw}

    # 切片
    chunk_records = []   # {chunk_id, source_id, seq, text, char_range}
    for source_id, _filename, _title in SOURCE_FILES:
        raw = source_meta[source_id]["raw"]
        seq = 0
        for ctext, start, end in chunk_text(raw):
            seq += 1
            if not (0 <= start < end <= len(raw) and raw[start:end] == ctext):
                print("⚠ 切片校验失败: %s 第 %d 块 char_range 与原文不一致" % (source_id, seq))
                return None
            chunk_records.append({
                "chunk_id": "%s_c%03d" % (source_id, seq), "source_id": source_id,
                "seq": seq, "text": ctext, "char_range": [start, end],
            })
        for entry in register:
            if entry["source_id"] == source_id:
                entry["chunk_count"] = seq

    total_chunks = len(chunk_records)
    if total_chunks == 0:
        print("⚠ 切片结果为空")
        return None

    # 词表与 IDF
    doc_freq = collections.Counter()
    per_chunk_grams = []
    for cr in chunk_records:
        grams = grams_of(cr["text"])
        per_chunk_grams.append(grams)
        for gram in grams:
            doc_freq[gram] += 1
    vocab = sorted(doc_freq.keys())   # 按码点排序 → 确定性
    gram_index = {gram: i for i, gram in enumerate(vocab)}
    vocab_rows = [{"gram": gram, "df": doc_freq[gram],
                   "idf": round(math.log(total_chunks / doc_freq[gram]) + 1.0, 6)} for gram in vocab]

    index_chunks = []
    fingerprint_lines = []
    for cr, grams in zip(chunk_records, per_chunk_grams):
        vec, norm = [], 0.0
        for gram, tf in sorted(grams.items(), key=lambda kv: gram_index[kv[0]]):
            idx = gram_index[gram]
            weight = round(tf * vocab_rows[idx]["idf"], 6)
            vec.append([idx, weight])
            norm += weight * weight
        norm = round(math.sqrt(norm), 6)
        index_chunks.append({**cr, "vec": vec, "norm": norm})
        fingerprint_lines.append("%s|%s|%s|%d|%d|%s|%s" % (
            cr["source_id"], source_meta[cr["source_id"]]["sha256"], cr["chunk_id"],
            cr["char_range"][0], cr["char_range"][1], cr["text"],
            ",".join("%d:%s" % (i, w) for i, w in vec)))

    for source_id, _filename, _title in SOURCE_FILES:
        fingerprint_lines.append("%s|%s" % (source_id, source_meta[source_id]["sha256"]))
    build_fingerprint = hashlib.sha256("\n".join(fingerprint_lines).encode("utf-8")).hexdigest()

    index = {
        "version": 1,
        "built_at": _now_iso(),
        "build_fingerprint": build_fingerprint,
        "normalization": "NFKC + lowercase + whitespace-collapse",
        "chunking": {"target_chars": CHUNK_TARGET, "max_chars": CHUNK_MAX,
                     "min_chars": CHUNK_MIN, "overlap_chars": CHUNK_OVERLAP},
        "total_chunks": total_chunks,
        "vocab_size": len(vocab),
        "sources": [{"source_id": r["source_id"], "title": r["title"],
                     "sha256": r["sha256"], "chunk_count": r["chunk_count"]} for r in register],
        "vocab": vocab_rows,
        "chunks": index_chunks,
    }

    # 对照旧 register 的 sha256,源文件变化给 ⚠(防篡改信号)
    if REGISTER_PATH.exists():
        try:
            old_register = json.loads(REGISTER_PATH.read_text(encoding="utf-8"))
            old_map = {r["source_id"]: r.get("sha256") for r in old_register}
            for r in register:
                if r["source_id"] in old_map and old_map[r["source_id"]] != r["sha256"]:
                    print("⚠ 资料 %s(%s)内容与上次构建不同,请确认是否预期修改并重新校准阈值"
                          % (r["source_id"], r["title"]))
        except (OSError, ValueError):
            pass

    _write_json(REGISTER_PATH, register)
    _write_json(INDEX_PATH, index)
    _index_cache = None
    return register, index


def _write_json(path, obj):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        return True
    except OSError as e:
        print("⚠ 文件写入失败 %s: %s" % (path, e))
        return False


def load_index():
    """加载索引(带缓存);缺失/损坏返回 None。"""
    global _index_cache
    if _index_cache is not None:
        return _index_cache
    if not INDEX_PATH.exists():
        return None
    try:
        index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for cr in index.get("chunks", []):
        cr["wmap"] = {int(i): float(w) for i, w in cr.get("vec", [])}
    _index_cache = index
    return index


def load_register():
    """加载 source 登记;缺失/损坏返回 []。"""
    if not REGISTER_PATH.exists():
        return []
    try:
        return json.loads(REGISTER_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def register_map(register=None):
    register = register if register is not None else load_register()
    return {r["source_id"]: r for r in register}


# ---------------- 公开接口(官方契约,供 B07 复用)----------------
def search_tomato_knowledge(query, k=DEFAULT_K, score_threshold=None):
    """番茄知识库检索:Query → 字符向量 Top-K → retrieval_evidence。
    资料无法覆盖时 status="insufficient" + 【资料不足/无法确认】,绝不编造。
    库调用只记日志不落盘;无 API Key 依赖。"""
    threshold = score_threshold if score_threshold is not None else B04_SCORE_THRESHOLD
    k = max(1, min(int(k), MAX_K))
    ev = {
        "query": str(query), "retrieved_at": _now_iso(), "k": k,
        "threshold": threshold, "status": "insufficient", "insufficient": True,
        "message": None, "top_chunks": [],
    }
    qtext = normalize(str(query))
    if not qtext:
        ev["message"] = "【资料不足/无法确认】查询内容为空。"
        return ev
    index = load_index()
    if index is None:
        ev["message"] = "【资料不足/无法确认】知识库索引不存在,请先运行 py b04_rag.py build。"
        return ev
    gram_lookup = {row["gram"]: i for i, row in enumerate(index["vocab"])}
    query_vec = {}
    for gram, tf in grams_of(str(query)).items():
        gidx = gram_lookup.get(gram)
        if gidx is not None:
            query_vec[gidx] = tf * index["vocab"][gidx]["idf"]
    if not query_vec:
        ev["message"] = "【资料不足/无法确认】查询内容与知识库无共同词汇,无法检索。"
        return ev
    query_norm = math.sqrt(sum(w * w for w in query_vec.values()))
    register = register_map()
    scored = []
    for cr in index["chunks"]:
        score = cosine(query_vec, cr["wmap"], query_norm, cr["norm"])
        if score > 0:
            scored.append((round(score, 4), cr))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    for rank, (score, cr) in enumerate(scored[:k], start=1):
        source = register.get(cr["source_id"], {})
        ev["top_chunks"].append({
            "rank": rank, "chunk_id": cr["chunk_id"], "source_id": cr["source_id"],
            "score": score, "text": cr["text"], "char_range": cr["char_range"],
            "citation": {"source_id": cr["source_id"], "title": source.get("title", cr["source_id"]),
                         "chunk_id": cr["chunk_id"], "char_range": cr["char_range"], "seq": cr["seq"]},
        })
    if ev["top_chunks"] and ev["top_chunks"][0]["score"] >= threshold:
        ev["status"] = "ok"
        ev["insufficient"] = False
    else:
        top_score = ev["top_chunks"][0]["score"] if ev["top_chunks"] else 0.0
        ev["message"] = ("【资料不足/无法确认】检索最高得分 %.4f 低于阈值 %.4f,"
                         "知识库未覆盖该问题,依据边界约束不作回答。" % (top_score, threshold))
    _log_rag("[检索] query=%s k=%d 状态=%s top1_score=%s" % (
        str(query)[:80], k, ev["status"],
        ev["top_chunks"][0]["score"] if ev["top_chunks"] else 0.0))
    return ev


# ---------------- LLM 回答路径 ----------------
def _get_api_key():
    """读取 LLM API Key: DEEPSEEK_API_KEY 优先,回退 ANTHROPIC_AUTH_TOKEN。
    返回 (key|None, 来源名|None);绝不硬编码、绝不打印 key 值。"""
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        return key, "env:DEEPSEEK_API_KEY"
    key = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if key:
        return key, "env:ANTHROPIC_AUTH_TOKEN"
    return None, None


def _call_llm(messages):
    """b01 ask_llm 模式移植:真实 HTTP 调用,成功路径只有解析真实返回一条;失败如实上报。"""
    try:
        import requests   # 惰性导入:检索功能无需 requests
    except ImportError:
        return None, {"error_type": "missing_dependency",
                      "message": "未安装 requests 库,无法调用 LLM(可执行 pip install requests)"}
    headers = {"Authorization": "Bearer %s" % _get_api_key()[0], "Content-Type": "application/json"}
    payload = {"model": B04_LLM_MODEL, "messages": messages, "stream": False}
    try:
        resp = requests.post(B04_LLM_BASE_URL, json=payload, headers=headers,
                             timeout=(B04_LLM_TIMEOUT, B04_LLM_TIMEOUT))
    except requests.exceptions.ConnectTimeout:
        return None, {"error_type": "network_timeout",
                      "message": "连接超时(超过 %g 秒未建立连接),请检查网络或 B04_LLM_TIMEOUT" % B04_LLM_TIMEOUT}
    except requests.exceptions.ReadTimeout:
        return None, {"error_type": "network_timeout",
                      "message": "读取超时(服务器 %g 秒内未返回结果)" % B04_LLM_TIMEOUT}
    except requests.exceptions.ConnectionError:
        return None, {"error_type": "network_timeout",
                      "message": "网络连接失败,无法访问 %s" % B04_LLM_BASE_URL}
    except Exception as e:
        return None, {"error_type": "network_timeout", "message": "网络异常: %s" % e}
    if resp.status_code in (401, 403):
        return None, {"error_type": "api_key_error",
                      "message": "HTTP %d 认证失败,API Key 无效或已过期" % resp.status_code}
    if resp.status_code != 200:
        return None, {"error_type": "http_error",
                      "message": "HTTP %d: %s" % (resp.status_code, resp.text[:200])}
    try:
        answer = resp.json()["choices"][0]["message"]["content"]
    except Exception:
        return None, {"error_type": "bad_response",
                      "message": "API 返回内容无法解析(缺少 choices[0].message.content)"}
    return str(answer), None


def _build_rag_messages(query, top_chunks, register):
    system = ("你是 GreenMate 番茄种植知识库助手。回答必须严格遵守以下规则:\n"
              "1. 只能依据用户消息中提供的【资料】内容作答,不得使用资料之外的任何知识,"
              "不得编造数据、阈值或结论;\n"
              "2. 引用资料时,在对应句末标注资料编号 [n](n 必须且只能是资料列表中出现的编号);\n"
              "3. 如果资料不足以回答该问题,只输出:【资料不足/无法确认】,不得猜测或补充;\n"
              "4. 资料为课程项目自编模拟资料,不得声称其来自出版社或官方机构。")
    lines = ["请回答以下问题:%s" % query, "", "可依据的资料(仅限以下内容):"]
    for i, chunk in enumerate(top_chunks, start=1):
        src = register.get(chunk["source_id"], {})
        lines.append("[%d] 来源 %s(%s,字符区间 %d-%d):%s" % (
            i, src.get("title", chunk["source_id"]), chunk["source_id"],
            chunk["char_range"][0], chunk["char_range"][1], chunk["text"]))
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n".join(lines)}]


def answer_tomato_question(query, k=DEFAULT_K):
    """完整 RAG:检索 → (资料充足才)调 LLM → 基于检索内容回答 + 引用。
    资料不足:不调 LLM,直接返回【资料不足/无法确认】;LLM 失败如实上报,禁止编造。"""
    k = max(1, min(int(k), MAX_K))
    ev = {
        "query": str(query), "answered_at": _now_iso(), "k": k, "status": "error",
        "answer": None, "citations": [], "retrieval_evidence": None,
        "error": None, "llm": None, "citation_check": {"markers_found": [], "unknown_markers": []},
    }
    retrieval = search_tomato_knowledge(query, k=k)
    ev["retrieval_evidence"] = retrieval
    if retrieval["status"] != "ok":
        ev["status"] = "insufficient"
        ev["answer"] = "【资料不足/无法确认】当前知识库未检索到与该问题相关的可信内容,依据边界约束不作回答。"
        _log_rag("[检索不足] query=%s message=%s" % (str(query)[:80], retrieval["message"]))
        return ev
    key, key_source = _get_api_key()
    if key is None:
        ev["error"] = {"error_type": "no_api_key",
                       "message": "未配置 API Key(需设置环境变量 DEEPSEEK_API_KEY 或 ANTHROPIC_AUTH_TOKEN)"}
        _log_rag("[失败] query=%s error_type=no_api_key" % str(query)[:80])
        return ev
    register = register_map()
    messages = _build_rag_messages(str(query), retrieval["top_chunks"], register)
    _log_rag("[请求] query=%s k=%d top1=%s score=%s" % (
        str(query)[:80], k, retrieval["top_chunks"][0]["chunk_id"],
        retrieval["top_chunks"][0]["score"]))
    answer, err = _call_llm(messages)
    if err is not None:
        ev["error"] = err
        _log_rag("[失败] query=%s error_type=%s message=%s" % (
            str(query)[:80], err["error_type"], err["message"][:120]))
        return ev
    ev["status"] = "success"
    ev["answer"] = answer
    ev["citations"] = [
        {"n": chunk["rank"], "rank": chunk["rank"], "source_id": chunk["source_id"],
         "title": chunk["citation"]["title"], "chunk_id": chunk["chunk_id"],
         "char_range": chunk["char_range"], "seq": chunk["citation"]["seq"],
         "score": chunk["score"]}
        for chunk in retrieval["top_chunks"]
    ]
    ev["llm"] = {"base_url": B04_LLM_BASE_URL, "model": B04_LLM_MODEL, "key_source": key_source}
    markers = sorted({int(m) for m in re.findall(r"\[(\d+)\]", answer)})
    ev["citation_check"] = {
        "markers_found": markers,
        "unknown_markers": [m for m in markers if m < 1 or m > len(ev["citations"])],
    }
    _log_rag("[成功] query=%s citations=%s unknown_markers=%s" % (
        str(query)[:80], len(ev["citations"]), ev["citation_check"]["unknown_markers"]))
    return ev


# ---------------- 校验 / 落盘 / 打印 / 日志 ----------------
def save_evidence(ev, filename):
    """证据落盘 evidence/b04/{filename}(自动建目录,UTF-8)。"""
    try:
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        with open(EVIDENCE_DIR / filename, "w", encoding="utf-8") as f:
            json.dump(ev, f, ensure_ascii=False, indent=2)
        return True
    except OSError as e:
        print("⚠ 证据保存失败: %s" % e)
        return False


def _log_rag(text):
    """追加一行时间戳日志到 logs/b04_rag.log 并镜像控制台;绝不记录密钥。
    库级路径(B07 import 调用)不会先走 main 的 setup_console,必须在此兜底(GBK 教训)。"""
    setup_console()
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), text)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(RAG_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line)


def _print_retrieval(ev):
    mark = "✓" if ev["status"] == "ok" else "⚠"
    print("\n---------------- 检索结果 retrieval_evidence ----------------")
    print("  %s status=%s | query=%s | k=%d | threshold=%.4f" % (
        mark, ev["status"], ev["query"], ev["k"], ev["threshold"]))
    if ev["message"]:
        print("  message: %s" % ev["message"])
    for chunk in ev["top_chunks"]:
        c = chunk["citation"]
        print("  [%d] score=%.4f %s | %s(%s) | 字符区间 %d-%d" % (
            chunk["rank"], chunk["score"], chunk["chunk_id"], c["title"], c["source_id"],
            chunk["char_range"][0], chunk["char_range"][1]))
        print("      %s" % chunk["text"][:120].replace("\n", " ") + ("…" if len(chunk["text"]) > 120 else ""))


def _print_answer(ev):
    if ev["status"] == "success":
        print("\n---------------- RAG 回答 ----------------")
        print("  问题: %s" % ev["query"])
        print("  回答: %s" % ev["answer"])
        print("  引用:")
        for c in ev["citations"]:
            print("    [%d] %s(%s) %s 字符区间 %d-%d (score=%.4f)" % (
                c["n"], c["title"], c["source_id"], c["chunk_id"],
                c["char_range"][0], c["char_range"][1], c["score"]))
    elif ev["status"] == "insufficient":
        print("\n---------------- RAG 回答 ----------------")
        print("  问题: %s" % ev["query"])
        print("  ⚠ %s" % ev["answer"])
    else:
        err = ev["error"] or {}
        print("\n---------------- RAG 回答(失败)----------------")
        print("  问题: %s" % ev["query"])
        print("  ⚠ error_type=%s | %s" % (err.get("error_type"), err.get("message")))


# ---------------- CLI ----------------
def _join_query(args):
    """非 -- 开头的位置参数拼接为查询语句。"""
    parts = [a for a in args if not a.startswith("-")]
    return " ".join(parts) if parts else None


def _parse_flag(args, name, default=None):
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return default


def cmd_build():
    result = build_knowledge_base()
    if result is None:
        return
    register, index = result
    print("\n================ B04 知识库构建 ================")
    for r in register:
        print("  %s %s: %d 字符, %d 块 (sha256 %s…)" % (
            r["source_id"], r["title"], r["char_count"], r["chunk_count"], r["sha256"][:8]))
    print("  共 %d 个 chunk,词表 %d 个 gram" % (index["total_chunks"], index["vocab_size"]))
    print("  build_fingerprint: %s" % index["build_fingerprint"])
    print("✓ 已写入 knowledge/source_register.json 与 knowledge/index.json")
    save_evidence({
        "built_at": index["built_at"], "build_fingerprint": index["build_fingerprint"],
        "total_chunks": index["total_chunks"], "vocab_size": index["vocab_size"],
        "sources": index["sources"],
    }, "build_%s.json" % datetime.now().strftime("%Y%m%d_%H%M%S"))


def cmd_sources():
    register = load_register()
    if not register:
        print("⚠ source 登记不存在,请先运行 py b04_rag.py build")
        return
    print("\n---------------- source 登记表 ----------------")
    print("%-6s %-22s %-24s %-12s %-10s %-8s" % ("id", "标题", "作者", "日期", "sha256", "chunks"))
    for r in register:
        print("%-6s %-22s %-24s %-12s %-10s %-8d" % (
            r["source_id"], r["title"], r["author"], r["date"], r["sha256"][:8], r["chunk_count"]))
    print("来源声明: %s" % register[0]["origin"])


def cmd_search(args):
    query = _join_query(args)
    if not query:
        print("⚠ 请提供查询内容,如: py b04_rag.py search 番茄适宜生长温度是多少")
        return
    k_text = _parse_flag(args, "--k", str(DEFAULT_K))
    try:
        k = int(k_text)
    except ValueError:
        print("⚠ --k 必须是整数")
        return
    threshold_text = _parse_flag(args, "--threshold")
    threshold = None
    if threshold_text is not None:
        try:
            threshold = float(threshold_text)
        except ValueError:
            print("⚠ --threshold 必须是数字")
            return
    ev = search_tomato_knowledge(query, k=k, score_threshold=threshold)
    _print_retrieval(ev)
    if "--save" in args:
        filename = "retrieval_%s.json" % datetime.now().strftime("%Y%m%d_%H%M%S")
        if save_evidence(ev, filename):
            print("✓ 证据已保存: evidence/b04/%s" % filename)


def cmd_ask(args):
    query = _join_query(args)
    if not query:
        print("⚠ 请提供问题,如: py b04_rag.py ask 番茄适宜生长温度是多少")
        return
    k_text = _parse_flag(args, "--k", str(DEFAULT_K))
    try:
        k = int(k_text)
    except ValueError:
        print("⚠ --k 必须是整数")
        return
    ev = answer_tomato_question(query, k=k)
    _print_answer(ev)
    filename = "rag_answer_%s.json" % datetime.now().strftime("%Y%m%d_%H%M%S")
    if save_evidence(ev, filename):
        print("✓ 证据已保存: evidence/b04/%s" % filename)


def cmd_calibrate():
    """阈值校准:跑 10 条固定查询,输出分数分布与推荐阈值(要求分离度 ≥0.03)。"""
    print("================ B04 阈值校准(检索分数分布)================")
    rows = []
    for query, covered in CALIBRATION_QUERIES:
        ev = search_tomato_knowledge(query)
        top1 = ev["top_chunks"][0] if ev["top_chunks"] else None
        rows.append({
            "query": query, "covered": covered, "status": ev["status"],
            "top1_score": top1["score"] if top1 else 0.0,
            "top1_source": top1["source_id"] if top1 else None,
        })
        print("  [%s] %-24s top1=%.4f (%s)" % (
            "覆盖" if covered else "未覆盖", query,
            rows[-1]["top1_score"], rows[-1]["top1_source"] or "无"))
    covered_scores = [r["top1_score"] for r in rows if r["covered"]]
    uncovered_scores = [r["top1_score"] for r in rows if not r["covered"]]
    s_min, s_max = min(covered_scores), max(uncovered_scores)
    separation = s_min - s_max
    recommended = round(max((s_min + s_max) / 2.0, 0.05), 2)
    print("\n  覆盖类 top1 最低分: %.4f | 未覆盖类 top1 最高分: %.4f | 分离度: %.4f" % (s_min, s_max, separation))
    print("  推荐阈值: %.2f(当前模块阈值 B04_SCORE_THRESHOLD=%.4f)" % (recommended, B04_SCORE_THRESHOLD))
    if separation < 0.03:
        print("  ⚠ 分离度不足 0.03,建议调整语料措辞后重新校准")
    else:
        print("  ✓ 分离度充分,阈值两侧均有安全余量")
    save_evidence({
        "calibrated_at": _now_iso(),
        "threshold_current": B04_SCORE_THRESHOLD,
        "threshold_recommended": recommended,
        "covered_min": s_min, "uncovered_max": s_max, "separation": separation,
        "queries": rows,
    }, "threshold_calibration.json")


def cmd_test():
    """先验检索测试(无 LLM):7 覆盖 + 3 未覆盖 + 溯源校验。"""
    print("================ B04 先验检索测试(不接 LLM)================")
    index = load_index()
    register = register_map()
    if index is None or not register:
        print("⚠ 知识库未构建,请先运行 py b04_rag.py build")
        return
    report = {"checked_at": _now_iso(), "threshold": B04_SCORE_THRESHOLD,
              "summary": {"total": len(TEST_CASES) + 1, "passed": 0, "failed": 0}, "checks": []}
    case_evidences = {}
    for cid, query, expected in TEST_CASES:
        ev = search_tomato_knowledge(query)
        case_evidences[cid] = ev
        top3_sources = [c["source_id"] for c in ev["top_chunks"][:3]]
        if expected is None:   # 未覆盖类
            ok = (ev["status"] == "insufficient" and ev["insufficient"] is True
                  and (not ev["top_chunks"] or all(c["score"] < B04_SCORE_THRESHOLD for c in ev["top_chunks"])))
            detail = "status=%s top1_score=%s" % (ev["status"],
                                                  ev["top_chunks"][0]["score"] if ev["top_chunks"] else 0.0)
        else:                  # 覆盖类
            ok = (ev["status"] == "ok" and ev["insufficient"] is False
                  and ev["top_chunks"] and ev["top_chunks"][0]["score"] > B04_SCORE_THRESHOLD
                  and expected in top3_sources)
            detail = "status=%s top1=%s(score=%.4f) top3=%s" % (
                ev["status"], ev["top_chunks"][0]["chunk_id"] if ev["top_chunks"] else "无",
                ev["top_chunks"][0]["score"] if ev["top_chunks"] else 0.0, top3_sources)
        report["checks"].append({
            "id": cid, "name": "资料覆盖" if expected else "资料不足边界", "query": query,
            "expected": "命中 %s" % expected if expected else "insufficient",
            "actual": detail, "passed": ok,
        })
        print("  [%s] %-2s %-22s %s | %s" % ("✓" if ok else "⚠", cid, query,
                                             "命中 %s" % expected if expected else "资料不足", detail))
    # 溯源校验:1~7 返回的全部引用必须可追溯回原文
    trace_ok, trace_detail = _traceability_check(index, register, case_evidences)
    report["checks"].append({"id": "c0", "name": "溯源校验", "query": "全部覆盖类用例的引用",
                             "expected": "每条引用可追溯回原文", "actual": trace_detail,
                             "passed": trace_ok})
    print("  [%s] c0 溯源校验: %s" % ("✓" if trace_ok else "⚠", trace_detail))
    for c in report["checks"]:
        report["summary"]["passed" if c["passed"] else "failed"] += 1
    h = hashlib.sha256()
    for c in report["checks"]:
        h.update(("%s|%s|%s\n" % (c["id"], "1" if c["passed"] else "0", c["actual"])).encode("utf-8"))
    report["fingerprint"] = h.hexdigest()[:12]
    print("\n  判定: %s(通过 %d/%d;指纹 %s)" % (
        "PASS — 先验检索测试通过" if report["summary"]["failed"] == 0 else "FAIL — 存在未通过用例",
        report["summary"]["passed"], report["summary"]["total"], report["fingerprint"]))
    save_evidence(report, "test_report.json")
    for cid, ev in case_evidences.items():
        save_evidence(ev, "test_case_%s.json" % cid)
    print("✓ 测试报告已保存: evidence/b04/test_report.json 与 test_case_*.json")


def _traceability_check(index, register, case_evidences):
    """每条引用:source_id 在登记中、chunk_id 在索引中、原文切片 == chunk 文本。"""
    chunk_map = {c["chunk_id"]: c for c in index["chunks"]}
    checked, problems = 0, []
    for cid, ev in case_evidences.items():
        for chunk in ev["top_chunks"]:
            checked += 1
            citation = chunk["citation"]
            if citation["source_id"] not in register:
                problems.append("%s: source 未登记" % chunk["chunk_id"])
                continue
            entry = chunk_map.get(chunk["chunk_id"])
            if entry is None:
                problems.append("%s: chunk 不在索引" % chunk["chunk_id"])
                continue
            src_entry = register[citation["source_id"]]
            path = BASE_DIR / src_entry["file"]
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError:
                problems.append("%s: 原文不可读" % chunk["chunk_id"])
                continue
            start, end = chunk["char_range"]
            if not (0 <= start < end <= len(raw) and raw[start:end] == chunk["text"]):
                problems.append("%s: 原文切片不一致" % chunk["chunk_id"])
    if problems:
        return False, "检查 %d 条引用,发现问题 %d: %s" % (checked, len(problems), "; ".join(problems[:3]))
    return True, "检查 %d 条引用全部可追溯回原文" % checked


def print_usage():
    print("用法:")
    print("  py b04_rag.py build                # 重建 chunk/索引(source_register.json + index.json)")
    print("  py b04_rag.py sources              # 查看 source 登记表")
    print("  py b04_rag.py search <query> [--k N] [--threshold T] [--save]")
    print("      纯检索(先验检索人工验证工具,不接 LLM)")
    print("  py b04_rag.py ask <query> [--k N]  # 完整 RAG:检索 → LLM 回答 + 引用")
    print("  py b04_rag.py calibrate            # 阈值校准(分数分布 → 推荐阈值)")
    print("  py b04_rag.py test                 # 先验检索测试(资料存在/缺失两类用例)")
    print("  py b04_rag.py --help")
    print("环境变量: B04_SCORE_THRESHOLD(检索充分性阈值,默认 0.15)")
    print("          B04_LLM_BASE_URL / B04_LLM_MODEL / B04_LLM_TIMEOUT")
    print("          API Key 读取顺序: DEEPSEEK_API_KEY → ANTHROPIC_AUTH_TOKEN")
    print("公开接口: search_tomato_knowledge(query) -> retrieval_evidence,供 B07 复用")
    print("业务边界: 资料不足输出【资料不足/无法确认】,禁止编造答案与引用")


def main():
    """B04 入口:子命令分发。退出码恒 0(清单语义);Ctrl+C/EOF 友好退出。"""
    setup_console()   # 必须最先执行:GBK 管道打印 ℃/CO₂ 会崩溃
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print_usage()
        return
    cmd, rest = args[0], args[1:]
    try:
        if cmd == "build":
            cmd_build()
        elif cmd == "sources":
            cmd_sources()
        elif cmd == "search":
            cmd_search(rest)
        elif cmd == "ask":
            cmd_ask(rest)
        elif cmd == "calibrate":
            cmd_calibrate()
        elif cmd == "test":
            cmd_test()
        else:
            print("未知参数: %s" % " ".join(args))
            print_usage()
    except KeyboardInterrupt:
        print("\n检测到 Ctrl+C,程序已安全退出。")
    except EOFError:
        print("\n输入已结束 (EOF),程序退出。")


if __name__ == "__main__":
    main()
