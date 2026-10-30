# GreenMate · 番茄大棚环境监测与多源证据判断系统

> 把「瞬时读数」升级为「可追溯、可复现、会说"有些话现在不能说"」的环境判断系统

## 1. 它解决什么问题

| 项 | 内容 |
|---|---|
| 给谁用 | 番茄大棚种植者与农业环境监测学习者(课程/竞赛演示场景) |
| 在什么场景 | 大棚环境数据(温度/湿度/CO₂/光照)需要被持续记录、对比、判断时:种植者打开系统查看当前状态、状态如何演化、判断依据是什么 |
| 解决什么 | 不用它:只有瞬时读数,无法区分"稳定 30℃"和"升温到 30℃",也无法追溯"为什么这样判断";用了它:历史时序 + 外部天气 + 专业知识 + 短时预测四类证据各自自报身份,结构化判断可保存、可对比、可复现,证据缺失时明确收窄判断边界而不是编造 |
| 明确不做什么 | 不做疾病诊断;不接真实传感器(数据为模拟器生成);不自动控制设备(只输出判断与解释);不做长期天气预报 |

## 2. 核心功能

| 功能 | 当前状态 | 一句话说明 |
|---|---|---|
| B01 数据采集(CLI/Web/A-B 验证) | 已实现 | 采集 4 项环境参数并校验,原始数据按唯一 run_id 持久保存 |
| B02 数据记忆(SQLite + Chart.js 可视化) | 已实现 | 30 分钟网格 7 天时序,重启保留,时间窗查询/统计/趋势 |
| B03 天气证据(真实 API) | 已实现 | Open-Meteo 真实预报 + 失败验证;QWeather 鉴权失败测试需自有 Key(部分实现) |
| B04 专业知识 RAG | 已实现 | 自编番茄知识库,零依赖字符向量检索,资料不足时拒答 |
| B05 工具智能体闭环 | 已实现 | LLM tool_calls 决策 + Python 真实执行 + 强制调用日志 |
| B06 短时预测(传统 ML) | 已实现 | 3h 窗口预测未来 1~2h WATCH/NORMAL,基线对比,模型可重载 |
| B07 系统组装(四视图 + 可复现实验框架) | 已实现 | 统一 run_id,证据四分类与可信降级,S1~S5 场景 + 6 组实验 |
| B08 契约校验 | 已实现 | run_result.json 五项校验 + sha256 稳定性指纹 |
| B09 整合门户(数字孪生 3D) | 已实现 | 三页统一首页,CSS 3D 大棚孪生联动实时数据,serve 一键起三服务 |
| MCP 外部工具连接 | 未实现 | 规格列为非必需扩展,未接入 |
| 真实传感器接入 | 未实现 | 设计边界:数据为课程模拟器生成,结论不声称真实监测 |

## 3. 10 分钟快速复现

> 全部命令在 Windows 上验证(实测版本见第 4 节);`py` 为 Python 启动器。

### 路线 A:完整模式(真实天气 + LLM,可选)

```bash
git clone https://github.com/Wang-star-web/nova-GreenMate.git
cd nova-GreenMate
py -m pip install pandas scikit-learn joblib requests   # 依赖仅 4 个第三方包
copy .env.example .env        # 可选:填入 DEEPSEEK_API_KEY 启用 LLM 功能
py b02_memory.py generate     # 生成 7 天模拟时序数据(本机实测 0.88 秒)
py b07_main.py run S1         # 跑通完整链路:8 次工具调用 → 证据 → 判断 → 报告
py b09_portal.py serve        # 一键起三服务:门户 8300 + B01 8123 + B02 8223
```

期望结果:终端输出 `✓ S1 稳态基线 → 结论=正常 关注级=normal 信任=高 降级=0`,并生成 `report/run_*.html`;浏览器打开 http://127.0.0.1:8300/ 看到整合首页(数字孪生 + 报告列表)。

### 路线 B:最小可跑模式(无密钥 / 无外网)

```bash
git clone https://github.com/Wang-star-web/nova-GreenMate.git
cd nova-GreenMate
py -m pip install pandas scikit-learn joblib requests
py b02_memory.py generate
py b02_memory.py check                          # 质量检查 10/10
py b07_main.py run S1                           # 天气断网时如实降级
py b07_main.py selftest                         # 校验函数自测,零 LLM 零网络
py b08_verify.py --json-path evidence/b07/<最新run_id>/run_result.json
py b09_portal.py serve --portal-only            # 只起门户(或直接双击 web/index.html)
```

降级后的能力:LLM 解释显示 `[解释] status=omitted reason=no_api_key`(结构化判断不受影响);天气证据显示 `network_timeout` 并标注【证据缺失】、信任降级为「低」;其余全部功能(时序、检索、预测、判断、报告、门户)照常。**不会白屏、不会崩溃。**

## 4. 环境与依赖

