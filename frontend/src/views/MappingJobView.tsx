import { useEffect, useMemo, useState } from 'react'
import {
  Accordion,
  Alert,
  Badge,
  Box,
  Button,
  Checkbox,
  Code,
  FileInput,
  Group,
  Loader,
  NativeSelect,
  SimpleGrid,
  Stack,
  Table,
  Text,
  TextInput,
  Textarea,
  Title,
  Tooltip,
} from '@mantine/core'
import { ApiError, createMappingJob, deleteMappingJob, downloadMappingReviewExport, getMappingContracts, getMappingJob, saveMappingReview } from '../api'
import { StatCard } from '../components/StatCard'
import type {
  IdentifierInteractionEvidence,
  MappingCandidate,
  MappingContractSummary,
  MappingExportFormat,
  MappingJobResponse,
  MappingResult,
  MappingReviewAction,
  MappingReviewDecision,
  MappingReviewSummary,
  MappingScorer,
  MappingSourceProfile,
} from '../lib/mappingJobs'
import { STATUS } from '../lib/theme'

const MAX_CSV_BYTES = 1024 * 1024
const MAX_REVIEW_NOTE_LENGTH = 500
const JOB_ID_PATTERN = /^[0-9a-f]{32}$/

interface ReviewDraft {
  action: MappingReviewAction | ''
  target_fields: string[]
  note: string
}

function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    const detail = error.detail
    if (typeof detail === 'object' && detail !== null) {
      const value = detail as { error?: unknown; message?: unknown }
      return [value.error, value.message].filter((item) => typeof item === 'string' && item.length > 0).join(' · ')
    }
    return error.message
  }
  return String(error)
}

function shortValue(value: string | null | undefined, start = 10, end = 6): string {
  if (!value) return '-'
  if (value.length <= start + end + 3) return value
  return `${value.slice(0, start)}...${value.slice(-end)}`
}

function numberText(value: number | null | undefined, digits = 3): string {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '-'
}

function percentText(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '-'
  return `${(value * 100).toFixed(1)}%`
}

function statusColor(status: string): string {
  if (status === 'suggested') return STATUS.good
  if (status === 'needs_review') return STATUS.warning
  if (status === 'possible_false_friend') return STATUS.serious
  return STATUS.critical
}

function scorerLabel(scorer: MappingScorer): string {
  if (scorer === 'precision_tiered_v5') return 'Precision Tiered V5'
  if (scorer === 'precision_tiered_v4') return 'Precision Tiered V4'
  return 'Baseline'
}

function primitiveText(value: unknown): string | null {
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return null
}

function evidenceItems(value: unknown): string[] {
  const primitive = primitiveText(value)
  if (primitive) return [primitive]
  if (Array.isArray(value)) {
    return value
      .map((item) => primitiveText(item) ?? (typeof item === 'object' && item !== null ? Object.keys(item).join(', ') : null))
      .filter((item): item is string => Boolean(item))
      .slice(0, 6)
  }
  if (typeof value === 'object' && value !== null) {
    return Object.entries(value as Record<string, unknown>)
      .map(([key, item]) => {
        const text = primitiveText(item)
        if (text) return `${key}: ${text}`
        if (Array.isArray(item)) return `${key}: ${item.length} items`
        if (typeof item === 'object' && item !== null) return `${key}: ${Object.keys(item).join(', ')}`
        return key
      })
      .slice(0, 8)
  }
  return []
}

function conceptText(value: string[] | undefined): string {
  return value && value.length > 0 ? value.join(', ') : '-'
}

function IdentifierEvidence({ evidence }: { evidence: IdentifierInteractionEvidence }) {
  return (
    <Stack gap={2}>
      <Text size="xs" fw={600}>Identifier interaction: {evidence.interaction_id ?? '-'}</Text>
      <Text size="xs">tier {evidence.tier ?? '-'} 路 matched entity concepts {conceptText(evidence.matched_entity_concepts)}</Text>
      <Text size="xs">source concepts {conceptText(evidence.source_concepts)}</Text>
      <Text size="xs">target concepts {conceptText(evidence.target_concepts)}</Text>
      <Text size="xs">
        bonus weight {numberText(evidence.bonus_weight)} 路 bonus {numberText(evidence.bonus)}
      </Text>
      <Text size="xs">may displace V4 Top-1: {evidence.may_displace_v4_top1 ? 'yes' : 'no'}</Text>
    </Stack>
  )
}

function actionLabel(action: MappingReviewAction | ''): string {
  if (action === 'accept_suggestion') return '接受算法建议'
  if (action === 'select_target') return '改选目标'
  if (action === 'mark_unmapped') return '标记不映射'
  return '待复核'
}

