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

### 脏法的设计要经得起"平凡基线"的检验

第一版生成器造重复变体时只改写 `vendor_name` 与 `country`，其余字段**整条复制**。结果是 `city` / `street` / `postal_code` / `currency` / `created_date` 在每一对真实重复记录中都逐字相同——**一句 `GROUP BY postal_code` 就能完美复原 ground truth**，Splink 的 F1 也是 1.0，但那度量的是生成器的性质，不是模型的能力。

根因是那些脏法（大小写、空格、标点、`&`/`und`、法律形式后缀写法）**全都可被标准化完全还原**，因此不构成难度。真正的难度来自不可逆噪声。现在按脏度梯度注入：

- **名称**：拼写错误、字符换位、字符重复、键盘相邻键误击、OCR 混淆（`m` ↔ `rn`、`0` ↔ `O`、`1` ↔ `l`）
- **地址**：街道类型词的缩写/展开（`Apt.` ↔ `Apartment`、`straße` ↔ `str.`）、门牌号数字笔误
- **邮编**：数字错位，或整体缺失
- **建档日期**：漂移(重复档案本就是后来才建的)
- **联系方式与税号**：`info@` → `sales@`、电话数字笔误、税号分隔符写法不同

每条变体抽一个脏度档（`clean` / `moderate` / `dirty`，比例是模块顶部的常量），`clean` 档只有格式差异，`dirty` 档必须靠模糊匹配才可能找回。**梯度的意义在于下游的匹配置信度才会有分布、`needs_review` 才有内容**。

改完之后，标准化后名称仍逐字相同的比例从 **100% 降到 44.9%**，全部单字段 `GROUP BY` 的 F1 最高只剩 **0.8041**。生成器的 `_leakage_report()` 每次运行都会打印这张表并给出判词，**让泄漏在数据产出的那一刻就暴露，而不是等下游评估时才被发现**。

噪声走**独立的 `noise_rng`**，不消费主 rng。因此基础供应商、哪些供应商产生变体、`country` 的写法抽样全部与注入噪声前逐字一致——记录总数、实体数、国家分布不变，只有变体记录的字段内容变脏。这让改动的影响面可控且可审计（实测：150 条基础记录与 11 条完全重复记录逐字未变，ground truth 未变）。

`legacy_vendors_variant_manifest.json` 记录每条记录的角色（`base` / `name_variant` / `exact_duplicate`）、脏度档与实际施加的噪声清单。它与 ground truth 一样**只用于评估**，判定组件在结构上被禁止读取。

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
| 1 | `src/tools/generate_legacy_vendors.py` | `data/legacy/legacy_vendors.json` + ground truth + 变体清单 |
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

## 实体解析

`entity_resolution.py` 用 Splink 4（Fellegi-Sunter 概率匹配）做 `dedupe_only`，在 224 条记录中识别出 **50 个疑似重复组**（覆盖 123 条记录）。

| cluster 级（只看真实存在重复的 51 组） | precision | recall | F1 |
| --- | --- | --- | --- |
| **Splink** | **1.0000** | **0.9804** | **0.9901** |
| 最佳平凡基线（`GROUP BY postal_code`） | 0.8478 | 0.7647 | 0.8041 |

按脏度档拆开看，模型在不同难度下的表现是不一样的——这比一个总数有价值：

| 脏度档 | 召回 | 说明 |
| --- | --- | --- |
| `exact_duplicate` | 11/11 = 1.000 | 整条复制 |
| `clean` | 21/21 = 1.000 | 只有可被标准化还原的格式差异 |
| `moderate` | 20/20 = 1.000 | 少量字符噪声 + 地址缩写 |
| `dirty` | 21/22 = 0.955 | 名称被打坏 + 地址/邮编/日期同时变化 |

### 这份报告有三处在自我拆台，都是刻意的

**一、`metric_validity`：先证明指标本身有没有意义。** 报告内置平凡基线对照——每个字段单独 `GROUP BY` 的 cluster 级 F1。若某个字段单独就能复原 ground truth，那么任何模型在这份数据上的分数都不构成能力证据。当前判定是 `informative`：最佳平凡基线 F1 = 0.8041，Splink = 0.9901。

泄漏判据是 **`GROUP BY` 的 F1，不是"组内一致率"**。反例就印在报告里：`currency` 在**每一对**真实重复记录中都逐字相同（同一家公司当然同币种），但同国供应商的币种也都相同，`GROUP BY currency` 只得到 3 个大组，F1 = 0.0000。**组内一致是必要条件，跨实体有区分度才是充分条件**——早前只看一致率的版本把 `currency` 误列为泄漏字段。

**二、`veto_levels`：模型里有一票否决层。** EM 对"匹配对中从未出现过不一致"的比较层把 `m` 估成 ~0，于是：

| 比较层 | m | 匹配权重 |
| --- | --- | --- |
| `country_code :: All other` | 4.6e-107 | **−352.6 bit** |
| `legal_form :: All other` | 3.7e-20 | **−64.3 bit** |
| `name_core :: All other` | 1.6e-16 | **−52.5 bit** |

而全部正证据加起来只有 **+68.6 bit**。一对记录落进其中任意一层，概率立刻归零——即便税号与城市完全相同。这不是在权衡证据，是硬性 AND。`m=0` 编码的是"不可能"，而它实际只是"有限样本里没见过"。