| 软件 | 要求版本 | 检查命令 | 我实测的版本 |
|---|---|---|---|
| Python | ≥ 3.12(依赖 statistics.linear_regression) | `py --version` | 3.12.10 |
| pandas | ≥ 2.0(3.x 已适配) | `py -c "import pandas; print(pandas.__version__)"` | 3.0.6 |
| scikit-learn | ≥ 1.5 | `py -c "import sklearn; print(sklearn.__version__)"` | 1.9.1 |
| joblib | ≥ 1.4 | `py -c "import joblib; print(joblib.__version__)"` | 1.6.0 |
| requests | 2.x | `py -c "import requests; print(requests.__version__)"` | 2.34.2 |
| 浏览器 | Chrome/Edge 较新版本(CSS `:has()` 与 3D 变换) | — | Chrome 系 |

依赖清单:安装命令见上(`requirements.txt` 仅记录 requests,其余三包按上表版本实测;无 package-lock 类文件,Python 项目不适用)。

## 5. 环境变量

模板文件 `.env.example` 已在仓库根目录(参赛包 `03_复现指南/` 内为同内容副本)。全部变量**可选**,不填走诚实降级:

| 变量名 | 是否必填 | 作用 | 不填会怎样 |
|---|---|---|---|
| `DEEPSEEK_API_KEY` | 可选 | LLM 功能主密钥(B01 问答/B04 回答/B05 智能体/B07 解释) | 全部 LLM 路径如实报 `no_api_key` 并标【证据缺失】;结构化判断、天气、检索、预测不受影响 |
| `B03_LOCATION` | 可选 | 天气默认地点(北京/上海/广州 或 lat,lng) | 按默认「北京」处理 |
| `B01/B02/B09_WEB_PORT` | 可选 | Web 服务端口(默认 8123/8223/8300,只绑 127.0.0.1) | 使用默认端口;占用时打印提示并返回,`serve` 自动探测跳过 |
| `B0N_LLM_BASE_URL/MODEL/TIMEOUT` | 可选 | 各模块 LLM 端点与超时(默认 DeepSeek) | 使用默认值 |
| `B03_QWEATHER_API_KEY` | 可选 | QWeather 备用接口(仅鉴权失败测试) | qweather 调用如实报 api_key_error(即 B03 失败验证用例) |

```bash
copy .env.example .env      # Windows;Linux/macOS 用 cp .env.example .env
# 然后按注释填入你自己的值
```

## 6. 复现实测记录

| 项 | 内容 |
|---|---|
| 测试人 | 本人(汪语含) |
| 机器 | Windows 11 Home 10.0.26100;Python 3.12.10 |
| 是否首次 | 本机已安装过本项目依赖(如实说明);10 分钟实测记录见参赛包 `03_复现指南/10分钟实测记录.md` |
| 测试日期 | 2026-10-31 |
| 总耗时 | 核心步骤合计 18.4 秒(逐步骤计时见参赛包实测记录;首次 pip 安装视网络而定,本机已安装未计入) |

> 完整步骤计时表(含卡点与失败修复)见参赛包 `03_复现指南/10分钟实测记录.md`,本节与之一致。

## 7. 验收清单(跑起来该看到什么)

| 验收项 | 操作 | 期望 | 截图文件 |
|---|---|---|---|
| 数据生成 | `py b02_memory.py generate` + `check` | 终端输出 336 点生成摘要;`通过 10 / 10` | `docs/screenshots/01-generate-check.png` |
| 完整链路 | `py b07_main.py run S1` | 输出 `结论=正常 关注级=normal 信任=高 降级=0`,生成 `report/run_*_report.html` | `docs/screenshots/02-run-s1.png` |
| 四视图报告 | 打开 `report/run_*_report.html` | 四视图齐全;证据表含 类型/来源标记/自报时间/支持-限制 | `docs/screenshots/03-report-views.png` |
| 数字孪生 | `py b09_portal.py serve` 后打开 8300 门户与 8123 状态 B | 3D 孪生动画;切换 B 后红太阳/浓雾/暗光/关注脉冲环 | `docs/screenshots/04-twin.png` |
| 异常/降级 | 无 Key 且断网执行 `py b07_main.py run S1` | LLM `omitted(no_api_key)`、天气 `network_timeout`【证据缺失】、信任降「低」,不崩溃 | `docs/screenshots/05-degrade.png` |
| 可复现实验 | `py b07_main.py exp-all` + `check-all` | 6 组 `run_<type>_001` 落盘;check-all 输出 6 行 0 FAIL | `docs/screenshots/06-exp-all.png` |
| 契约校验 | `py b08_verify.py --json-path …` | `2 OK / 3 本轮预期缺失`,两次运行 sha256 一致 | `docs/screenshots/07-b08-verify.png` |
| 数据留痕 | `py b01_cli.py web` 提交采集 | `evidence/raw/run_*.json` 生成且页面历史出现该条 | `docs/screenshots/08-collect.png` |

> 截图目录 `docs/screenshots/` 由本人在交稿前按上表逐项拍摄并提交(文件名固定)。

## 8. 排错 FAQ