function hasControlCharacter(value: string): boolean {
  return [...value].some((char) => {
    const codePoint = char.codePointAt(0)
    return codePoint !== undefined && ((codePoint >= 0 && codePoint <= 8) || (codePoint >= 10 && codePoint <= 31) || codePoint === 127)
  })
}

function reviewDraftFromDecision(decision: MappingReviewDecision): ReviewDraft {
  return {
    action: decision.action,
    target_fields: decision.target_fields ?? [],
    note: decision.note ?? '',
  }
}

function emptyReviewDraft(): ReviewDraft {
  return { action: '', target_fields: [], note: '' }
}

function reviewDraftSignature(drafts: Record<string, ReviewDraft>, mappings: MappingResult[]): string {
  return JSON.stringify(
    mappings.map((mapping) => {
      const draft = drafts[mapping.source_field] ?? emptyReviewDraft()
      return [
        mapping.source_field,
        draft.action,
        [...draft.target_fields].sort(),
        draft.note,
      ]
    }),
  )
}

function buildReviewDecisions(drafts: Record<string, ReviewDraft>, mappings: MappingResult[]): MappingReviewDecision[] {
  return mappings
    .map((mapping) => {
      const draft = drafts[mapping.source_field]
      if (!draft?.action) return null
      const note = draft.note.trim()
      const decision: MappingReviewDecision = {
        source_field: mapping.source_field,
        action: draft.action,
      }
      if (draft.action === 'select_target') decision.target_fields = draft.target_fields
      if (draft.action === 'mark_unmapped') decision.target_fields = []
      if (note) decision.note = note
      return decision
    })
    .filter((decision): decision is MappingReviewDecision => decision !== null)
}

function reviewSummaryFromDrafts(
  result: MappingJobResponse,
  drafts: Record<string, ReviewDraft>,
): MappingReviewSummary {
  const decisions = buildReviewDecisions(drafts, result.mappings)
  return {
    mapping_report_sha256: result.job.mapping_report.content_sha256,
    reviewed_fields: decisions.length,
    total_fields: result.mappings.length,
    pending_fields: result.mappings.length - decisions.length,
    accepted_count: decisions.filter((decision) => decision.action === 'accept_suggestion').length,
    overridden_count: decisions.filter((decision) => decision.action === 'select_target').length,
    unmapped_count: decisions.filter((decision) => decision.action === 'mark_unmapped').length,
    export_ready: decisions.length === result.mappings.length,
    updated_at: result.review?.updated_at ?? null,
    decisions,
  }
}

function ErrorAlert({ message }: { message: string | null }) {
  if (!message) return null
  return (
    <Alert color="red" variant="light" title="Request failed">
      {message}
    </Alert>
  )
}

function ConstraintNote() {
  return (
    <Alert color="blue" variant="light">
      CSV must be a single .csv file, at most 1 MiB, with validation finalized by the backend. Uploaded
      content is sent only in the POST body and is not stored in browser storage.
    </Alert>
  )
}

function JobSummary({ result }: { result: MappingJobResponse }) {
  const { job, summary } = result
  return (
    <SimpleGrid cols={{ base: 1, sm: 2, lg: 4 }} spacing="md">
      <StatCard label="Original file" value={job.original_filename} hint={shortValue(job.job_id, 8, 6)} />
      <StatCard label="Contract" value={job.contract.title} hint={`${job.contract.domain} · v${job.contract.version}`} />
      <StatCard label="Scorer" value={scorerLabel(job.scorer)} hint={job.status} />
      <StatCard label="Rows" value={job.source.row_count} />
      <StatCard label="Source fields" value={job.source.field_count} />
      <StatCard label="Target fields" value={job.contract.target_field_count} />
      <StatCard label="Suggested" value={summary.suggested ?? 0} accent={STATUS.good} />
      <StatCard label="Needs review" value={summary.needs_review ?? 0} accent={STATUS.warning} />
      <StatCard label="No confident target" value={summary.no_confident_target ?? 0} accent={STATUS.critical} />
      <StatCard label="Target coverage" value={percentText(summary.target_coverage)} />
      <StatCard label="Job ID" value={shortValue(job.job_id)} />
      <StatCard label="Mapping SHA" value={shortValue(job.mapping_report.content_sha256)} />
    </SimpleGrid>
  )
}

