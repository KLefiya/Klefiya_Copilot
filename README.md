# CarveOps Copilot

面向企业**并购剥离（carve-out）/ ERP 重建**场景的多智能体辅助项目。用 LangGraph + MCP 搭三个 Agent：数据迁移映射、Fit-to-Standard 差异分析、Cutover/RAID 治理。

定位是**轻量、可解释的教学 / 作品集级实现**，不与 SAP 生态的企业级商业产品（Syniti/ADMM、SAP Cloud ALM）竞争。

---

## 数据安全与"离线"的准确定义

这个项目对"离线 / 合规"的承诺是具体的，下面逐条说明它保证什么、不保证什么。

**保证：**

- **不连接任何真实 SAP 系统。** 代码里没有任何 SAP 系统的连接串、主机名、客户端号或凭据。
- **不调用任何真实 SAP API。** `schemas/` 下的字段结构参考是**人工整理**自 api.sap.com 的公开接口文档，不是从任何 SAP 实例导出的，运行时也不会去请求 SAP 的服务。
- **不接触任何真实客户数据。** `data/legacy/` 下的全部数据由 `src/tools/generate_legacy_vendors.py` 用 Faker 合成，公司名、地址、税号、银行账号均为伪造值，不对应任何真实实体。
- **不外传任何真实数据。** 项目里根本不存在真实数据可供外传。模块一的全部工具读写本地文件，不向任何外部服务发送任何内容。模块二的 Fit/Gap 判定环节会调用 Anthropic API——发出去的是**合成访谈文本与自撰知识库片段**，详见下面的说明。

**LLM 判定环节的数据边界（模块二）：**

`src/tools/gap_analysis.py` 用 Claude 判定需求属于 Fit / Configuration / Enhancement / Development。首次运行会把两类内容发送给 Anthropic API：

1. `data/synthetic/interview_notes.json` 里的合成访谈笔记——虚构的 NewCo 剥离场景，虚构的人名与外部系统名，不对应任何真实公司、项目或人；
2. `data/knowledge/standard_processes.json` 里我自己撰写的 SAP 标准流程知识条目。

红线保护的是**真实客户数据**，而这里处理的是纯合成数据，因此不违反红线初衷。但"不外传任何数据"这句话字面上不再成立，所以上面改成了"不外传任何真实数据"——这不是文字游戏，是把承诺收窄到它实际能兑现的范围。

**这次调用只发生一次。** 每次 LLM 调用按请求指纹 sha256 缓存进 `data/synthetic/llm_cache/`，该目录**提交进 git**。缓存填充后，重跑完全离线且字节一致：

```bash
export ANTHROPIC_API_KEY=sk-ant-...        # 仅首次需要
python src/tools/gap_analysis.py           # 抽取 + 判定，写入缓存
python src/tools/gap_analysis.py --offline # 只读缓存；缓存缺失即报错，绝不静默联网
```

模型是 `claude-sonnet-5`，**不传 `temperature`**。该模型已移除采样参数，非默认的 `temperature` / `top_p` / `top_k` 会返回 400；退回 `claude-sonnet-4-6` 又不支持结构化输出，而判定结果的 `category` / `confidence` / `evidence` / `rationale` 四件套依赖它。可复现性由磁盘缓存承担——`temperature=0` 在任何模型上都从未保证过逐字一致，它只是降低方差。

**一个需要说清楚的例外——embedding 模型的一次性下载：**

`src/tools/field_mapping.py` 依赖 sentence-transformers 的 `all-MiniLM-L6-v2` 模型（约 80MB）。**首次运行时会从 HuggingFace Hub 下载该模型并缓存到本地**，之后所有运行都从本地缓存加载，可在完全断网的环境下工作。

这次下载是**环境准备**，不是数据外传：传输方向是"模型权重从 HuggingFace 下载到本机"，项目数据不会离开本机。它不违反上述任何一条边界。

如果你的环境需要严格审计，可以把下载与运行分离：

```bash
# 1) 在允许联网的环境里，一次性拉取模型
python scripts/prefetch_model.py

# 2) 在断网环境里验证缓存可用（强制 HF_HUB_OFFLINE=1 加载）
python scripts/prefetch_model.py --check
```

模型缓存默认落在 `~/.cache/huggingface`（可用 `HF_HOME` 改）。设 `HF_HUB_OFFLINE=1` 可强制禁止任何 Hub 网络请求。