当前**没有**真实重复对落进否决层，但安全裕度极薄:真实重复对中最低的 `name_core` Jaro-Winkler 相似度是 **0.8167**，兜底层门槛在 **0.80**，只差 0.0167。这是台阶设置的运气，不是稳健性。两级台阶（0.95/0.88）的上一版正因此漏掉 2 对。

正确缓解是给 `m` 加平滑下限，splink 4 未暴露该接口；改私有属性不是本项目愿意付的代价，因此选择如实报告。**已验证"删掉 `legal_form` / `country_code` 比较器"无法消除否决**——否决会转移到 `name_core` 与 `tax_norm` 上，F1 反降至 0.9505。

**三、`borderline_pairs`：漏配只能在这里被看见。** `needs_review` 只能标出"可能被错并到一起"的组，那是精确度风险。它在结构上标不出"本该并进来却没并"的记录——那条记录成了 singleton，不属于任何组，没有任何组会为它亮灯。因此模型打分打到 0.30，聚类只用 0.95 以上的边，中间那段单独列出。

唯一漏掉的一对正是这么被捞出来的：`Ritter Automation GmbH` vs `ritter  autornation gmbh`（OCR 的 `m` ↔ `rn` 混淆），匹配概率 **0.9493**，比阈值 0.95 低 0.0007。**阈值 0.95 是先验选定的常用值，没有按 ground truth 调过**——下调到 0.94 就能拿满分，但那是拿答案调参，不是模型变好了。

### 阻断：recall 的天花板

阻断阶段漏掉的记录对，后面无论模型多好都救不回来。

| 阻断规则集 | 候选对 | 占全部 24976 对 | 天花板 recall |
| --- | --- | --- | --- |
| `name_norm` / `postal_code` / `tax_number` / `email` | 86 | 0.3% | 0.8673 |
| **+ `city` + `name_prefix`（采用）** | **204** | **0.8%** | **0.9898** |
| + `country_code + legal_form` | 3324 | 13.3% | 1.0000 |

最后一档为多召回 1 对把候选对放大 16 倍，是边际收益崩溃，不采用。`phone` 单独能覆盖 57 对，但已被其它规则全部覆盖，加进来一条新候选对都不产生，故不加。

召不回的那 1 对（`V100082D` / `V100082D2`）两条记录**同时**被打坏：城市各自笔误成不同值、邮编各自错位、税号一缺一有、邮箱全缺、电话两处笔误。任何不做全量两两比较的方案都够不着它。**这是数据本身的极端情况，不是无限放宽阻断的理由**——实际上它最终经由基础记录的传递闭包被并了回来，而该组被如实标成"由传递闭包形成，需人工复核"。

### EM 的训练集偏倚

EM 只在阻断规则圈出的记录对上估计 `m`。若用强精确键（`postal_code`、`name_norm`）去圈，圈进来的几乎全是**干净的**重复对，于是 `m` 被系统性推向"处处一致"，模型对脏对要求过严。症状很直白：`postal_code` 精确匹配层的 `m = 1`，而我们明知邮编在 23.5% 的真实重复对里并不相同；`dirty` 档召回率只有 **0.55**。

改用 `name_prefix` + `city` 做 EM 的阻断规则（`name_prefix` **不是**任何比较器所用的列，该轮 EM 因此不固定任何参数，且候选集里含大量脏对）：未训练层从 3 降到 0，`dirty` 档召回率 **0.55 → 0.95**。

### 标准化

`vendor_name → name_norm / name_core / legal_form`，`country → ISO 3166-1 alpha-2`，`tax_number → tax_norm`（去分隔符），`phone → phone_norm`（只留数字）。

标准化后的列**同时**用于阻断、确定性规则与比较器——只在比较器里标准化而阻断仍用原始列，标准化就不会作用到候选生成上。对 `tax_number` / `phone` 做标准化的依据是 `data_profile` 独立报出的 `format_consistency` 告警，不是 ground truth。

`LEGAL_FORMS` 编码的是真实世界的公司法律形式（GmbH / K.K. / LLC …），与生成器的变体表重合是因为二者描述同一个客观事实，不是因为读了它。**ground truth 与 `legacy_vendors_variant_manifest.json` 只用于评估，不参与训练、阻断、阈值选择或聚类**；二者都在 `data/legacy/` 下，而后端白名单只暴露 `data/synthetic/`，结构上够不到。

### 关于 splink 的 salting bug

早前把 `estimate_u_using_random_sampling()` 的 `"Salting partitions must be specified"` 归因于 splink 4.0.16 + duckdb 的版本组合。在本机（splink 4.0.16 + duckdb 1.5.4，Windows / Python 3.12）**实测未复现**——完整比较器、`max_pairs` 到 1e7、带 seed，均正常。因此走标准训练路径（确定性规则估 λ → 随机抽样估 u → EM 估 m），没有为一个不存在的 bug 降级。若在别的平台重现该报错，退路记在 `requirements.txt` 的注释里。

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

**一个真实踩到的坑：浮点末位会毁掉 sha256。** `entity_resolution.py` 最初把 EM 训练出的 `m_probability` 原样写进报告。EM 在 duckdb 里做并行浮点求和，加法不满足结合律，末位随线程调度抖动——`4.610490895533171e-107` 与 `4.6104908955331746e-107` 交替出现，于是每次运行的 `content_sha256` 都不一样。聚类结果与全部指标本身是完全确定的，抖的只有这两个数的最后几位。

修法是按**有效数字**舍入（`_round_sig`，6 位）后再写入，不能用 `round(x, n)`——这些概率跨越 1e-107 到 1 的量级。现已验证连续 5 次运行 sha256 完全一致。
