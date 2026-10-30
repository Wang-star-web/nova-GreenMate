# B08 契约校验模块 — run_result.json 契约 / 校验细则 / 稳定性说明

实现:`b08_verify.py`(只读验证器,零第三方依赖,不 import 任何 B01~B07 模块)。
输入:B07 每个 run 落盘的 `证据/b07/{run_id}/run_result.json`。

## 契约 schema(B07 产出)

```json
{
  "run_id": "run_20261030_014752_59f5a7",
  "scenario_id": "S1",
  "generated_at": "2026-10-30T01:47:54+08:00",
  "raw": {
    "status": "ok",
    "path": "证据/raw/run_20261030_014752_59f5a7.json",
    "record": {
      "run_id": "...", "collect_time": "2026-10-28T12:00:00+08:00",
      "source_type": "SIMULATED",
      "data": {"temperature": {"value": 27.6, "unit": "℃"}, "...": "4 参数"},
      "valid_ranges": {"temperature": {"min": 5.0, "max": 40.0, "unit": "℃"}, "...": "4 参数"}
    }
  },
  "parsed": {
    "status": "ok",
    "source": "b02_memory environment_samples",
    "anchor": {"sample_time": "2026-10-28 12:00:00", "source_type": "SIMULATED",
               "temperature": 27.6, "humidity": 54.0, "co2": 923.0, "light": 59910.0},
    "history_count": 49,
    "history": ["49 行,与 anchor 同形状,升序,末行==anchor"],
    "grid_step_minutes": 30
  },
  "rule": null, "ml": null, "final": null
}
```

| 字段 | 说明 |
|---|---|
| raw | B01 原始采集记录(回读 证据/raw 盘上真实文件;status=missing 时 record=null,锚点缺失或保存失败) |
| parsed | B02 解析后数据:锚点行 + 历史窗口行(嵌入 query_range 原始行,不加工) |
| rule / ml / final | 本轮 B07 未生成这三类模块输出,显式 `null` 占位(契约面可见) |

不变量:锚点存在 ⇒ history 必非空且末行==锚点行(query_range 闭区间含锚点)。
体积约 12 KB(49 行历史),b08 零依赖 DB、自包含可校验。

## 五项校验细则

### ① raw 原始数据(不在豁免名单)

判定顺序(首项失败即返回):

1. 键不存在 / null → `✗ 缺失`("raw 不在豁免名单,契约要求必须存在");
2. 非对象 → `⚠ 异常`;
3. `status` ∉ {ok, missing} → `⚠ 异常`;
4. `record` 为 null:status=missing → `✗ 缺失`;status=ok → `⚠ 异常`(声明矛盾);
5. `record` 非对象 → `⚠ 异常`;
6. `record.run_id` 非字符串,或与顶层 run_id 不一致(顶层 run_id 缺失时跳过一致性并说明)→ `⚠ 异常`;
7. `collect_time` 不可按带时区 ISO 解析(`datetime.fromisoformat`)→ `⚠ 异常`;
8. `source_type` 非空字符串 → `⚠ 异常`;
9. `data` 4 参数逐项:`{value: 数字(排除 bool), unit: 非空字符串}`,失败定位到参数 → `⚠ 异常`;
10. `valid_ranges` 4 键各含数字 min/max → `⚠ 异常`;
11. 全部通过 → `✓ OK`。

附注(不计状态):盘上 `证据/raw/{run_id}.json` 是否存在、与嵌入 record 内容是否一致。

### ② parsed 解析后数据(不在豁免名单)

1. 键不存在 / null → `✗ 缺失`;
2. 非对象 / status 非法 → `⚠ 异常`;
3. **声明一致性**:status=missing 且 anchor=None 且 history=[] → `✗ 缺失`;status=missing
   但有数据,或 status=ok 但 anchor=None/history 为空 → `⚠ 异常`;
4. anchor 形状:sample_time 按本地网格串 `%Y-%m-%d %H:%M:%S` 解析、source_type 非空、
   4 参数为数字 → 否则 `⚠ 异常`;
5. history 为数组且 `history_count == len(history)` → 否则 `⚠ 异常`;
6. 逐行同形状(失败定位到行号)→ `⚠ 异常`;
7. sample_time 严格升序 → 否则 `⚠ 异常`;
8. **末行 == anchor 行**(dict 全等)→ 否则 `⚠ 异常`(锚点缺失时该比对已被声明检查覆盖);
9. 网格步长与 `grid_step_minutes` 不一致 → 仅注记不判错;`source`/`grid_step_minutes`
   缺失 → 仅注记。

### ③④⑤ rule / ml / final(豁免名单:缺失即「本轮预期缺失」)