**商标声明：** SAP、S/4HANA 及其它 SAP 产品名称为 SAP SE 的商标。本项目是独立的教学性实现，与 SAP SE 无关联，也未获其背书。

---

## 为什么用合成数据，而不是公开的实体解析基准集

做实体解析（entity resolution）时，用 Leipzig record linkage benchmark、MusicBrainz、DBLP-Scholar 这类公开基准集是常见做法。这个项目没有这么做，原因是它们解决不了这里的核心问题。

**公开基准集不是 SAP 字段结构。** 本项目的重点根本不是"两条记录是不是同一个实体"这个通用问题，而是**遗留字段如何映射到 SAP A2X 的目标 schema**：`vendor_name` 该落到 `OrganizationBPName1` 还是 `BusinessPartnerName`？`OrganizationBPName1` 只有 40 字符，超长的公司名该溢出到 `Name2` 还是截断？`Country` 是 `CHAR(3)`，`"United States"` 进不去。这些约束只存在于 SAP 的目标 schema 里，任何通用基准集都不具备。

**公开基准集不体现跨国主数据的真实形态。** 德国公司的 `GmbH & Co. KG` 后缀、日本的 `K.K.`、`NNN-NNNN` 邮编、`+49` 电话格式、各国互不相同的税号规则——这些是 carve-out 场景中数据质量问题的主要来源，也是"格式一致性检测必须按国家分组"这个结论的由来。MusicBrainz 里没有这些。

**合成数据同时给了两样东西：精确的 ground truth，和零真实实体的合规性。** `generate_legacy_vendors.py` 在生成脏数据的同时输出 `legacy_vendors_ground_truth.json`（`record_id → 真实实体 id`），Splink 的 precision/recall 可以精确计算，不依赖人工标注，也不受基准集自身标注噪声的影响。而且每一条记录都是伪造的，不存在任何真实公司、真实地址、真实税号——这让"不接触真实客户数据"从一句声明变成了结构上的事实。

代价是合成数据的脏法是我们自己设计的，可能不覆盖真实世界的全部脏法。这是自觉接受的权衡：本项目要演示的是**方法与可解释性**，不是刷某个基准集的分数。

---

## 字段结构参考的核实状态

`schemas/` 下的两个文件不是凭记忆写的。每个字段带 `verification_status`：

- `verified` —— 已对照**一手 `$metadata`** 逐字段核对一致（类型、`MaxLength`、`Nullable`、`sap:creatable` / `sap:updatable`）。来源 EDMX 的仓库、URL 与 sha256 记录在各文件的 `_revision_log` 里，均为公开可访问、无需登录。
- `unverified` —— 该字段（或其所属实体）不在核对所用的 metadata 快照中，标注仍来自人工整理。文件顶部的 `_verification.unverified_list` 列出全部这类字段。

当前：Business Partner 84/87 verified，Product 61/77 verified（未核实的主要是 `A_MaterialStock` / `A_MatlStkInAcctMod`，它们属于另一个 OData 服务 `API_MATERIAL_STOCK_SRV`）。

**核对是有代价的，也确实抓到了错。** 首轮凭记忆整理的标注里，`ProductOldID` 被标成 18 位（实为 40），`Brand` 标成 2（实为 4），`StandardPrice` / `MovingAveragePrice` 的精度标成 `(11,2)`（实为 `(12,3)`），四个 MRP 字段被错放进 `A_ProductPlant`（实际属于 `A_ProductSupplyPlanning` / `A_ProductPlantProcurement`），`ValidityStartDate` / `ValidityEndDate` 类型标成 `Edm.DateTime`（实为 `Edm.DateTimeOffset`），`BankName` / `SWIFTCode` 漏标了只读。

需要注意：这两份 metadata 都是**快照**（BP 来自集成测试 fixture，Product 来自 2019 年已归档的 iOS 示例），`verified` 的含义是"与该快照一致"，**不是**"与你所在 SAP release 一致"。真实项目请以自己系统的 `$metadata` 为准。

---

## 目录结构

```
carveops-copilot/
├── schemas/              # SAP 公开字段结构参考（人工整理自 api.sap.com 公开 API 文档）
├── data/legacy/          # 合成"遗留系统"脏数据
├── data/synthetic/       # 各工具输出的报告
├── src/tools/            # 各 MCP 工具
├── src/agents/           # 各 Agent
├── scripts/              # 环境准备脚本（模型预下载等）
├── notebooks/            # 演示
└── tests/
```

