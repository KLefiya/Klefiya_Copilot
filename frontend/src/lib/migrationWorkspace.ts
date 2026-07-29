export type DecisionMode = 'approved' | 'rejected' | 'deferred'

export interface MigrationCandidate {
  target: string
  target_resource: string | null
  target_field: string
  rank: number
  score: number
  semantic_score: number
  fuzzy_score: number
  alias_hit: boolean
  alias_source: string | null
  lexical_overlap: number
  type_gate: number
  warnings: string[]
}

export interface MigrationMapping {
  source_field: string
  status: string
  recommendation: string | null
  confidence: number
  band: string
  mapping_basis: string
  source_profile: {
    inferred_kind: string
    row_count: number
    present_count: number
    missing_count: number
    missing_ratio: number
    distinct_count: number
    distinct_ratio: number
    observed_max_length: number
    samples: string[]
  }
  review_reasons: string[]
  top_candidates: MigrationCandidate[]
}

export interface MigrationDecision {
  source_field: string
  target: string | null
  decision: DecisionMode
  reason: string | null
  transformation: {
    type: 'copy' | 'constant' | 'value_map'
    value?: string
    values?: Record<string, string>
    on_missing?: 'reject_row' | 'keep_original' | 'empty'
  }
}

export interface MigrationWorkspaceDetail {
  workspace: {
    workspace_id: string
    title: string
    description: string
    contract_id: string
    contract_version: string
    contract_sha256: string
    domain: string
    source_path: string
    source_sha256: string
    mapping_content_sha256: string
    mapping_report_sha256: string
    decision_source: 'seed' | 'runtime'
    decision_sha256: string
    runtime_state: boolean
  }
  summary: {
    source_rows: number
    source_fields: number
    target_fields: number
    approved_links: number
    unique_approved_sources: number
    rejected_sources: number
    deferred_sources: number
    multi_target_sources: number
  }
  mappings: MigrationMapping[]
  decisions: MigrationDecision[]
  build: {
    available: boolean
    summary?: {
      build_status: string
      resources_generated: number
      rows_generated: number
      rejected_rows: number
      lineage_entries: number
    }
    validation?: {
      valid: boolean
      finding_count: number
    }
    manifest?: {
      content_sha256: string
      resource_count: number
    }
    build_report_sha256?: string
  }
  resources: { name: string; fields: string[] }[]
}

export interface MigrationResourcePreview {
  resource: string
  available: boolean
  columns: string[]
  rows: Record<string, string>[]
  total_rows: number
  returned_rows: number
  content_sha256: string | null
}

export interface MigrationLineageEntry {
  source_row_number: number
  source_record_id: string
  source_field: string
  source_value_sha256: string
  target_resource: string
  target_row_number: number
  target_field: string
  transformation_type: string
  status: string
}

export interface MigrationLineageResponse {
  available: boolean
  total_entries: number
  matched_entries: number
  returned_entries: number
  entries: MigrationLineageEntry[]
}

export type SourceReview = {
  mode: DecisionMode
  targets: string[]
  reason: string
}

export type ReviewState = Record<string, SourceReview>

export function decisionsToReviewState(decisions: MigrationDecision[]): ReviewState {
  const state: ReviewState = {}
  for (const decision of decisions) {
    const current = state[decision.source_field]
    if (decision.decision === 'approved') {
      const next = current ?? { mode: 'approved', targets: [], reason: '' }
      state[decision.source_field] = {
        mode: 'approved',
        targets: [...next.targets, decision.target].filter((target): target is string => Boolean(target)),
        reason: decision.reason ?? next.reason,
      }
    } else {
      state[decision.source_field] = {
        mode: decision.decision,
        targets: [],
        reason: decision.reason ?? '',
      }
    }
  }
  return canonicalReviewState(state)
}

export function reviewStateToDecisions(state: ReviewState): MigrationDecision[] {
  const decisions: MigrationDecision[] = []
  for (const source of Object.keys(state).sort()) {
    const review = state[source]
    if (review.mode === 'approved') {
      for (const target of [...review.targets].sort()) {
        decisions.push({
          source_field: source,
          target,
          decision: 'approved',
          reason: review.reason || null,
          transformation: { type: 'copy' },
        })
      }
    } else {
      decisions.push({
        source_field: source,
        target: null,
        decision: review.mode,
        reason: review.reason || null,
        transformation: { type: 'copy' },
      })
    }
  }
  return decisions
}

export function countApprovedLinks(state: ReviewState): number {
  return Object.values(state).reduce((sum, review) => sum + (review.mode === 'approved' ? review.targets.length : 0), 0)
}

export function countUniqueApprovedSources(state: ReviewState): number {
  return Object.values(state).filter((review) => review.mode === 'approved' && review.targets.length > 0).length
}

export function findTargetConflict(
  state: ReviewState,
  sourceField: string,
  target: string,
): string | null {
  for (const [source, review] of Object.entries(state)) {
    if (source !== sourceField && review.mode === 'approved' && review.targets.includes(target)) return source
  }
  return null
}

export function canonicalReviewState(state: ReviewState): ReviewState {
  const canonical: ReviewState = {}
  for (const source of Object.keys(state).sort()) {
    const review = state[source]
    const targets = review.mode === 'approved' ? Array.from(new Set(review.targets)).sort() : []
    canonical[source] = {
      mode: review.mode,
      targets,
      reason: review.reason,
    }
  }
  return canonical
}
