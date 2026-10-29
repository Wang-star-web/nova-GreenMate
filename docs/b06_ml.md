# B06 传统机器学习闭环模块 — 窗口化 / 特征 / 切分 / 持久化说明

实现:`b06_ml.py`(pandas + scikit-learn + joblib;无深度学习)。数据源:B02
`data/environment.db`(**只读连接 mode=ro**,任何写入直接报错——不改动 B02 原始数据)。

## 预测任务与 DERIVED_LABEL

- 输入窗口:最近 3 小时 = 6 个 30 分钟采样点;预测窗口:未来 1~2 小时 = 4 个采样点。
- 标签 `next_window_attention`:预测窗口内任一采样点违反适宜带(温度 15~30 ℃、
  湿度 45~85 %RH、CO₂ 800~1500 ppm;光照仅白天 6≤h<18 检查 10000~70000 lux,
  夜间 light=0 属正常)→ WATCH,否则 NORMAL。
- **来源标注**(全项目固定引用):`DERIVED_LABEL: 规则派生(阈值表 b03_rules.json R1~R8),
  源=environment_samples 原始参数值,非 sim_scenario`。
- **sim_scenario 结构性禁用**:SQL SELECT 列清单只含 sample_time + 4 参数
  (sim_scenario 从不被读取);特征名不含 scenario/weather;标签由原始未来值派生。
- 窗口完整性按行位置判定:输入窗口不足(前 5 行)与预测窗口不足(库尾 4 行)的锚点
  不打标签,仍可作为推理输入(接口对输入窗口完整性做同等校验)。

## 特征(恰好 26 个,与模型一起保存)

| 组 | 特征 |
|---|---|
| 4 参数 × 6 | `{param}_current`(末点)、`_mean`、`_std`(ddof=1)、`_min`、`_max`、`_delta`(末−首) |
| 时间 × 2 | `hour_sin` / `hour_cos`(锚点小时正弦/余弦,午夜自然连续) |

## 切分与防泄露

- 时间边界:`2026-10-27 01:30`(前 5 天训练 235 窗 / 后 2 天测试 92 窗)。
- 运行时 3 项断言(结果写入 train_report.json 的 leak_checks):
  ① 训练锚点全部早于测试锚点;② 训练/测试锚点集合不相交;③ 特征行哈希无交集
  (测试行绝不参与训练)。
- 如实说明:5 个训练窗(锚点 10-26 23:00 ~ 10-27 01:00)的**标签**引用了边界后样本值
  (仅作监督标签,绝不作为特征)——walk-forward 标准实践,config.json 的
  `label_boundary_note` 中标注。

## 基线与指标(实测)

| 模型 | accuracy | macro-F1 | WATCH recall | 混淆矩阵 |
|---|---|---|---|---|
| 基线(多数类 DummyClassifier) | 0.6739 | 0.4026 | 0.0000 | [[62,0],[30,0]] |
| RandomForest(n=200, seed=42, balanced) | **0.8696** | **0.8600** | **0.9333** | [[52,10],[2,28]] |

判定规则:模型 macro-F1 必须超过基线(0.8600 > 0.4026 ✓)。确定性:random_state=42、
无洗牌、SQL ORDER BY,`eval` 重载后重算指标与训练报告逐位一致。

## 错误样本分析(evidence/b06/error_analysis.json)

程序化选取(P 极值,并列取最早锚点)+ 程序化中文解释:

- **假阳性** 锚点 2026-10-28 09:00(P(WATCH)=0.84):输入窗 06:30~09:00 含黎明弱光
  (3048 lux 白天低于 10000),模型从训练集"弱光→WATCH"模式误判;预测窗 09:30~11:00
  全部适宜。
- **假阴性** 锚点 2026-10-27 11:00(P=0.32):输入窗全部适宜,预测窗 13:00 起
  disturbance_multi 突变(32.8℃/87.5%/7972 lux)——突变尚未出现在输入窗,属时间
  序列前推的固有局限。
- 计数:FP 10 / FN 2。

## 模型保存与重载(重启证明)

- `models/b06/model.joblib`(joblib:模型 + 特征列表 + 配置)、`feature_list.json`、
  `config.json`(版本/窗口/切分/标签规则/指标/库版本)。
- 版本自动递增 `B06-ML-vN`;`load_model()` 三文件齐全校验 + 特征列表交叉核对。
- `eval`/`predict` 只加载不训练:完全重启后新进程直接推理
  (验证命令见下,已实测指标一致)。
- joblib 为 pickle 格式,仅加载自产 models/b06/ 文件;train/eval/predict 串行运行
  (Windows 文件锁)。

## 接口契约(B07 复用)

```python
predict_next_attention(sample_time=None) -> dict
# {"prediction": "NORMAL"|"WATCH", "probability": {...},
#  "model_version": "B06-ML-v3", "MODEL_OUTPUT": "B06_ML", "generated_at": ...,
#  "window": {"start","end","points":6}, "features": {...26 键...},
#  "label_source": "DERIVED_LABEL: ...", "status": "ok"|"error", "error": null|{...}}
```

`MODEL_OUTPUT="B06_ML"` 为独立证据标记(供 B07 识别模型来源)。错误变体(同形状,
prediction/probability/features 为 null,退出码恒 0):`model_missing` /
`model_corrupt` / `insufficient_history`(不足 6 个连续 30 分钟网格样本)/
`db_unavailable` / `bad_sample_time`。

## 复现命令

```bash
py b06_ml.py train            # 训练(预期指标见上表;版本自动递增)
py b06_ml.py eval             # 新进程重载重算 → 指标与训练报告一致
py b06_ml.py predict          # 默认最新样本
py b06_ml.py predict --sample-time "2026-10-28 20:00"
py b06_ml.py info
```

## 交付资产

| 资产 | 路径 |
|---|---|
| 窗口化脚本 + 特征定义 | b06_ml.py:`build_dataset` / `feature_names`(26 特征) |
| 训练测试切分脚本 | b06_ml.py:`split_dataset`(时间边界 + 3 项防泄露断言) |
| 基线对比代码 | b06_ml.py:`cmd_train`(DummyClassifier vs RandomForest) |
| 保存/重载推理代码 | models/b06/model.joblib + feature_list.json + config.json;`load_model` / `predict_next_attention` |
| 错误样本分析 | evidence/b06/error_analysis.json(FP/FN 各 1 例 + 计数) |
| 训练证据 | evidence/b06/train_report.json、eval_report.json、prediction_*.json |
| 本文档 | docs/b06_ml.md |