## 安装

```bash
pip install -r requirements.txt
```

`requirements.txt` 里的 `duckdb` 版本与 splink 的 salting bug 直接相关，**不要随意升级**，原因写在该文件的注释里。

## 当前已实现的工具（模块一：数据迁移映射）

| 顺序 | 工具 | 输出 |
| --- | --- | --- |
| 1 | `src/tools/generate_legacy_vendors.py` | `data/legacy/legacy_vendors.json` + ground truth |
| 2 | `src/tools/data_profile.py` | `data/synthetic/vendor_profile_report.json` |
| 3 | `src/tools/field_mapping.py` | `data/synthetic/vendor_field_mapping.json` |
| 4 | `src/tools/pre_migration_validation.py` | `data/synthetic/vendor_validation_report.json` |
| 5 | `src/tools/entity_resolution.py` | `data/synthetic/vendor_duplicate_report.json` |

按顺序运行即可复现全部产物：

```bash
python src/tools/generate_legacy_vendors.py
python src/tools/data_profile.py
python src/tools/field_mapping.py
python src/tools/pre_migration_validation.py
python src/tools/entity_resolution.py
```

迁移前校验把两个**正交**维度分开表达，不混为一谈：`semantic_match`（映射语义是否正确，沿用 field_mapping 的结论）与 `loadable`（目标字段是否可写入，取 `is_creatable or is_updatable`）。典型情形是 `created_date → CreationDate`：语义完全正确，但该字段 `sap:creatable` 与 `sap:updatable` 均为 false，只能作 lineage / 参考，不能记为"通过"。

## 实体解析：为什么它的 precision/recall 是 1.0，以及为什么这不算好消息

`entity_resolution.py` 用 Splink 4（Fellegi-Sunter 概率匹配）做 `dedupe_only`，在 224 条记录中识别出 **51 个疑似重复组**（覆盖 125 条记录），cluster 级 precision / recall / F1 **全部为 1.0000**。

**这个 1.0 不构成模型能力的证据，报告本身会这么说。** `evaluation.metric_validity.verdict` 的取值是 `not_informative`，理由写在报告里，也复述在这里：

- 生成器构造"变体重复"时只改写 `vendor_name` 与 `country`，其余字段**整条复制**。于是 `city` / `street` / `postal_code` / `currency` / `created_date` 在**每一对**真实重复记录中都逐字相同。
- 报告内置三个平凡基线做对照。`group_by_postal_code` 与 `group_by_street_and_created_date` 各自就能取得 F1 = 1.0000——**一句 `GROUP BY` 打平了整套概率模型**。
- 模型诊断（`_meta.training.model_diagnostics`）显示：10 个比较器的精确匹配层 `m` 全部退化为 1，7 个模糊匹配层（Jaro-Winkler ≥0.95 / ≥0.88、Levenshtein ≤2）**从未被观测到**。因为标准化之后，98 对真实重复的名称已经逐字相同，模糊匹配无事可做。Fellegi-Sunter 的概率加权在这份数据上根本没有被触发。

根因是**生成器只注入了可被标准化完全还原的格式变化**（大小写、空格、标点、`&`/`und`、法律形式后缀写法），没有注入任何字符级噪声。要让这套指标具备意义，需要在生成器中加入拼写错误、字符换位、地址缩写（`Straße` / `Str.`）、邮编错位等不可逆噪声。**在那之前，把 1.0 当成绩单展示是误导。**

把这条结论写进产物而不是藏起来，是这个项目的取舍：一个能自证"我的指标现在没有意义"的报告，比一个印着 1.0 的报告更有价值。

标准化预处理本身是真的：`vendor_name → name_norm / name_core / legal_form`，`country → ISO 3166-1 alpha-2`。`LEGAL_FORMS` 编码的是真实世界的公司法律形式（GmbH / K.K. / LLC …），与生成器的变体表重合是因为二者描述同一个客观事实，不是因为读了它。ground truth 只用于评估，不参与训练、阻断、阈值选择或聚类。

