/**
 * 三份报告的类型定义。字段名逐一对照实际 JSON 核对过，不是凭记忆写的。
 * 报告由 src/tools/ 下的分析工具生成，前端只读不改。
 */

// ---------------------------------------------------------------- 通用
export interface RunInfo {
  content_sha256: string
  note: string
  generated_at?: string
}

// ---------------------------------------------------------------- 数据质量画像
export interface FormatSignature {
  signature: string
  count: number
  example: string
}

export interface ValueCount {
  value: string
  count: number
}

export interface FieldProfile {
  record_count: number
  missing_count: number
  missing_rate: number
  distinct_count: number
  distinct_ratio: number
  avg_length: number
  is_probable_identifier: boolean
  is_free_text: boolean
  free_text_source: string
  /** 自由文本字段为 null —— 格式一致性检测对它们不适用 */
  format_variants: number | null
  format_signatures: FormatSignature[] | null
  /** 仅低基数字段有 */
  value_distribution?: ValueCount[]
}

export interface QualityFlag {
  field: string
  issue_type: string
  severity: 'high' | 'medium' | 'low'
  message: string
}

export interface ProfileReport {
  _run_info: RunInfo
  _meta: {
    record_count: number
    field_count: number
    source_file: string
    schema_file: string
    thresholds: {
      missing_rate_flag_threshold: number
      free_text_distinct_ratio: number
      free_text_avg_length: number
      low_cardinality_max_distinct: number
      identifier_distinct_ratio: number
    }
  }
  fields: Record<string, FieldProfile>
  quality_flags: QualityFlag[]
}

// ---------------------------------------------------------------- 字段映射建议
export interface MappingSignals {
  semantic: number
  fuzzy: number
  type: number
  alias: string | null
  lexical_overlap: string[]
}

export interface Candidate {
  target_entity: string
  target_field: string
  qualified: string
  confidence: number
  band: 'high' | 'medium' | 'low'
  signals: MappingSignals
  target_type: string
  target_max_length: number | null
  target_description_zh: string
  evidence: string[]
  warnings: string[]
}

export type MappingStatus =
  | 'suggested'
  | 'needs_review'
  | 'possible_false_friend'
  | 'no_confident_target'

export interface Mapping {
  legacy_field: string
  legacy_profile: {
    observed_max_length: number
    distinct_ratio: number
    is_free_text: boolean
    inferred_kind: string
    samples: string[]
  }
  recommendation: string | null
  confidence: number
  band: string
  status: MappingStatus
  needs_review: boolean
  candidates: Candidate[]
}

export interface MappingGap {
  legacy_field: string
  status: string
  best_candidate: string | null
  best_confidence: number
  message: string
}

export interface MappingReport {
  _run_info: RunInfo
  _meta: {
    disclaimer: string
    embedding_model: string
    legacy_record_count: number
    target_field_count: number
    scoring: Record<string, unknown>
    thresholds: { alias_confidence_floor: number; high: number; medium: number; no_match: number }
  }
  mappings: Mapping[]
  gaps: MappingGap[]
}

// ---------------------------------------------------------------- 迁移前校验
export interface FieldIssue {
  issue_type: string
  severity: 'high' | 'medium' | 'low'
  field: string
  detail_zh: string
  suggestion_zh: string
  /** 仅 normalization_required 有 */
  non_conforming_values?: ValueCount[]
}

export interface RecordIssue {
  field: string
  target: string
  issue_type: string
  severity: 'high' | 'medium' | 'low'
  value: string | null
  detail_zh: string
  suggestion_zh: string
  based_on_unverified: boolean
  unverified_disclaimer_zh?: string
}

export interface TargetConstraints {
  type: string
  max_length: number | null
  nullable: boolean
  is_key: boolean
  is_creatable: boolean
  is_updatable: boolean
  allowed_values: string[] | null
  verification_status: string
}

export type Verdict =
  | 'loadable_ok'
  | 'mapping_ok_but_not_loadable'
  | 'needs_human_decision'
  | 'no_target'
  | 'no_source'

export interface FieldView {
  legacy_field: string | null
  target: string | null
  mapping_status: string
  mapping_confidence: number | null
  /** 三态：true / false / null(不确定) —— 与 loadable 正交 */
  semantic_match: boolean | null
  loadable: boolean | null
  loadable_reason: string | null
  verdict: Verdict
  based_on_unverified: boolean
  target_constraints: TargetConstraints | null
  record_issue_counts: Record<string, number>
  field_issues: FieldIssue[]
}