| 输入形态 | 结论 |
|---|---|
| 键不存在 | `◌ 本轮预期缺失`("键不存在") |
| null 占位(B07 本轮输出) | `◌ 本轮预期缺失`("null 占位") |
| `{}` 或 `""` | `◌ 本轮预期缺失`("空占位") |
| 非空非对象(字符串/数组/数字) | `⚠ 异常` |
| 非空对象 | `✓ OK`("宽松通过,结构校验待后续轮次细化";含 run_id 且与顶层不一致 → `⚠ 异常`) |

行为约定:**rule/ml/final 缺失不抛异常、不判定为错误**;raw/parsed 不在豁免名单。

## sha256 与校验指纹(稳定性)

- 每次运行输出 `文件 sha256: <64 hex>` + `短哈希: <12 hex>`,与 `sha256sum` 可直接对照;
- 「校验指纹」= sha256 over 五项清单行(`seq|item|status|digest`)+ 文件哈希,**不含校验时间**;
- 约定:重复运行同一文件,sha256 与校验指纹一致即确认结果稳定可复现;文件被改动
  (即使只是重排版或 rule 由 null 改为 {})→ 哈希与指纹均变化。

## 退出码与只读约定

- 退出码恒 0:校验清单结论(缺失/异常)不等于脚本错误;任何输入问题(文件不存在/
  JSON 损坏/非法参数)都转友好中文提示,绝不 traceback;
- 脚本只读,不写任何文件。

## 运行示例

```bash
python b08_verify.py --json-path 证据/b07/<run_id>/run_result.json
python b08_verify.py --json-path=<路径>          # 等号形态等价
python b08_verify.py                            # 不带参数:用法 + 可用文件列表
python b08_verify.py --help
```

正常输出摘要(实测):

```
  [1/5] raw 原始数据       ✓ OK  盘上 证据/raw/run_….json 存在且与嵌入 record 逐字节内容一致
  [2/5] parsed 解析后数据   ✓ OK  30 分钟网格一致
  [3/5] 规则输出           ◌ 本轮预期缺失  null 占位(本轮 B07 未生成规则输出)
  [4/5] 模型输出           ◌ 本轮预期缺失  null 占位(本轮 B07 未生成模型输出)
  [5/5] 最终结论           ◌ 本轮预期缺失  null 占位(本轮 B07 未生成最终结论)
汇总: 2 OK / 3 本轮预期缺失 / 0 缺失 / 0 异常 (共 5 项)
```

## 负向用例(实测 27/27 通过)

| 类 | 用例 | 预期 |
|---|---|---|
| 输入错误 | 文件不存在 / 路径是目录 / JSON 损坏 / 顶层非对象 / 非 UTF-8 | ✗ 友好提示,退出码 0 |
| raw | 键删除或 null / record 非对象 / run_id 不一致 / collect_time 非法 / data 缺参数 / value 非数字 / status=ok∧record=null 矛盾 | ✗ 缺失(前 2)/ ⚠ 异常(其余) |
| parsed | 键删除 / {} / status=missing∧空 / status=ok∧history 空 / count 不符 / 乱序 / 末行≠anchor / anchor 缺参数 | ✗ 缺失(前 3)/ ⚠ 异常(其余) |
| 顶层 | run_id 缺失 | 头部 ⚠ + raw 跳过一致性,其余照常 |
| rule/ml/final | 键不存在 / null / {} / 有效 dict / "abc" | ◌×3 / ◌ / ◌ / ✓ OK / ⚠ 异常 |
| 稳定性 | 同文件两次运行;改动文件(rule null→{}) | 哈希与指纹一致;均变化 |
| 参数 | 无参 / 未知参数 / 裸位置参数 | 友好提示,退出码 0 |

## 与旧六产物契约的关系

旧契约(证据/raw|parsed|rule|ml|final + report HTML,位置参数 run_id)**已废弃**:
旧 证据/parsed|rule|ml|final 目录已不存在;证据/raw 与 报告/*.html 为历史
产物,新 b08 唯一输入是 `--json-path` 指定的 run_result.json,不扫描它们、无需兼容。
旧 run_id 无 run_result.json 时,无参扫描不列出,重跑 B07(如 `py b07_main.py run S1`)
即可生成。

## 交付资产

| 资产 | 路径 |
|---|---|
| 契约校验脚本 | b08_verify.py(五项校验 + sha256 + 校验指纹,只读,退出码恒 0) |
| 契约输入 | 证据/b07/{run_id}/run_result.json(由 B07 步骤⑥ 落盘) |
| 本文档 | 文档/b08_verify.md |
