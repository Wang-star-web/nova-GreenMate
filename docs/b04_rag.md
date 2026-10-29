# B04 RAG 检索增强模块 — 结构定义与重建方法

实现:`b04_rag.py`(纯标准库检索 + 惰性 requests 的 LLM 部分;不使用 LangChain/LangGraph,
不接实时传感器)。

## RAG 流程

```
Query → 字符向量(字符 bigram + TF-IDF)→ Top-K 检索 → 检索片段送入 LLM 上下文
     → LLM 基于检索内容回答 + 输出引用 [n] → 引用列表服务端构建(可溯源回原文)
资料无法覆盖 → 不调 LLM,直接输出【资料不足/无法确认】(禁止编造答案与引用)
```

## 知识库结构

| 路径 | 说明 |
|---|---|
| `knowledge/sources/s1~s5_*.md` | 5 份可信风格番茄种植资料(温度/湿度与 CO₂/光照/水肥/病虫害授粉采收;阈值与 b03_rules.json 的 R1~R8 适宜带逐条对齐) |
| `knowledge/source_register.json` | source 登记:source_id/title/author/date/origin/file/encoding/char_count/sha256/chunk_count。origin 如实声明为课程项目自编模拟资料,不冒用任何出版社或机构 |
| `knowledge/index.json` | 可重建的 chunk/index:切片参数、词表(gram/df/idf)、每 chunk 的 text/char_range/稀疏向量与模长、build_fingerprint |

## 切片规则

- 按 `。!?;` 与换行断句(句尾保留);贪心打包:目标 180 字符、上限 220、下限 60;超长句在 [140,220] 内找断点硬切,否则 180 处切。
- 相邻块重叠 ≥40 字符且优先落在句边界;尾部 <30 字并入前一块。
- `chunk_id = "{source_id}_c{seq:03d}"`;`char_range = [start, end)` 为**原文字符偏移**——每条 chunk 可精确追溯回原文(构建时断言 `raw[start:end] == text`)。

## 向量化与检索

- 归一化:`NFKC + lowercase + 空白折叠`(₂→2、²→2、℃→°C、全角→半角)——用户输入 "CO2" 也能命中资料中的 "CO₂"。
- grams = 全部单字 + 相邻双字(tf=原始频次);`idf = ln(N/df)+1`;chunk/查询向量均为稀疏 `tf*idf`,余弦相似度。
- 充分性判定只看 **top1 分数** 是否 ≥ 阈值(`B04_SCORE_THRESHOLD`,env 可覆盖)——修改 k 不影响判定;阈值不写入索引,重校准无需重建。

## 阈值校准(evidence/b04/threshold_calibration.json)

`py b04_rag.py calibrate` 跑 10 条固定查询(7 覆盖 + 3 未覆盖),实测:

| 指标 | 实测值 |
|---|---|
| 覆盖类 top1 最低分 | 0.2149 |
| 未覆盖类 top1 最高分 | 0.1234 |
| 分离度(要求 ≥0.03) | 0.0915 ✓ |
| 采用阈值(中点) | **0.17** |

覆盖类 top1 全部命中正确 source(s1~s5),未覆盖类全部正确判 insufficient。

## 证据结构

`search_tomato_knowledge(query, k=5, score_threshold=None) -> retrieval_evidence`(供 B07 复用):

```json
{"query", "retrieved_at", "k", "threshold",
 "status": "ok"|"insufficient", "insufficient": bool, "message": null|"【资料不足/无法确认】…",
 "top_chunks": [{"rank", "chunk_id", "source_id", "score", "text", "char_range",
   "citation": {"source_id", "title", "chunk_id", "char_range", "seq"}}]}
```

`answer_tomato_question(query, k=5) -> answer_evidence`:
`{query, answered_at, k, status: success|insufficient|error, answer, citations, retrieval_evidence,
error, llm, citation_check}`——citations **服务端从真实检索片段构建**(绝不解析 LLM 文本);
`citation_check` 审计答案中的 `[n]` 标记(软检查);insufficient 时**不调 LLM**
(日志只有 `[检索不足]` 无 `[请求]`,可验证);LLM 失败如实上报,answer=null。

## 测试用例(先验检索,无 LLM)

`py b04_rag.py test` — 11 项全过,指纹 ece7e61d3a27:

| 类 | 用例 | 断言 |
|---|---|---|
| 资料存在(7) | 适宜温度/湿度/CO₂浓度/光照不足/灰霉病/授粉/坐果期追肥 | status=ok ∧ top1>阈值 ∧ 期望 source 在 top3(实测全部命中 s1~s5 对应文档) |
| 资料缺失(3) | 比特币价格/量子计算/番茄股票代码 | status=insufficient ∧ 候选为空或全低于阈值 |
| 溯源(1) | 前 7 例返回的全部 50 条引用 | source 在登记 ∧ chunk 在索引 ∧ `原文[char_range] == chunk.text` |

## 重建方法(可重建性)

```bash
py b04_rag.py build      # 重新切片 + 重建 source_register.json 与 index.json
```

- 除 `built_at` 外构建完全确定:**同一批源文件反复构建,`build_fingerprint` 恒定**
  (sha256 覆盖 source sha256/chunk 内容与向量,不含时间)——指纹一致即证明索引可复现。
- 构建时对照旧 register 的 sha256:源文件被改动会打印 ⚠(防篡改信号);改语料后需重新
  `calibrate` 并重跑 `test`。

## LLM 配置(b01 同款约定)

| 环境变量 | 默认值 |
|---|---|
| `B04_LLM_BASE_URL` | `https://api.deepseek.com/chat/completions` |
| `B04_LLM_MODEL` | `deepseek-chat` |
| `B04_LLM_TIMEOUT` | `30`(秒) |
| API Key 读取顺序 | `DEEPSEEK_API_KEY` → `ANTHROPIC_AUTH_TOKEN` |

提示词约束:只能依据提供的【资料】作答、句末标注 [n]、资料不足只输出【资料不足/无法确认】、
不得声称资料来自官方机构。密钥绝不落日志。

## 交付资产

| 资产 | 路径 |
|---|---|
| source 登记 | knowledge/source_register.json |
| 可重建 chunk/index | knowledge/index.json(build 重建,指纹可验证) |
| 检索函数 | b04_rag.py:`search_tomato_knowledge(query)` |
| 测试用例 | evidence/b04/test_report.json + test_case_c1~c10.json(资料存在/缺失两类) |
| 校准与构建证据 | evidence/b04/threshold_calibration.json、build_*.json、retrieval_*.json、rag_answer_*.json |
| 本文档 | docs/b04_rag.md |