**Q:终端打印 ℃/CO₂ 等字符时崩溃(`UnicodeEncodeError: 'gbk' codec can't encode`)?**
- 原因:Windows 控制台管道默认 GBK 编码。
- 解决:各模块入口均先执行 `setup_console()` 强制 UTF-8;若单独脚本重定向仍报错,在脚本开头加 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`。

**Q:`py b02_memory.py web` 执行了但页面打不开?**
- 原因:服务未启动或端口被占用时静默无反馈;门户 iframe 依赖服务运行。
- 解决:统一用 `py b09_portal.py serve` 一键同起三服务(自动探测端口占用);单独启动时核对终端是否打印了「页面地址」横幅。

**Q:B06 训练或推理结果与文档记录不一致?**
- 原因:pandas 3.x 的 `shift()` NaN 语义与旧版不同,窗口尾部标签处理方式变化。
- 解决:本项目已改为按行位置判定窗口完整性(见 B06 源码 `build_dataset` 注释);如自行升级依赖版本请以 `py b06_ml.py eval` 输出为准。

**Q:格式串报错 `unsupported format character`(如 "%RH")?**
- 原因:Python `%` 格式化把 `%R` 当占位符。
- 解决:字面量百分号写成 `%%`(本项目已修复,新代码沿用此约定)。

**Q:LLM 调用被拦截(发送 ANTHROPIC_AUTH_TOKEN 到 DeepSeek)?**
- 原因:本机 Claude Code 的权限分类器禁止把自身凭据外传到第三方端点(凭据安全边界,属预期)。
- 解决:显式设置 `DEEPSEEK_API_KEY` 使用自己的密钥;验证脚本可用 `DEEPSEEK_API_KEY= ANTHROPIC_AUTH_TOKEN=` 前缀走无 Key 降级路径。

**Q:门户 404 或图表脚本 404?**
- 原因:门户静态服务只映射 `/`、`/report/*`、`/b02_assets/*` 三类路径;错误消息曾含中文导致 latin-1 编码异常(已改为 ASCII)。
- 解决:确认访问路径在上述映射内;图表资产必须保留 `b02_assets/chart.umd.js`。

## 9. AI 协同说明

| 项 | 内容 |
|---|---|
| 使用的工具 | Claude Code(CLI 版,模型 Claude 系) |
| 怎么用的 | 由我拆解任务与验收标准(每模块一条含硬性约束的规格消息),AI 生成初版实现,我运行验证后逐处纠偏(见参赛包 `02_AI对话快照/` 3 个案例) |
| 对话记录 | 原始会话转录存于本机 Claude Code 项目目录,导出摘录见参赛包 `02_AI对话快照/`;关键截图与提交对应关系见 `01_Git提交日志/commit-index.md` |
| 人工把关 | 涉及阈值判定、数据降级、凭据处理的代码均要求 AI 先说明方案再由我确认;每次改动必须跑通对应模块的自检/验证命令后才算完成;AI 生成内容中禁止出现未调用的数据源断言(由 B07 的 LLM 校验函数强制) |

## 10. 目录结构

```
nova-GreenMate/
├── b01_cli.py ~ b09_portal.py   # 九个模块:采集/记忆/天气/RAG/智能体/预测/组装/校验/门户
├── docs/                        # 各模块说明文档 + b07_pass23(实验框架)+ req/(参赛规范原文)
├── data/                        # environment.db(SQLite,由 py b02_memory.py generate 生成)
├── evidence/                    # 运行证据:raw/各模块证据/b07 实验存档
├── report/                      # 四视图 HTML 报告
├── models/                      # B06 模型(joblib + 特征列表 + 配置)
├── testdata/                    # S1~S5 场景与 6 组实验预设(JSON)
├── web/                         # 整合门户首页(由 py b09_portal.py 生成)
├── .env.example                 # 环境变量模板(全部可选)
└── requirements.txt             # 依赖清单
```

## 11. 已知限制

| 限制 | 影响 | 后续打算 |
|---|---|---|
| 环境数据来自模拟器,非真实传感器 | 结论不能外推到真实大棚环境 | 不会补,属设计边界(系统明确标注 source_type=SIMULATED) |
| LLM 功能依赖外部 API 与密钥 | 无 Key 时解释/问答功能降级(其余功能不受影响) | 不会补,降级路径即设计的一部分 |
| 天气证据依赖外网 | 断网时天气证据缺失、判断边界收窄 | 不会补,如实降级是验收点 |
| 单机运行,无多用户并发 | 无法多端同时采集 | 视后续需求 |
| 知识库为课程自编资料(5 份) | 建议仅作参考,不构成专业农艺结论 | 视后续需求替换真实资料 |

---

**模块技术细节**:B01 说明见 `b01_cli.py` 头部注释;其余模块文档见 [docs/](docs/)(b02_schema / b03_README / b04_rag / b05_agent / b06_ml / b07_main / b07_pass23 / b08_verify)。参赛规范原文见 [docs/req/](docs/req/)。