function DeleteJobPanel({
  result,
  deleting,
  error,
  onDelete,
}: {
  result: MappingJobResponse
  deleting: boolean
  error: string | null
  onDelete: () => Promise<void>
}) {
  const [confirming, setConfirming] = useState(false)
  const [confirmation, setConfirmation] = useState('')
  const [confirmationError, setConfirmationError] = useState<string | null>(null)
  const shortId = result.job.job_id.slice(0, 8)
  const confirmationMatches = confirmation === shortId || confirmation === result.job.job_id

  const requestDelete = async () => {
    if (!confirmationMatches) {
      setConfirmationError(`delete_confirmation_mismatch - Type ${shortId} or the full job id to confirm.`)
      return
    }
    setConfirmationError(null)
    await onDelete()
  }

  return (
    <Alert color="red" variant="light" title="Delete Job">
      <Stack gap="sm">
        <Text size="sm">
          Deletes this local runtime job, including source CSV, mapping report, review, metadata, and job-local temp files.
        </Text>
        {!confirming ? (
          <Button
            color="red"
            variant="filled"
            onClick={() => {
              setConfirming(true)
              setConfirmation('')
              setConfirmationError(null)
            }}
            disabled={deleting}
          >
            Delete Job
          </Button>
        ) : (
          <Stack gap="sm">
            <TextInput
              label={`Type ${shortId} or the full job id`}
              value={confirmation}
              onChange={(event) => {
                setConfirmation(event.currentTarget.value.trim())
                setConfirmationError(null)
              }}
              disabled={deleting}
            />
            <Group gap="sm">
              <Button color="red" loading={deleting} disabled={deleting} onClick={requestDelete}>
                Confirm delete job
              </Button>
              <Button
                variant="subtle"
                color="gray"
                disabled={deleting}
                onClick={() => {
                  setConfirming(false)
                  setConfirmation('')
                  setConfirmationError(null)
                }}
              >
                Cancel
              </Button>
            </Group>
          </Stack>
        )}
        <ErrorAlert message={confirmationError ?? error} />
      </Stack>
    </Alert>
  )
}

function SourceProfileStats({ profile }: { profile: MappingSourceProfile | undefined }) {
  if (!profile) return <Text size="sm" c="dimmed">No source profile statistics returned.</Text>
  return (
    <SimpleGrid cols={{ base: 1, sm: 2, md: 4 }} spacing="sm">
      <Text size="sm"><Text span c="dimmed">Kind: </Text>{profile.inferred_kind ?? '-'}</Text>
      <Text size="sm"><Text span c="dimmed">Missing: </Text>{percentText(profile.missing_ratio)}</Text>
      <Text size="sm"><Text span c="dimmed">Distinct: </Text>{percentText(profile.distinct_ratio)}</Text>
      <Text size="sm"><Text span c="dimmed">Max length: </Text>{profile.observed_max_length ?? '-'}</Text>
      <Text size="sm"><Text span c="dimmed">Min length: </Text>{profile.observed_min_length ?? '-'}</Text>
      <Text size="sm"><Text span c="dimmed">Mean length: </Text>{numberText(profile.observed_mean_length, 1)}</Text>
      <Text size="sm"><Text span c="dimmed">Present: </Text>{profile.present_count ?? '-'}</Text>
      <Text size="sm"><Text span c="dimmed">Rows: </Text>{profile.row_count ?? '-'}</Text>
    </SimpleGrid>
  )
}

function EvidenceBadges({ label, value }: { label: string; value: unknown }) {
  const items = evidenceItems(value)
  if (items.length === 0) return null
  return (
    <Group gap={6}>
      <Text size="xs" c="dimmed">{label}</Text>
      {items.map((item) => (
        <Badge key={`${label}-${item}`} size="xs" variant="light">
          {item}
        </Badge>
      ))}
    </Group>
  )
}

function CandidateSignals({ candidate }: { candidate: MappingCandidate }) {
  const overlap = Array.isArray(candidate.lexical_overlap)
    ? candidate.lexical_overlap.join(', ')
    : candidate.lexical_overlap
  const identifierEvidence = candidate.identifier_interaction_evidence ?? []
  return (
    <Stack gap={4}>
      <Text size="xs">semantic {numberText(candidate.semantic_score)} · fuzzy {numberText(candidate.fuzzy_score)}</Text>
      <Text size="xs">
        alias {candidate.alias_hit ? 'hit' : 'none'} · type gate {numberText(candidate.type_gate, 2)}
        {overlap !== undefined && overlap !== null ? ` · lexical ${String(overlap)}` : ''}
      </Text>
      <EvidenceBadges label="value pattern" value={candidate.value_pattern_evidence} />
      <EvidenceBadges label="resource context" value={candidate.resource_context_evidence} />
      <EvidenceBadges label="interactions" value={candidate.activated_interactions} />
      <EvidenceBadges label="interaction evidence" value={candidate.interaction_evidence} />
      {(candidate.diagnostic_bonus !== undefined || candidate.supportive_bonus !== undefined) && (
        <Text size="xs">
          diagnostic {numberText(candidate.diagnostic_bonus)} · supportive {numberText(candidate.supportive_bonus)}
        </Text>
      )}
      {candidate.top1_selection_reason && (
        <Text size="xs" c="dimmed">Top-1 reason: {candidate.top1_selection_reason}</Text>
      )}
      {identifierEvidence.length > 0 && (
        <Stack gap={4}>
          {identifierEvidence.map((evidence, index) => (
            <IdentifierEvidence
              key={`${candidate.target ?? 'candidate'}-${evidence.interaction_id ?? index}`}
              evidence={evidence}
            />
          ))}
          <Text size="xs">
            V4 score {numberText(candidate.v4_score)} 路 identifier bonus {numberText(candidate.identifier_bonus)}
          </Text>
          <Text size="xs">
            adjusted V5 score {numberText(candidate.identifier_adjusted_score)} 路 Top-1 eligibility {candidate.v5_top1_eligible ? 'yes' : 'no'}
          </Text>
          {candidate.v5_top1_selection_reason && (
            <Text size="xs" c="dimmed">V5 selection reason: {candidate.v5_top1_selection_reason}</Text>
          )}
        </Stack>
      )}
    </Stack>
  )
}

