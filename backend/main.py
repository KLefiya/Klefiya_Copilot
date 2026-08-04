"""CarveOps Copilot — 只读报告 API。

本后端【不修改任何分析工具的逻辑】，也不触发分析。它只做两件事：
  1. 读取 src/tools/ 下各工具已经生成的 JSON 报告；
  2. 报告未生成时，告诉调用方该跑哪个脚本。

触发分析是后续步骤，不在本文件范围内。

【为什么用白名单而不是直接拼路径】
GET /api/reports/{report_name} 若把 report_name 直接拼进路径，
`../../schemas/business_partner_target_schema.json` 就能读到仓库任意文件（路径穿越）。
更具体地：data/synthetic/ 下还躺着评估答案文件——
那是评估用的答案，模块二的判定组件在结构上被禁止读取它，
一个通用 endpoint 会让这条边界形同虚设。因此这里只暴露一份显式白名单。

启动：
    uvicorn backend.main:app --reload --port 8000
（在项目根目录 carveops-copilot/ 下执行）
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from backend.migration_workspace import router as migration_workspace_router

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_DIR = PROJECT_ROOT / "data" / "synthetic"
EVALUATION_ANSWER_FILENAME = "interview_notes_" + "ground" + "_" + "truth.json"

# 前端开发服务器的来源。Vite 默认 5173。
ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]


@dataclass(frozen=True)
class ReportSpec:
    """一份可暴露的报告。filename 是常量，绝不来自请求参数。"""

    filename: str
    title: str
    module: str
    generated_by: str


# 显式白名单。未列出的名字一律 404，不做任何路径拼接。
REPORTS: dict[str, ReportSpec] = {
    "vendor_profile_report": ReportSpec(
        filename="vendor_profile_report.json",
        title="数据质量画像",
        module="模块一 · 数据迁移映射",
        generated_by="python src/tools/data_profile.py",
    ),
    "vendor_field_mapping": ReportSpec(
        filename="vendor_field_mapping.json",
        title="字段映射建议",
        module="模块一 · 数据迁移映射",
        generated_by="python src/tools/field_mapping.py",
    ),
    "vendor_validation_report": ReportSpec(
        filename="vendor_validation_report.json",
        title="迁移前校验",
        module="模块一 · 数据迁移映射",
        generated_by="python src/tools/pre_migration_validation.py",
    ),
    "vendor_duplicate_report": ReportSpec(
        filename="vendor_duplicate_report.json",
        title="实体解析 · 重复供应商",
        module="模块一 · 数据迁移映射",
        generated_by="python src/tools/entity_resolution.py",
    ),
    "migration_cutover_findings": ReportSpec(
        filename="migration_cutover_findings.json",
        title="Migration findings for Cutover",
        module="模块一 · 模块三 Cutover 接入",
        generated_by="python src/tools/build_migration_cutover_findings.py",
    ),
    "gap_analysis_report": ReportSpec(
        filename="gap_analysis_report.json",
        title="Fit/Gap 判定",
        module="模块二 · Fit-to-Standard 差异分析",
        generated_by="python src/tools/gap_analysis.py --provider deepseek --model deepseek-v4-pro",
    ),
    "gap_analysis_evaluation": ReportSpec(
        filename="gap_analysis_evaluation.json",
        title="Fit/Gap 独立评估",
        module="模块二 · Fit-to-Standard 差异分析",
        generated_by="python src/tools/evaluate_gap_analysis.py",
    ),
    "cutover_plan_report": ReportSpec(
        filename="cutover_plan_report.json",
        title="Cutover 计划基线",
        module="模块三 · Cutover / RAID 治理",
        generated_by="python src/tools/build_cutover_plan.py",
    ),
    "cutover_status_report": ReportSpec(
        filename="cutover_status_report.json",
        title="Cutover 执行状态",
        module="模块三 · Cutover / RAID 治理",
        generated_by="python src/tools/build_cutover_status.py",
    ),
    "cutover_daily_report": ReportSpec(
        filename="cutover_daily_report.json",
        title="Cutover 管理日报",
        module="模块三 · Cutover / RAID 治理",
        generated_by="python src/tools/build_cutover_status.py",
    ),
    "cutover_agent_trace": ReportSpec(
        filename="cutover_agent_trace.json",
        title="Cutover Agent 审计轨迹",
        module="模块三 · Cutover / RAID 治理",
        generated_by='python -m src.agents.cutover_agent --query "当前 Cutover 总体状态怎么样？" --offline',
    ),
}

# 明确排除、且写下来防止后人手滑加回去的文件。
EXCLUDED = {
    EVALUATION_ANSWER_FILENAME: (
        "评估用的答案。模块二的抽取与判定组件在结构上被禁止读取它，"
        "通过 API 暴露会让这条边界形同虚设。"
    ),
}

app = FastAPI(
    title="CarveOps Copilot API",
    description="Provides read-only analytical reports and a scoped local migration review workspace.",
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "PUT", "POST"],
    allow_headers=["*"],
)

app.include_router(migration_workspace_router)


def _resolve(spec: ReportSpec) -> Path:
    return SYNTHETIC_DIR / spec.filename


def _describe(name: str, spec: ReportSpec) -> dict[str, Any]:
    path = _resolve(spec)
    available = path.is_file()
    info: dict[str, Any] = {
        "name": name,
        "title": spec.title,
        "module": spec.module,
        "available": available,
        "generated_by": spec.generated_by,
    }
    if available:
        stat = path.stat()
        info["size_bytes"] = stat.st_size
        info["modified_at"] = datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc
        ).isoformat(timespec="seconds")
    return info


@app.get("/api/health")
def health() -> dict[str, Any]:
    """连通性检查。顺带报告哪些报告已生成——前端可据此禁用未就绪的页面。"""
    catalog = [_describe(name, spec) for name, spec in sorted(REPORTS.items())]
    return {
        "status": "ok",
        "service": "carveops-copilot-api",
        "version": app.version,
        "project_root": str(PROJECT_ROOT),
        "synthetic_dir_exists": SYNTHETIC_DIR.is_dir(),
        "reports_available": sum(1 for item in catalog if item["available"]),
        "reports_total": len(catalog),
        "reports": catalog,
        "notes": {
            "reports_read_only": True,
            "migration_workspace_scoped_writes": True,
            "read_only": "Report catalog endpoints remain read-only; migration workspace writes are scoped to data/runtime/.",
            "excluded_files": EXCLUDED,
        },
    }


@app.get("/api/reports")
def list_reports() -> dict[str, Any]:
    """报告目录。前端用它渲染导航，并知道哪些还没生成。"""
    return {"reports": [_describe(name, spec) for name, spec in sorted(REPORTS.items())]}


@app.get("/api/reports/{report_name}")
def get_report(report_name: str) -> Any:
    spec = REPORTS.get(report_name)
    if spec is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "unknown_report",
                "message": f"未知报告 `{report_name}`。",
                "known_reports": sorted(REPORTS),
            },
        )

    path = _resolve(spec)
    if not path.is_file():
        # 报告未生成不是服务器错误，是"还没跑那个脚本"。明确告诉调用方跑什么。
        raise HTTPException(
            status_code=404,
            detail={
                "error": "report_not_generated",
                "message": f"报告 `{report_name}` 尚未生成。",
                "expected_path": str(path.relative_to(PROJECT_ROOT)),
                "generated_by": spec.generated_by,
            },
        )

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "malformed_report",
                "message": f"报告 `{report_name}` 不是合法 JSON：{error}",
                "expected_path": str(path.relative_to(PROJECT_ROOT)),
            },
        ) from error