**关于 splink 的 salting bug：** 早前把 `estimate_u_using_random_sampling()` 的 `"Salting partitions must be specified"` 归因于 splink 4.0.16 + duckdb 的版本组合。在本机（splink 4.0.16 + duckdb 1.5.4，Windows / Python 3.12）**实测未复现**——完整比较器、`max_pairs` 到 1e7、带 seed，均正常。因此走标准训练路径（确定性规则估 λ → 随机抽样估 u → EM 估 m），没有为一个不存在的 bug 降级。若在别的平台重现该报错，退路记在 `requirements.txt` 的注释里。

## SAP 标准流程知识库（模块二的 RAG 检索底座）

`data/knowledge/standard_processes.json` 是 26 条**自行撰写**的合成知识条目，覆盖 P2P、O2C、R2R、master_data 四个业务域，外加一组 `cross_cutting` 条目承载"配置 / 应用内扩展 / 定制开发"的判定框架本身。每条描述标准流程是怎样的、标准覆盖哪些能力点、常见配置点是什么、什么情况通常超出标准。

**合规**：全部 26 条 `authorship` 均为 `self_authored`，没有从 SAP 官方文档批量抓取，没有整段照抄。`source` 字段全为 null——不引用未经核实的链接。字段结构保留了 `paraphrased_public_doc` 这个取值，供将来确有转述时标注来源。知识条目里不含任何"某需求 = Configuration"之类的判定结论，也不出现访谈笔记里的虚构外部系统名，判定留给下游组件。

**离线**：向量化复用模块一已有的 `all-MiniLM-L6-v2`（首次联网下载约 80MB，之后本地缓存，见上文"离线的准确定义"）。脚本显式编码后把向量交给 Chroma，避免 Chroma 默认的 embedding function 去下载它自己的 ONNX 模型。Chroma 是纯本地持久化，不外联。

```bash
python src/tools/build_knowledge_base.py                       # 建库 + 跑内置检索测试
python src/tools/build_knowledge_base.py --no-rebuild --query "..."   # 只检索
```

条目正文（`standard_process` / 三个列表字段）是英文，因为检索 query 和 MiniLM 都是英文；中文只出现在 `title_zh` 和说明字段，不参与编码。

**切分与检索**：每条条目切成 4 个 chunk（`overview` / `capabilities` / `configuration` / `beyond_standard`），26 × 4 = 104 个 chunk。整条编码会稀释信号——`"approval threshold"` 打中的是 configuration 段，`"external system interface"` 打中的是 beyond_standard 段。但按 chunk 检索会让同一条目的 4 个 section 包揽 top-3，因此检索默认**按条目去重**（`--no-dedupe` 可关闭对比）。

**产物**：`data/knowledge/knowledge_index_manifest.json` 记录条目数、chunk 数、模型、以及 `_run_info.content_sha256`（当前 `312ef55c4e81f3ba`，跨次运行稳定）。Chroma 持久化目录 `data/knowledge/chroma/` 是派生物，已加入 `.gitignore`；知识条目源文件进 git。

## 运行 UI

Web UI 分两层：`backend/`（FastAPI，**只读**已生成的 JSON 报告）和 `frontend/`（React + Vite）。

**后端不修改任何分析工具，也不导入它们，更不触发分析。** 它只做一件事：把 `data/synthetic/` 下各工具已经写好的报告读出来。触发分析是后续步骤。

### 安装

前后端依赖分开管理——分析工具不该为了跑起来而拖进一个 web 框架，API 也不该为了跑起来而拖进 torch。两份清单互不引用。

```bash
pip install -r backend/requirements.txt     # fastapi + uvicorn
cd frontend && npm install && cd ..
```