function CandidateTable({ candidates }: { candidates: MappingCandidate[] }) {
  return (
    <Box style={{ overflowX: 'auto' }}>
      <Table striped highlightOnHover verticalSpacing="sm" miw={840}>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Rank</Table.Th>
            <Table.Th>Target</Table.Th>
            <Table.Th>Ranking Score</Table.Th>
            <Table.Th>Main Signals</Table.Th>
            <Table.Th>Warnings</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {candidates.map((candidate) => (
            <Table.Tr key={`${candidate.rank}-${candidate.target}`}>
              <Table.Td>{candidate.rank ?? '-'}</Table.Td>
              <Table.Td><Code>{candidate.target ?? '-'}</Code></Table.Td>
              <Table.Td ff="monospace">{numberText(candidate.score)}</Table.Td>
              <Table.Td><CandidateSignals candidate={candidate} /></Table.Td>
              <Table.Td>{candidate.warnings?.length ? candidate.warnings.join(', ') : '-'}</Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </Box>
  )
}

function MappingItem({ mapping }: { mapping: MappingResult }) {
  const candidates = mapping.top_candidates ?? []
  return (
    <Accordion.Item value={mapping.source_field}>
      <Accordion.Control>
        <Group justify="space-between" wrap="nowrap" pr="sm">
          <Group gap="sm" wrap="nowrap" style={{ minWidth: 0 }}>
            <Code>{mapping.source_field}</Code>
            <Text size="sm" c="dimmed">to</Text>
            {mapping.recommendation ? (
              <Text size="sm" ff="monospace" truncate>{mapping.recommendation}</Text>
            ) : (
              <Text size="sm" c="dimmed" fs="italic">No automatic recommendation, manual review required</Text>
            )}
          </Group>
          <Group gap="xs" wrap="nowrap">
            <Tooltip label="Ranking score, not a probability">
              <Text size="sm" ff="monospace">{numberText(mapping.confidence)}</Text>
            </Tooltip>
            <Badge size="sm" variant="light" style={{ backgroundColor: `${statusColor(mapping.status)}22`, color: statusColor(mapping.status) }}>
              {mapping.status}
            </Badge>
          </Group>
        </Group>
      </Accordion.Control>
      <Accordion.Panel>
        <Stack gap="md">
          <SourceProfileStats profile={mapping.source_profile} />
          {mapping.review_reasons && mapping.review_reasons.length > 0 && (
            <Group gap={6}>
              <Text size="xs" c="dimmed">Review reasons</Text>
              {mapping.review_reasons.map((reason) => (
                <Badge key={reason} size="xs" variant="outline">{reason}</Badge>
              ))}
            </Group>
          )}
          <CandidateTable candidates={candidates.slice(0, 3)} />
        </Stack>
      </Accordion.Panel>
    </Accordion.Item>
  )
}

function MappingResults({ result }: { result: MappingJobResponse }) {
  return (
    <Stack gap="md">
      <Title order={3} size="h5">Mapping Results</Title>
      <Accordion
        variant="separated"
        radius="md"
        chevronPosition="left"
        multiple
        defaultValue={result.mappings.map((mapping) => mapping.source_field)}
      >
        {result.mappings.map((mapping) => (
          <MappingItem key={mapping.source_field} mapping={mapping} />
        ))}
      </Accordion>
    </Stack>
  )
}

function ReviewPanel({
  result,
  drafts,
  targetOptions,
  targetOptionsAvailable,
  isDirty,
  saving,
  downloading,
  mutationDisabled,
  message,
  error,
  onDraftChange,
  onSave,
  onDownload,
}: {
  result: MappingJobResponse
  drafts: Record<string, ReviewDraft>
  targetOptions: string[]
  targetOptionsAvailable: boolean
  isDirty: boolean
  saving: boolean
  downloading: MappingExportFormat | null
  mutationDisabled: boolean
  message: string | null
  error: string | null
  onDraftChange: (sourceField: string, draft: ReviewDraft) => void
  onSave: () => void
  onDownload: (format: MappingExportFormat) => void
}) {
  const summary = reviewSummaryFromDrafts(result, drafts)
  const canExport = summary.export_ready && !isDirty && !saving && downloading === null && !mutationDisabled
  const validationErrors = reviewValidationErrors(result, drafts)
  const canSave = validationErrors.length === 0 && !saving && !mutationDisabled
  return (
    <Stack gap="md">
      <Title order={3} size="h5">人工复核</Title>
      <SimpleGrid cols={{ base: 1, sm: 2, lg: 4 }} spacing="md">
        <StatCard label="已复核" value={`${summary.reviewed_fields} / ${summary.total_fields}`} />
        <StatCard label="待复核" value={summary.pending_fields} accent={summary.pending_fields ? STATUS.warning : STATUS.good} />
        <StatCard label="接受建议" value={summary.accepted_count} />
        <StatCard label="人工改选" value={summary.overridden_count} />
        <StatCard label="标记不映射" value={summary.unmapped_count} />
        <StatCard label="可导出" value={summary.export_ready ? 'Yes' : 'No'} accent={summary.export_ready ? STATUS.good : STATUS.warning} />
        <StatCard label="未保存修改" value={isDirty ? 'Yes' : 'No'} accent={isDirty ? STATUS.warning : STATUS.good} />
        <StatCard label="Review SHA" value={shortValue(result.job.mapping_report.content_sha256)} />
      </SimpleGrid>

      {message && <Alert color="green" variant="light">{message}</Alert>}
      <ErrorAlert message={error} />
      {validationErrors.length > 0 && (
        <Alert color="yellow" variant="light">
          {validationErrors[0]}
        </Alert>
      )}

      <Group gap="sm">
        <Button aria-label="Save review" onClick={onSave} loading={saving} disabled={!canSave}>
          保存复核
        </Button>
        <Button
          aria-label="Download JSON export"
          variant="light"
          onClick={() => onDownload('json')}
          loading={downloading === 'json'}
          disabled={!canExport}
        >
          下载 JSON
        </Button>
        <Button
          aria-label="Download CSV export"
          variant="light"
          onClick={() => onDownload('csv')}
          loading={downloading === 'csv'}
          disabled={!canExport}
        >
          下载 CSV
        </Button>
        {!summary.export_ready && (
          <Text size="sm" c="dimmed">还剩 {summary.pending_fields} 个字段待复核</Text>
        )}
        {isDirty && (
          <Text size="sm" c="orange">有未保存修改 · 请先保存复核后再导出</Text>
        )}
      </Group>

      <Accordion
        variant="separated"
        radius="md"
        multiple
        defaultValue={result.mappings.map((mapping) => `review-${mapping.source_field}`)}
      >
        {result.mappings.map((mapping) => {
          const draft = drafts[mapping.source_field] ?? emptyReviewDraft()
          const acceptDisabled = !mapping.recommendation
          const noteLength = draft.note.length
          const selectDisabled = !targetOptionsAvailable
          const visibleTargetOptions = targetOptionsAvailable ? targetOptions : draft.target_fields
          return (
            <Accordion.Item value={`review-${mapping.source_field}`} key={mapping.source_field}>
              <Accordion.Control>
                <Group justify="space-between" wrap="nowrap" pr="sm">
                  <Group gap="sm" wrap="nowrap" style={{ minWidth: 0 }}>
                    <Code>{mapping.source_field}</Code>
                    <Text size="sm" c="dimmed">{actionLabel(draft.action)}</Text>
                  </Group>
                  <Badge size="sm" variant="light">
                    {draft.action ? 'reviewed' : 'pending'}
                  </Badge>
                </Group>
              </Accordion.Control>
              <Accordion.Panel>
                <Stack gap="md">
                  <Group gap="sm">
                    <Badge variant="light" style={{ backgroundColor: `${statusColor(mapping.status)}22`, color: statusColor(mapping.status) }}>
                      {mapping.status}
                    </Badge>
                    <Text size="sm">推荐目标: <Code>{mapping.recommendation ?? '-'}</Code></Text>
                  </Group>

                  <Checkbox
                    label={mapping.recommendation ? `接受算法建议 ${mapping.recommendation}` : '接受算法建议不可用'}
                    checked={draft.action === 'accept_suggestion'}
                    disabled={acceptDisabled || mutationDisabled}
                    description={acceptDisabled ? '该字段没有可接受的 recommendation' : undefined}
                    onChange={(event) => {
                      onDraftChange(mapping.source_field, {
                        ...draft,
                        action: event.currentTarget.checked ? 'accept_suggestion' : '',
                        target_fields: [],
                      })
                    }}
                  />
                  <Checkbox
                    label="改选目标"
                    checked={draft.action === 'select_target'}
                    disabled={selectDisabled || mutationDisabled}
                    description={selectDisabled ? '当前 job 的 contract target allowlist 尚未加载或不匹配，不能猜测目标字段。' : undefined}
                    onChange={(event) => {
                      onDraftChange(mapping.source_field, {
                        ...draft,
                        action: event.currentTarget.checked ? 'select_target' : '',
                        target_fields: event.currentTarget.checked ? draft.target_fields : [],
                      })
                    }}
                  />
                  {draft.action === 'select_target' && (
                    <Checkbox.Group
                      label="目标字段"
                      description="来自当前 job contract 的完整 target field allowlist；Top-3 仅作为 evidence 展示。"
                      value={draft.target_fields}
                      onChange={(values) => onDraftChange(mapping.source_field, { ...draft, target_fields: values })}
                    >
                      <SimpleGrid cols={{ base: 1, md: 2 }} spacing={6} mt="xs">
                        {visibleTargetOptions.map((target) => (
                          <Checkbox key={`${mapping.source_field}-${target}`} value={target} label={target} disabled={!targetOptionsAvailable || mutationDisabled} />
                        ))}
                      </SimpleGrid>
                    </Checkbox.Group>
                  )}
                  <Checkbox
                    label="标记不映射"
                    checked={draft.action === 'mark_unmapped'}
                    disabled={mutationDisabled}
                    onChange={(event) => {
                      onDraftChange(mapping.source_field, {
                        ...draft,
                        action: event.currentTarget.checked ? 'mark_unmapped' : '',
                        target_fields: [],
                      })
                    }}
                  />
                  <Textarea
                    label="Note"
                    value={draft.note}
                    description={`${noteLength} / ${MAX_REVIEW_NOTE_LENGTH}`}
                    error={hasControlCharacter(draft.note) ? 'note must not contain control characters' : undefined}
                    maxLength={MAX_REVIEW_NOTE_LENGTH}
                    disabled={mutationDisabled}
                    onChange={(event) => onDraftChange(mapping.source_field, { ...draft, note: event.currentTarget.value })}
                  />
                  <CandidateTable candidates={(mapping.top_candidates ?? []).slice(0, 3)} />
                </Stack>
              </Accordion.Panel>
            </Accordion.Item>
          )
        })}
      </Accordion>
    </Stack>
  )
}

function reviewValidationErrors(result: MappingJobResponse, drafts: Record<string, ReviewDraft>): string[] {
  const errors: string[] = []
  for (const mapping of result.mappings) {
    const draft = drafts[mapping.source_field]
    if (!draft?.action) continue
    if (draft.action === 'select_target' && draft.target_fields.length === 0) {
      errors.push(`${mapping.source_field}: select_target requires at least one target field`)
    }
    if (hasControlCharacter(draft.note)) {
      errors.push(`${mapping.source_field}: note must not contain control characters`)
    }
  }
  return errors
}

function triggerDownload(download: { blob: Blob; filename: string }) {
  const url = URL.createObjectURL(download.blob)
  try {
    const link = document.createElement('a')
    link.href = url
    link.download = download.filename
    document.body.appendChild(link)
    link.click()
    link.remove()
  } finally {
    URL.revokeObjectURL(url)
  }
}

export function MappingJobView() {
  const [contracts, setContracts] = useState<MappingContractSummary[]>([])
  const [contractId, setContractId] = useState('')
  const [scorer, setScorer] = useState<MappingScorer>('precision_tiered_v5')
  const [file, setFile] = useState<File | null>(null)
  const [result, setResult] = useState<MappingJobResponse | null>(null)
  const [jobId, setJobId] = useState('')
  const [loading, setLoading] = useState(false)
  const [catalogLoading, setCatalogLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [reviewDrafts, setReviewDrafts] = useState<Record<string, ReviewDraft>>({})
  const [savedReviewSignature, setSavedReviewSignature] = useState(reviewDraftSignature({}, []))
  const [reviewMessage, setReviewMessage] = useState<string | null>(null)
  const [reviewError, setReviewError] = useState<string | null>(null)
  const [savingReview, setSavingReview] = useState(false)
  const [downloading, setDownloading] = useState<MappingExportFormat | null>(null)
  const [deletingJob, setDeletingJob] = useState(false)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const [deleteMessage, setDeleteMessage] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    getMappingContracts()
      .then((catalog) => {
        if (cancelled) return
        setContracts(catalog.contracts)
        setContractId(catalog.contracts[0]?.contract_id ?? '')
      })
      .catch((err) => !cancelled && setError(errorText(err)))
      .finally(() => !cancelled && setCatalogLoading(false))
    return () => {
      cancelled = true
    }
  }, [])

  const contractOptions = useMemo(
    () => contracts.map((contract) => ({ value: contract.contract_id, label: contract.title })),
    [contracts],
  )
  const resultContract = useMemo(
    () => (result ? contracts.find((contract) => contract.contract_id === result.job.contract_registry_id) ?? null : null),
    [contracts, result],
  )
  const targetOptions = resultContract?.target_fields ?? []
  const targetOptionsAvailable = targetOptions.length > 0
  const currentReviewSignature = useMemo(
    () => reviewDraftSignature(reviewDrafts, result?.mappings ?? []),
    [result?.mappings, reviewDrafts],
  )
  const hasUnsavedReview = currentReviewSignature !== savedReviewSignature
  const canRun = Boolean(file && contractId && !loading && !catalogLoading && !deletingJob)
  const canLoadJob = JOB_ID_PATTERN.test(jobId) && !loading && !deletingJob

  const resetReviewState = () => {
    setReviewDrafts({})
    setSavedReviewSignature(reviewDraftSignature({}, []))
    setReviewMessage(null)
    setReviewError(null)
    setSavingReview(false)
    setDownloading(null)
  }

  const clearDeleteState = () => {
    setDeleteError(null)
    setDeleteMessage(null)
  }

  const applyResult = (next: MappingJobResponse) => {
    const nextDrafts = Object.fromEntries(
      (next.review?.decisions ?? []).map((decision) => [decision.source_field, reviewDraftFromDecision(decision)]),
    )
    setResult(next)
    setReviewDrafts(nextDrafts)
    setSavedReviewSignature(reviewDraftSignature(nextDrafts, next.mappings))
    setReviewMessage(null)
    setReviewError(null)
    setDownloading(null)
    clearDeleteState()
  }

  const onFileChange = (next: File | null) => {
    setFile(next)
    setResult(null)
    setError(null)
    resetReviewState()
    clearDeleteState()
  }

  const validateFile = (selected: File): string | null => {
    if (!selected.name.toLowerCase().endsWith('.csv')) return 'invalid_mapping_filename · Filename must end with .csv'
    if (selected.size > MAX_CSV_BYTES) return 'mapping_csv_too_large · CSV must be at most 1 MiB'
    return null
  }

  const runJob = async () => {
    if (!file) {
      setError('invalid_mapping_filename · Select a CSV file before running')
      return
    }
    const invalid = validateFile(file)
    if (invalid) {
      setError(invalid)
      return
    }
    setLoading(true)
    setError(null)
    clearDeleteState()
    try {
      const csvText = await file.text()
      const next = await createMappingJob({
        contract_id: contractId,
        filename: file.name,
        csv_text: csvText,
        scorer,
      })
      applyResult(next)
      setJobId(next.job.job_id)
    } catch (err) {
      setError(errorText(err))
    } finally {
      setLoading(false)
    }
  }

  const loadJob = async () => {
    if (!JOB_ID_PATTERN.test(jobId)) {
      setError('invalid_mapping_job_id · Job ID must be 32 lowercase hex characters')
      return
    }
    setLoading(true)
    setError(null)
    clearDeleteState()
    try {
      applyResult(await getMappingJob(jobId))
    } catch (err) {
      setError(errorText(err))
    } finally {
      setLoading(false)
    }
  }

  const updateReviewDraft = (sourceField: string, draft: ReviewDraft) => {
    setReviewDrafts((current) => ({ ...current, [sourceField]: draft }))
    setReviewMessage(null)
    setReviewError(null)
  }

  const deleteCurrentJob = async () => {
    if (!result) return
    setDeletingJob(true)
    setDeleteError(null)
    setDeleteMessage(null)
    setReviewError(null)
    setReviewMessage(null)
    try {
      await deleteMappingJob(result.job.job_id, {
        mapping_report_sha256: result.job.mapping_report.content_sha256,
      })
      setResult(null)
      setJobId('')
      resetReviewState()
      setDeleteMessage('Mapping job deleted.')
    } catch (err) {
      setDeleteError(errorText(err))
    } finally {
      setDeletingJob(false)
    }
  }

  const saveReview = async () => {
    if (!result) return
    setSavingReview(true)
    setReviewError(null)
    setReviewMessage(null)
    try {
      const response = await saveMappingReview(result.job.job_id, {
        mapping_report_sha256: result.job.mapping_report.content_sha256,
        decisions: buildReviewDecisions(reviewDrafts, result.mappings),
      })
      const next = { ...result, review: response.review }
      const nextDrafts = Object.fromEntries(
        response.review.decisions.map((decision) => [decision.source_field, reviewDraftFromDecision(decision)]),
      )
      setResult(next)
      setReviewDrafts(nextDrafts)
      setSavedReviewSignature(reviewDraftSignature(nextDrafts, next.mappings))
      setReviewMessage('复核已保存')
    } catch (err) {
      setReviewError(errorText(err))
    } finally {
      setSavingReview(false)
    }
  }

  const downloadReview = async (format: MappingExportFormat) => {
    if (!result) return
    setDownloading(format)
    setReviewError(null)
    setReviewMessage(null)
    try {
      const download = await downloadMappingReviewExport(result.job.job_id, format)
      triggerDownload(download)
      setReviewMessage(`${format.toUpperCase()} 导出已开始`)
    } catch (err) {
      setReviewError(errorText(err))
    } finally {
      setDownloading(null)
    }
  }

  return (
    <Stack gap="xl">
      <div>
        <Title order={2} size="h3">新建字段映射</Title>
        <Text size="sm" c="dimmed" mt={4}>Upload CSV · Contract-aware Top-3 Ranking</Text>
      </div>

      <SimpleGrid cols={{ base: 1, lg: 2 }} spacing="lg" verticalSpacing="lg">
        <Stack gap="md">
          <FileInput
            label="CSV file"
            placeholder="Select a local .csv file"
            value={file}
            onChange={onFileChange}
            accept=".csv,text/csv"
            clearable
            disabled={loading || deletingJob}
          />
          <NativeSelect
            label="Contract"
            value={contractId}
            onChange={(event) => setContractId(event.currentTarget.value)}
            disabled={catalogLoading || contracts.length === 0 || loading || deletingJob}
            data={contractOptions}
          />
          <NativeSelect
            label="Scorer"
            value={scorer}
            onChange={(event) => setScorer(event.currentTarget.value as MappingScorer)}
            disabled={loading || deletingJob}
            data={[
              { value: 'precision_tiered_v5', label: 'Precision Tiered V5 — Identifier-aware' },
              { value: 'precision_tiered_v4', label: 'Precision Tiered V4' },
              { value: 'baseline', label: 'Baseline' },
            ]}
          />
          <Button onClick={runJob} disabled={!canRun} loading={loading}>
            Run mapping job
          </Button>
          {loading && (
            <Group gap="sm">
              <Loader size="sm" />
              <Text size="sm" c="dimmed">正在分析字段并生成 Top-3...</Text>
            </Group>
          )}
          <ConstraintNote />
        </Stack>

        <Stack gap="md">
          <TextInput
            label="Load existing job"
            placeholder="32 lowercase hex job ID"
            value={jobId}
            onChange={(event) => {
              setJobId(event.currentTarget.value.trim())
              setResult(null)
              setError(null)
              resetReviewState()
              clearDeleteState()
            }}
            disabled={deletingJob}
          />
          <Button variant="light" onClick={loadJob} disabled={!canLoadJob} loading={loading}>
            Load job
          </Button>
          {jobId && !JOB_ID_PATTERN.test(jobId) && (
            <Alert color="yellow" variant="light">invalid_mapping_job_id · Job ID must be 32 lowercase hex characters</Alert>
          )}
          <Alert color="gray" variant="light">
            GET reloads the saved job response and does not trigger rescoring or polling.
          </Alert>
        </Stack>
      </SimpleGrid>

      <ErrorAlert message={error} />
      {deleteMessage && <Alert color="green" variant="light">{deleteMessage}</Alert>}

      {result && (
        <Stack gap="xl">
          <JobSummary result={result} />
          <DeleteJobPanel
            result={result}
            deleting={deletingJob}
            error={deleteError}
            onDelete={deleteCurrentJob}
          />
          <MappingResults result={result} />
          <ReviewPanel
            result={result}
            drafts={reviewDrafts}
            targetOptions={targetOptions}
            targetOptionsAvailable={targetOptionsAvailable}
            isDirty={hasUnsavedReview}
            saving={savingReview}
            downloading={downloading}
            mutationDisabled={deletingJob}
            message={reviewMessage}
            error={reviewError}
            onDraftChange={updateReviewDraft}
            onSave={saveReview}
            onDownload={downloadReview}
          />
        </Stack>
      )}
    </Stack>
  )
}
