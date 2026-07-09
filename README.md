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
- **不外传任何数据。** 所有工具读写本地文件，不向任何外部服务发送项目数据、数据画像结果或映射建议。

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

按顺序运行即可复现全部产物：

```bash
python src/tools/generate_legacy_vendors.py
python src/tools/data_profile.py
python src/tools/field_mapping.py
python src/tools/pre_migration_validation.py
```

迁移前校验把两个**正交**维度分开表达，不混为一谈：`semantic_match`（映射语义是否正确，沿用 field_mapping 的结论）与 `loadable`（目标字段是否可写入，取 `is_creatable or is_updatable`）。典型情形是 `created_date → CreationDate`：语义完全正确，但该字段 `sap:creatable` 与 `sap:updatable` 均为 false，只能作 lineage / 参考，不能记为"通过"。

## 可复现性

合成数据用固定随机种子（`SEED`），报告内容跨次运行**字节一致**。

不可复现的运行元信息（时间戳）被隔离在每份报告的 `_run_info` 区块，该区块**不属于内容主体**。`_run_info.content_sha256` 是内容主体的 sha256，直接比对它即可验证可复现性：

```bash
python src/tools/data_profile.py   # 打印 Content : sha256 <前16位>
```

需要整个文件字节一致时（例如提交进 git 做 diff），设 `CARVEOPS_OMIT_TIMESTAMP=1` 即可完全不写入时间戳。
