export type MappingScorer = 'baseline' | 'precision_tiered_v4'

export interface MappingContractSummary {
  contract_id: string
  title: string
  domain: string
  version: string
  target_resource_count: number
  target_field_count: number
  target_fields: string[]
  supported_scorers: MappingScorer[]
}

export interface MappingContractCatalog {
  contracts: MappingContractSummary[]
}

export interface CreateMappingJobPayload {
  contract_id: string
  filename: string
  csv_text: string
  scorer: MappingScorer
}

export interface MappingJobMetadata {
  schema: string
  version: string
  job_id: string
  status: 'completed'
  created_at?: string
  original_filename: string
  contract_registry_id: string
  contract: {
    contract_id: string
    title: string
    domain: string
    version: string
    contract_sha256: string
    target_resource_count: number
    target_field_count: number
  }
  scorer: MappingScorer
  source: {
    path: string
    sha256: string
    hash_mode: string
    row_count: number
    field_count: number
  }
  mapping_report: {
    path: string
    content_sha256: string
  }
}

export interface MappingJobSummary {
  suggested?: number
  needs_review?: number
  possible_false_friend?: number
  no_confident_target?: number
  target_coverage?: number | null
}

export interface MappingSourceProfile {
  name?: string
  inferred_kind?: string
  row_count?: number
  present_count?: number
  missing_count?: number
  missing_ratio?: number
  distinct_count?: number
  distinct_ratio?: number
  observed_min_length?: number | null
  observed_mean_length?: number | null
  observed_max_length?: number | null
}

export interface MappingCandidate {
  target?: string | null
  rank?: number
  score?: number
  semantic_score?: number
  fuzzy_score?: number
  alias_hit?: boolean
  lexical_overlap?: number | string[] | null
  type_gate?: number
  value_pattern_evidence?: Record<string, unknown> | unknown[] | string | null
  resource_context_evidence?: Record<string, unknown> | unknown[] | string | null
  activated_interactions?: string[]
  interaction_evidence?: Record<string, unknown> | unknown[] | string | null
  diagnostic_bonus?: number
  supportive_bonus?: number
  top1_selection_reason?: string | null
  warnings?: string[]
}

export interface MappingResult {
  source_field: string
  status: string
  recommendation: string | null
  confidence?: number | null
  band?: string | null
  mapping_basis?: string | null
  review_reasons?: string[]
  source_profile?: MappingSourceProfile
  top_candidates?: MappingCandidate[]
}

export type MappingReviewAction = 'accept_suggestion' | 'select_target' | 'mark_unmapped'

export interface MappingReviewDecision {
  source_field: string
  action: MappingReviewAction
  target_fields?: string[]
  note?: string | null
}

export interface MappingReviewPayload {
  mapping_report_sha256: string
  decisions: MappingReviewDecision[]
}

export interface MappingReviewSummary {
  mapping_report_sha256: string
  reviewed_fields: number
  total_fields: number
  pending_fields: number
  accepted_count: number
  overridden_count: number
  unmapped_count: number
  export_ready: boolean
  updated_at?: string | null
  decisions: MappingReviewDecision[]
}

export interface MappingReviewResponse {
  review: MappingReviewSummary
}

export type MappingExportFormat = 'json' | 'csv'

export interface MappingExportDownload {
  blob: Blob
  filename: string
  contentType: string
}

export interface MappingJobResponse {
  job: MappingJobMetadata
  summary: MappingJobSummary
  mappings: MappingResult[]
  review?: MappingReviewSummary
}
