import { useEffect, useMemo, useState } from 'react'
import {
  Accordion,
  Alert,
  Badge,
  Box,
  Button,
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
  Title,
  Tooltip,
} from '@mantine/core'
import { ApiError, createMappingJob, getMappingContracts, getMappingJob } from '../api'
import { StatCard } from '../components/StatCard'
import type {
  MappingCandidate,
  MappingContractSummary,
  MappingJobResponse,
  MappingResult,
  MappingScorer,
  MappingSourceProfile,
} from '../lib/mappingJobs'
import { STATUS } from '../lib/theme'

const MAX_CSV_BYTES = 1024 * 1024
const JOB_ID_PATTERN = /^[0-9a-f]{32}$/

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
      <StatCard label="Scorer" value={job.scorer === 'precision_tiered_v4' ? 'Precision-Tiered V4' : 'Baseline'} hint={job.status} />
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
      <Accordion variant="separated" radius="md" chevronPosition="left" multiple>
        {result.mappings.map((mapping) => (
          <MappingItem key={mapping.source_field} mapping={mapping} />
        ))}
      </Accordion>
    </Stack>
  )
}

export function MappingJobView() {
  const [contracts, setContracts] = useState<MappingContractSummary[]>([])
  const [contractId, setContractId] = useState('')
  const [scorer, setScorer] = useState<MappingScorer>('precision_tiered_v4')
  const [file, setFile] = useState<File | null>(null)
  const [result, setResult] = useState<MappingJobResponse | null>(null)
  const [jobId, setJobId] = useState('')
  const [loading, setLoading] = useState(false)
  const [catalogLoading, setCatalogLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

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
  const canRun = Boolean(file && contractId && !loading && !catalogLoading)
  const canLoadJob = JOB_ID_PATTERN.test(jobId) && !loading

  const onFileChange = (next: File | null) => {
    setFile(next)
    setResult(null)
    setError(null)
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
    try {
      const csvText = await file.text()
      const next = await createMappingJob({
        contract_id: contractId,
        filename: file.name,
        csv_text: csvText,
        scorer,
      })
      setResult(next)
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
    try {
      setResult(await getMappingJob(jobId))
    } catch (err) {
      setError(errorText(err))
    } finally {
      setLoading(false)
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
          />
          <NativeSelect
            label="Contract"
            value={contractId}
            onChange={(event) => setContractId(event.currentTarget.value)}
            disabled={catalogLoading || contracts.length === 0}
            data={contractOptions}
          />
          <NativeSelect
            label="Scorer"
            value={scorer}
            onChange={(event) => setScorer(event.currentTarget.value as MappingScorer)}
            data={[
              { value: 'precision_tiered_v4', label: 'Precision-Tiered V4, experimental, review required' },
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
              setError(null)
            }}
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

      {result && (
        <Stack gap="xl">
          <JobSummary result={result} />
          <MappingResults result={result} />
        </Stack>
      )}
    </Stack>
  )
}