export interface RecordView {
  record_id: string
  issue_count: number
  issues: RecordIssue[]
}

export interface DeferredCheck {
  check: string
  fields: string[]
  status: string
  reason_zh: string
  blocked_by_zh: string
}

export interface ValidationReport {
  _run_info: RunInfo
  _meta: {
    disclaimer: string
    orthogonality_note_zh: string
    reuse_note_zh: string
    record_count: number
    records_with_issues: number
    records_clean: number
    sources: Record<string, string>
    checks_implemented: Record<string, string>
  }
  summary: {
    by_issue_type: Record<string, number>
    by_severity: { high: number; medium: number; low: number }
    verdicts: Record<string, number>
  }
  field_view: FieldView[]
  record_view: RecordView[]
  deferred_checks: DeferredCheck[]
}

// ---------------------------------------------------------------- Fit-to-Standard gap analysis
export type GapCategory = 'Fit' | 'Configuration' | 'Enhancement' | 'Development'
export type GapDomain = 'P2P' | 'O2C' | 'R2R' | 'master_data'

export interface GapBaseline {
  category: GapCategory
  rule_fired_zh: string
  top_entry_ids: string[]
  top_chunk: {
    entry_id: string
    section: string
    similarity: number
  }
}

export interface GapLLMJudgement {
  category: GapCategory
  confidence: number
  evidence: string[]
  rationale: string
  needs_review: boolean
  needs_review_reasons: string[]
  retrieved_entry_ids: string[]
}

export interface GapRequirement {
  extracted_id: string
  source_note_id: string
  requirement_description: string
  domain: GapDomain
  source_quote: string
  baseline: GapBaseline
  llm: GapLLMJudgement
}

export interface DevelopmentBacklogItem {
  backlog_id: string
  requirement_id: string
  source_note_id: string
  description: string
  domain: GapDomain
  rationale: string
  evidence: string[]
  confidence: number
  needs_review: boolean
}

export interface GapAnalysisReport {
  _run_info: RunInfo & {
    llm_cache?: {
      hits: number
      misses: number
    }
  }
  _meta: {
    provider: string
    model: string
    thinking?: string | null
    reasoning_effort?: string | null
    extracted_requirement_count: number
  }
  requirements: GapRequirement[]
  dev_backlog: DevelopmentBacklogItem[]
  dev_backlog_note_zh?: string
}

export interface ClassMetrics {
  support: number
  correct: number
  recall: number | null
  precision: number | null
}

export interface EvaluationModelMetrics {
  n: number
  accuracy: number
  per_class: Record<GapCategory, ClassMetrics>
}

export interface DevelopmentRationaleAuditItem {
  requirement_id: string
  extracted_id: string
  description: string
  llm_category: GapCategory
  correct: boolean
  evidence: string[]
  reasoning_markers_found: string[]
  surface_markers_found: string[]
  verdict_zh: string
  rationale: string
}

export interface GapAnalysisEvaluation {
  _run_info: RunInfo
  _meta: {
    ground_truth: number
    extracted: number
    matched: number
    spurious: number
    missed: number
    report_content_sha256: string
    model: string
  }
  baseline_no_llm: EvaluationModelMetrics
  llm: EvaluationModelMetrics
  llm_vs_baseline: {
    accuracy_delta: number
  }
  development_rationale_audit: DevelopmentRationaleAuditItem[]
}

// ---------------------------------------------------------------- 问题类型的中文名
export const ISSUE_TYPE_LABEL: Record<string, string> = {
  max_length_overflow: '长度溢出',
  normalization_required: '归一化需求',
  target_not_creatable_or_updatable: '只读阻断',
  unmapped_target_key: '目标主键无映射',
  no_target_in_schema: '目标 schema 无落点',
  possible_false_friend_target: '疑似假朋友',
  mapping_needs_review: '映射需复核',
  required_field_missing: '必填缺失',
  duplicate_primary_key: '重复主键',
  type_not_parseable: '类型不可解析',
  value_not_in_allowed_values: '取值越界',
  format_consistency: '格式不一致',
  completeness: '完整性',
}

export const SEVERITY_LABEL: Record<string, string> = {
  high: '高',
  medium: '中',
  low: '低',
}
