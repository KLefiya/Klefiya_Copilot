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

按顺序运行即可复现全部产物：

```bash
python src/tools/generate_legacy_vendors.py
python src/tools/data_profile.py
python src/tools/field_mapping.py
```

## 可复现性

合成数据用固定随机种子（`SEED`），报告内容跨次运行**字节一致**。

不可复现的运行元信息（时间戳）被隔离在每份报告的 `_run_info` 区块，该区块**不属于内容主体**。`_run_info.content_sha256` 是内容主体的 sha256，直接比对它即可验证可复现性：

```bash
python src/tools/data_profile.py   # 打印 Content : sha256 <前16位>
```

需要整个文件字节一致时（例如提交进 git 做 diff），设 `CARVEOPS_OMIT_TIMESTAMP=1` 即可完全不写入时间戳。