**Node 版本注意**：前端锁的是 **Vite 6**，不是最新的 Vite 8。Vite 8 与其构建器 rolldown 都要求 `node ^20.19.0 || >=22.12.0`；在更低的 Node 上，npm 会因引擎不匹配**静默跳过** rolldown 的原生二进制依赖，构建时才炸在 `Cannot find native binding`，且重装多少次都不会好（[npm/cli#4828](https://github.com/npm/cli/issues/4828) 的表现）。Vite 6 支持 `node ^18 || ^20 || >=22` 且用 rollup，没有原生二进制。若你的 Node ≥ 22.12，可以自行升回 Vite 8。

### 启动

两个终端分别跑：

```bash
# 终端 1 —— 后端，项目根目录下执行
python -m uvicorn backend.main:app --reload --port 8000

# 终端 2 —— 前端
cd frontend && npm run dev
```

或者一条命令起两个（Git Bash / WSL）：

```bash
bash scripts/dev.sh
```

前端 <http://localhost:5173>，后端 <http://127.0.0.1:8000>（交互式 API 文档在 `/docs`）。前端默认打 `http://127.0.0.1:8000`，要改就复制 `frontend/.env.example` 成 `frontend/.env.local` 并设 `VITE_API_BASE`。

### 接口

| 接口 | 说明 |
| --- | --- |
| `GET /api/health` | 连通性检查；顺带列出哪些报告已生成、未生成的该跑哪个脚本 |
| `GET /api/reports` | 报告目录 |
| `GET /api/reports/{name}` | 读取一份报告的 JSON |

`{name}` 走**显式白名单**，不做任何路径拼接。原因有两条：一是 `{name}` 直接拼路径就是路径穿越漏洞（`../../schemas/...`）；二是 `data/synthetic/` 下还躺着 `interview_notes_ground_truth.json`——那是评估用的答案，模块二的判定组件在结构上被禁止读取它，一个通用 endpoint 会让这条边界形同虚设。该文件在 `backend/main.py` 的 `EXCLUDED` 里显式排除并写明理由。

报告未生成时返回 404，`detail` 里带 `generated_by` 告诉你该跑哪个脚本——而不是假装它存在。

### 前端

React 19 + Vite 6 + [Mantine](https://mantine.dev) + recharts。组件按职责分层：

```
frontend/src/
├── api.ts                        # 客户端，结构化错误原样透传
├── lib/
│   ├── theme.ts                  # 主题 + 状态色（已过色盲/对比度校验）
│   ├── reports.ts                # 三份报告的类型，逐字段对照真实 JSON 核对
│   └── useReport.ts              # 拉取 hook，区分「未生成」与「出错」
├── components/
│   ├── ReportGate.tsx            # 加载中 / 未生成 / 出错 三态门
│   ├── StatCard.tsx              # 摘要数字卡
│   └── CountryVariantsChart.tsx  # country 写法分布（唯一的图表）
└── views/
    ├── ProfileView.tsx           # 数据质量画像
    ├── MappingView.tsx           # 字段映射建议
    ├── ValidationView.tsx        # 迁移前校验
    └── DuplicateView.tsx         # 实体解析（占位，等 Splink）
```

**图表配色是算出来的，不是挑出来的。** `country` 的 14 种写法用状态三色（合法 / 长度合格但值非法 / 非法且溢出），不是 14 个身份色。三色取自一份参考调色板的 status palette，并用其校验脚本对照本项目画布 `#171a21` 实测：对比度全部 ≥3:1，最差相邻对色盲区分度 ΔE 16.9（目标线 12）。状态色一律配文字标签，绝不单靠颜色传意。

### 前端测试

```bash
cd frontend && npm test
```

7 个用例，拿 `data/synthetic/` 下**真实的报告 JSON** 喂给 fetch mock，把每个视图完整渲染一遍——`tsc` 通过只说明类型对得上，不说明运行时不炸。报告的字段名一旦漂移，测试立刻红。

其中三个是纯函数测试，钉住 `country` 的三态分类，并断言**溢出记录数（100）与迁移前校验报告的 `max_length_overflow` 计数一致**——两份独立生成的报告在这个数字上必须对得上。

测试环境用 happy-dom 而非 jsdom：jsdom 新版的 `@csstools/css-calc` 依赖链是 ESM，而 `require()` 加载 ESM 需要 Node ≥ 20.19，本机 20.17 会抛 `ERR_REQUIRE_ESM`（与前面 Vite 8 是同一个根因）。

图表本身断言不到条形与轴刻度——recharts 的 `ResponsiveContainer` 在无头 DOM 里宽高为 0，不渲染任何 mark。所以分类逻辑抽成了纯函数 `classify()` 单独测，DOM 层只断言图例（它在容器之外）。

## 可复现性

合成数据用固定随机种子（`SEED`），报告内容跨次运行**字节一致**。

不可复现的运行元信息（时间戳）被隔离在每份报告的 `_run_info` 区块，该区块**不属于内容主体**。`_run_info.content_sha256` 是内容主体的 sha256，直接比对它即可验证可复现性：

```bash
python src/tools/data_profile.py   # 打印 Content : sha256 <前16位>
```

需要整个文件字节一致时（例如提交进 git 做 diff），设 `CARVEOPS_OMIT_TIMESTAMP=1` 即可完全不写入时间戳。
