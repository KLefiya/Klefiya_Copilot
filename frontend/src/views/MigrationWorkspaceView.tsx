import { useEffect, useMemo, useState } from 'react'
import {
  Accordion,
  Alert,
  Badge,
  Box,
  Button,
  Checkbox,
  Code,
  Group,
  Loader,
  NativeSelect,
  Paper,
  SegmentedControl,
  SimpleGrid,
  Stack,
  Table,
  Tabs,
  Text,
  Textarea,
  Title,
  Tooltip,
} from '@mantine/core'
import {
  ApiError,
  buildMigrationPackage,
  getMigrationLineage,
  getMigrationResource,
  getMigrationWorkspace,
  resetMigrationWorkspace,
  saveMigrationDecisions,
} from '../api'
import { StatCard } from '../components/StatCard'
import {
  canonicalReviewState,
  countApprovedLinks,
  countUniqueApprovedSources,
  decisionsToReviewState,
  findTargetConflict,
  reviewStateToDecisions,
  type DecisionMode,
  type MigrationLineageResponse,
  type MigrationMapping,
  type MigrationResourcePreview,
  type MigrationWorkspaceDetail,
  type ReviewState,
} from '../lib/migrationWorkspace'
import { STATUS } from '../lib/theme'

const WORKSPACE_ID = 'erpnext-item-price'
const RESOURCE_NAMES = ['item', 'item_price'] as const

function errorText(error: unknown) {
  if (error instanceof ApiError) {
    const detail = error.detail
    if (typeof detail === 'object' && detail !== null) {
      const d = detail as { error?: string; message?: string; decision_error?: { code?: string } }
      return [d.error, d.decision_error?.code, d.message].filter(Boolean).join(' · ')
    }
    return error.message
  }
  return String(error)
}

function shaShort(value: string | null | undefined) {
  if (!value) return '-'
  return `${value.slice(0, 10)}...${value.slice(-6)}`
}

function valueSha(value: string) {
  return `${value.slice(0, 8)}...${value.slice(-6)}`
}

function initialState(detail: MigrationWorkspaceDetail): ReviewState {
  const state = decisionsToReviewState(detail.decisions)
  for (const mapping of detail.mappings) {
    state[mapping.source_field] ??= { mode: 'deferred', targets: [], reason: '' }
  }
  return canonicalReviewState(state)
}

function stateKey(state: ReviewState) {
  return JSON.stringify(canonicalReviewState(state))
}

function Summary({ detail }: { detail: MigrationWorkspaceDetail }) {
  const validation = detail.build.validation
  return (
    <SimpleGrid cols={{ base: 1, sm: 2, md: 4 }} spacing="md">
      <StatCard label="Source Rows" value={detail.summary.source_rows} />
      <StatCard label="Source Fields" value={detail.summary.source_fields} />
      <StatCard label="Target Fields" value={detail.summary.target_fields} />
      <StatCard label="Approved Links" value={detail.summary.approved_links} accent={STATUS.good} />
      <StatCard label="Multi-target Sources" value={detail.summary.multi_target_sources} accent={STATUS.warning} />
      <StatCard
        label="Decision Source"
        value={detail.workspace.decision_source === 'runtime' ? 'Runtime' : 'Seed'}
        hint={detail.workspace.decision_source === 'runtime' ? '本地 Runtime 审批' : '示例审批基线'}
      />
      <StatCard
        label="Generated Validation"
        value={validation ? (validation.valid ? 'valid' : 'invalid') : 'not built'}
        accent={validation?.valid ? STATUS.good : validation ? STATUS.critical : undefined}
      />
      <StatCard label="Workspace" value={detail.workspace.workspace_id} hint={detail.workspace.domain} />
    </SimpleGrid>
  )
}

function CandidateRows({
  mapping,
  review,
  state,
  onToggleTarget,
}: {
  mapping: MigrationMapping
  review: ReviewState[string]
  state: ReviewState
  onToggleTarget: (target: string, checked: boolean) => void
}) {
  return (
    <Table striped highlightOnHover verticalSpacing="sm" miw={920}>
      <Table.Thead>
        <Table.Tr>
          <Table.Th>Approve</Table.Th>
          <Table.Th>Target</Table.Th>
          <Table.Th>Rank</Table.Th>
          <Table.Th>Score</Table.Th>
          <Table.Th>Semantic</Table.Th>
          <Table.Th>Fuzzy</Table.Th>
          <Table.Th>Type Gate</Table.Th>
          <Table.Th>Warnings</Table.Th>
        </Table.Tr>
      </Table.Thead>
      <Table.Tbody>
        {mapping.top_candidates.map((candidate) => {
          const conflict = findTargetConflict(state, mapping.source_field, candidate.target)
          const checked = review.mode === 'approved' && review.targets.includes(candidate.target)
          return (
            <Table.Tr key={candidate.target}>
              <Table.Td>
                <Checkbox
                  aria-label={`${mapping.source_field} ${candidate.target}`}
                  checked={checked}
                  disabled={Boolean(conflict)}
                  onChange={(event) => onToggleTarget(candidate.target, event.currentTarget.checked)}
                />
              </Table.Td>
              <Table.Td>
                <Stack gap={2}>
                  <Code>{candidate.target}</Code>
                  <Text size="xs" c="dimmed">
                    {candidate.target_resource} · {candidate.target_field}
                  </Text>
                  {conflict && (
                    <Badge color="red" variant="light">
                      conflict: {conflict}
                    </Badge>
                  )}
                </Stack>
              </Table.Td>
              <Table.Td>{candidate.rank}</Table.Td>
              <Table.Td ff="monospace">{candidate.score?.toFixed(4)}</Table.Td>
              <Table.Td ff="monospace">{candidate.semantic_score?.toFixed(4)}</Table.Td>
              <Table.Td ff="monospace">{candidate.fuzzy_score?.toFixed(4)}</Table.Td>
              <Table.Td ff="monospace">{candidate.type_gate?.toFixed(2)}</Table.Td>
              <Table.Td>{candidate.warnings?.length ? candidate.warnings.join(', ') : '-'}</Table.Td>
            </Table.Tr>
          )
        })}
      </Table.Tbody>
    </Table>
  )
}

function MappingReview({
  mappings,
  state,
  setState,
}: {
  mappings: MigrationMapping[]
  state: ReviewState
  setState: (state: ReviewState) => void
}) {
  const updateReview = (source: string, patch: Partial<ReviewState[string]>) => {
    const current = state[source] ?? { mode: 'deferred', targets: [], reason: '' }
    const next = { ...current, ...patch }
    if (next.mode !== 'approved') next.targets = []
    setState(canonicalReviewState({ ...state, [source]: next }))
  }

  return (
    <Paper p="lg" radius="md" withBorder>
      <Group justify="space-between" mb="md">
        <Title order={3} size="h5">
          Mapping Review
        </Title>
        <Group gap="xs">
          <Badge variant="light">Approved links {countApprovedLinks(state)}</Badge>
          <Badge variant="light">Approved sources {countUniqueApprovedSources(state)}</Badge>
        </Group>
      </Group>
      <Accordion variant="separated" radius="md" chevronPosition="left" multiple>
        {mappings.map((mapping) => {
          const review = state[mapping.source_field] ?? { mode: 'deferred', targets: [], reason: '' }
          return (
            <Accordion.Item key={mapping.source_field} value={mapping.source_field}>
              <Accordion.Control>
                <Group justify="space-between" gap="md">
                  <Group gap="sm">
                    <Code>{mapping.source_field}</Code>
                    <Badge variant="light" color={review.mode === 'approved' ? 'green' : review.mode === 'rejected' ? 'red' : 'yellow'}>
                      {review.mode}
                    </Badge>
                    <Badge variant="outline">{review.targets.length} targets</Badge>
                  </Group>
                  <Text size="xs" c="dimmed">
                    {mapping.status}
                  </Text>
                </Group>
              </Accordion.Control>
              <Accordion.Panel>
                <Stack gap="md">
                  <SimpleGrid cols={{ base: 1, md: 4 }} spacing="sm">
                    <Text size="sm">
                      <Text span c="dimmed">Kind: </Text>
                      {mapping.source_profile.inferred_kind}
                    </Text>
                    <Text size="sm">
                      <Text span c="dimmed">Rows: </Text>
                      {mapping.source_profile.row_count}
                    </Text>
                    <Text size="sm">
                      <Text span c="dimmed">Distinct: </Text>
                      {mapping.source_profile.distinct_count}
                    </Text>
                    <Text size="sm">
                      <Text span c="dimmed">Samples: </Text>
                      {mapping.source_profile.samples.slice(0, 3).join(', ')}
                    </Text>
                  </SimpleGrid>
                  <SegmentedControl
                    value={review.mode}
                    onChange={(value) => updateReview(mapping.source_field, { mode: value as DecisionMode })}
                    data={[
                      { value: 'approved', label: 'Approve selected targets' },
                      { value: 'rejected', label: 'Reject: no target' },
                      { value: 'deferred', label: 'Defer review' },
                    ]}
                  />
                  <Box style={{ overflowX: 'auto' }}>
                    <CandidateRows
                      mapping={mapping}
                      review={review}
                      state={state}
                      onToggleTarget={(target, checked) => {
                        const targets = checked
                          ? [...review.targets, target]
                          : review.targets.filter((item) => item !== target)
                        updateReview(mapping.source_field, { mode: 'approved', targets })
                      }}
                    />
                  </Box>
                  <Textarea
                    label="Review reason"
                    value={review.reason}
                    onChange={(event) => updateReview(mapping.source_field, { reason: event.currentTarget.value })}
                    minRows={2}
                  />
                </Stack>
              </Accordion.Panel>
            </Accordion.Item>
          )
        })}
      </Accordion>
    </Paper>
  )
}

function BuildResult({ detail }: { detail: MigrationWorkspaceDetail }) {
  if (!detail.build.available) {
    return (
      <Alert color="gray" variant="light">
        Runtime package has not been generated yet.
      </Alert>
    )
  }
  const summary = detail.build.summary
  const validation = detail.build.validation
  return (
    <Paper p="lg" radius="md" withBorder>
      <Title order={3} size="h5" mb="md">
        Build Result
      </Title>
      <SimpleGrid cols={{ base: 1, sm: 2, md: 4 }} spacing="md">
        <StatCard label="Build Status" value={summary?.build_status ?? '-'} />
        <StatCard label="Resources Generated" value={summary?.resources_generated ?? 0} />
        <StatCard label="Rows Generated" value={summary?.rows_generated ?? 0} />
        <StatCard label="Rejected Rows" value={summary?.rejected_rows ?? 0} />
        <StatCard label="Lineage Entries" value={summary?.lineage_entries ?? 0} />
        <StatCard
          label="Validation Valid"
          value={validation?.valid ? 'true' : 'false'}
          accent={validation?.valid ? STATUS.good : STATUS.critical}
        />
        <StatCard label="Validation Findings" value={validation?.finding_count ?? 0} />
        <StatCard label="Manifest SHA" value={shaShort(detail.build.manifest?.content_sha256)} />
        <StatCard label="Build Report SHA" value={shaShort(detail.build.build_report_sha256)} />
      </SimpleGrid>
    </Paper>
  )
}

function ResourcePreview({ previews }: { previews: Record<string, MigrationResourcePreview | null> }) {
  return (
    <Paper p="lg" radius="md" withBorder>
      <Title order={3} size="h5" mb="md">
        Resource Preview
      </Title>
      <Tabs defaultValue="item">
        <Tabs.List>
          <Tabs.Tab value="item">Item</Tabs.Tab>
          <Tabs.Tab value="item_price">Item Price</Tabs.Tab>
        </Tabs.List>
        {RESOURCE_NAMES.map((name) => {
          const preview = previews[name]
          return (
            <Tabs.Panel key={name} value={name} pt="md">
              {!preview?.available ? (
                <Text size="sm" c="dimmed">No runtime build available.</Text>
              ) : (
                <Stack gap="sm">
                  <Text size="sm" c="dimmed">
                    {preview.total_rows} rows · {preview.columns.length} columns · {shaShort(preview.content_sha256)}
                  </Text>
                  <Box style={{ overflowX: 'auto' }}>
                    <Table striped highlightOnHover miw={700}>
                      <Table.Thead>
                        <Table.Tr>
                          {preview.columns.map((column) => (
                            <Table.Th key={column}>{column}</Table.Th>
                          ))}
                        </Table.Tr>
                      </Table.Thead>
                      <Table.Tbody>
                        {preview.rows.map((row, index) => (
                          <Table.Tr key={`${name}-${index}`}>
                            {preview.columns.map((column) => (
                              <Table.Td key={column}>{row[column]}</Table.Td>
                            ))}
                          </Table.Tr>
                        ))}
                      </Table.Tbody>
                    </Table>
                  </Box>
                </Stack>
              )}
            </Tabs.Panel>
          )
        })}
      </Tabs>
    </Paper>
  )
}

function LineageExplorer({
  detail,
  lineage,
  sourceFilter,
  setSourceFilter,
}: {
  detail: MigrationWorkspaceDetail
  lineage: MigrationLineageResponse | null
  sourceFilter: string
  setSourceFilter: (value: string) => void
}) {
  return (
    <Paper p="lg" radius="md" withBorder>
      <Group justify="space-between" mb="md">
        <Title order={3} size="h5">
          Lineage Explorer
        </Title>
        <NativeSelect
          aria-label="Source Field Filter"
          value={sourceFilter}
          onChange={(event) => setSourceFilter(event.currentTarget.value)}
          data={[
            { value: '', label: 'All source fields' },
            ...detail.mappings.map((mapping) => ({ value: mapping.source_field, label: mapping.source_field })),
          ]}
        />
      </Group>
      {!lineage?.available ? (
        <Text size="sm" c="dimmed">No lineage has been generated yet.</Text>
      ) : (
        <Stack gap="sm">
          <Text size="sm" c="dimmed">
            {lineage.matched_entries} matched · {lineage.total_entries} total
          </Text>
          <Box style={{ overflowX: 'auto' }}>
            <Table striped highlightOnHover miw={900}>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>Source Row</Table.Th>
                  <Table.Th>Source Record ID</Table.Th>
                  <Table.Th>Source Field</Table.Th>
                  <Table.Th>Target Resource</Table.Th>
                  <Table.Th>Target Row</Table.Th>
                  <Table.Th>Target Field</Table.Th>
                  <Table.Th>Transformation</Table.Th>
                  <Table.Th>Value SHA</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {lineage.entries.map((entry, index) => (
                  <Table.Tr key={`${entry.source_field}-${entry.target_resource}-${entry.target_row_number}-${index}`}>
                    <Table.Td>{entry.source_row_number}</Table.Td>
                    <Table.Td>{entry.source_record_id}</Table.Td>
                    <Table.Td><Code>{entry.source_field}</Code></Table.Td>
                    <Table.Td>{entry.target_resource}</Table.Td>
                    <Table.Td>{entry.target_row_number}</Table.Td>
                    <Table.Td>{entry.target_field}</Table.Td>
                    <Table.Td>{entry.transformation_type}</Table.Td>
                    <Table.Td>
                      <Tooltip label={entry.source_value_sha256}>
                        <Code>{valueSha(entry.source_value_sha256)}</Code>
                      </Tooltip>
                    </Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </Box>
        </Stack>
      )}
    </Paper>
  )
}

export function MigrationWorkspaceView() {
  const [detail, setDetail] = useState<MigrationWorkspaceDetail | null>(null)
  const [savedKey, setSavedKey] = useState('')
  const [state, setState] = useState<ReviewState>({})
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [previews, setPreviews] = useState<Record<string, MigrationResourcePreview | null>>({ item: null, item_price: null })
  const [lineage, setLineage] = useState<MigrationLineageResponse | null>(null)
  const [sourceFilter, setSourceFilter] = useState('')

  const currentKey = useMemo(() => stateKey(state), [state])
  const dirty = currentKey !== savedKey
  const hasInvalidApproval = Object.values(state).some((review) => review.mode === 'approved' && review.targets.length === 0)

  const loadWorkspace = async () => {
    const next = (await getMigrationWorkspace(WORKSPACE_ID)) as MigrationWorkspaceDetail
    const nextState = initialState(next)
    setDetail(next)
    setState(nextState)
    setSavedKey(stateKey(nextState))
    return next
  }

  const loadRuntimeArtifacts = async (nextDetail: MigrationWorkspaceDetail, filter = sourceFilter) => {
    if (!nextDetail.build.available) {
      setPreviews({ item: null, item_price: null })
      setLineage(null)
      return
    }
    const [item, itemPrice, nextLineage] = await Promise.all([
      getMigrationResource(WORKSPACE_ID, 'item', 20),
      getMigrationResource(WORKSPACE_ID, 'item_price', 20),
      getMigrationLineage(WORKSPACE_ID, { source_field: filter || undefined, limit: 100 }),
    ])
    setPreviews({ item: item as MigrationResourcePreview, item_price: itemPrice as MigrationResourcePreview })
    setLineage(nextLineage as MigrationLineageResponse)
  }

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    loadWorkspace()
      .then((next) => {
        if (!cancelled) return loadRuntimeArtifacts(next)
      })
      .catch((err) => !cancelled && setError(errorText(err)))
      .finally(() => !cancelled && setLoading(false))
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    if (!detail?.build.available) return
    getMigrationLineage(WORKSPACE_ID, { source_field: sourceFilter || undefined, limit: 100 })
      .then((next) => setLineage(next as MigrationLineageResponse))
      .catch((err) => setError(errorText(err)))
  }, [detail?.build.available, sourceFilter])

  const save = async () => {
    if (!detail) return
    setBusy('save')
    setError(null)
    try {
      const payload = {
        expected_mapping_content_sha256: detail.workspace.mapping_content_sha256,
        expected_decision_sha256: detail.workspace.decision_sha256,
        decisions: reviewStateToDecisions(state),
      }
      const next = (await saveMigrationDecisions(WORKSPACE_ID, payload)) as MigrationWorkspaceDetail
      const nextState = initialState(next)
      setDetail(next)
      setState(nextState)
      setSavedKey(stateKey(nextState))
      setMessage('审批已保存。')
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(null)
    }
  }

  const build = async () => {
    if (!detail) return
    setBusy('build')
    setError(null)
    try {
      const next = (await buildMigrationPackage(WORKSPACE_ID, {
        expected_mapping_content_sha256: detail.workspace.mapping_content_sha256,
        expected_decision_sha256: detail.workspace.decision_sha256,
      })) as MigrationWorkspaceDetail
      setDetail(next)
      await loadRuntimeArtifacts(next)
      setMessage('迁移包已生成。')
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(null)
    }
  }

  const reset = async () => {
    if (!window.confirm('Reset local runtime state and return to the seed decision?')) return
    setBusy('reset')
    setError(null)
    try {
      const next = (await resetMigrationWorkspace(WORKSPACE_ID)) as MigrationWorkspaceDetail
      const nextState = initialState(next)
      setDetail(next)
      setState(nextState)
      setSavedKey(stateKey(nextState))
      setPreviews({ item: null, item_price: null })
      setLineage(null)
      setMessage('已恢复示例审批基线。')
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(null)
    }
  }

  if (loading) {
    return (
      <Paper p="lg" radius="md" withBorder>
        <Group gap="sm">
          <Loader size="sm" />
          <Text size="sm" c="dimmed">Loading migration workspace</Text>
        </Group>
      </Paper>
    )
  }

  if (!detail) {
    return <Alert color="red" title="Workspace unavailable">{error}</Alert>
  }

  return (
    <Stack gap="xl">
      <div>
        <Title order={2} size="h3">
          迁移工作台
        </Title>
        <Text size="sm" c="dimmed" mt={4}>
          ERPNext Item + Item Price · Human-in-the-loop Review
        </Text>
      </div>

      <Alert color="blue" variant="light">
        候选由算法生成，最终映射由人工批准。一个 Source 可以批准到多个 Top-3 Target。
      </Alert>

      {message && <Alert color="green" variant="light">{message}</Alert>}
      {error && <Alert color="red" variant="light">{error}</Alert>}

      <Paper p="lg" radius="md" withBorder>
        <Group justify="space-between" align="flex-start">
          <Stack gap={4}>
            <Title order={3} size="h5">{detail.workspace.title}</Title>
            <Text size="sm" c="dimmed">{detail.workspace.description}</Text>
            <Group gap="xs">
              <Code>{detail.workspace.contract_id}</Code>
              <Tooltip label={detail.workspace.decision_sha256}>
                <Badge variant="outline">Decision {shaShort(detail.workspace.decision_sha256)}</Badge>
              </Tooltip>
              <Tooltip label={detail.workspace.mapping_content_sha256}>
                <Badge variant="outline">Mapping {shaShort(detail.workspace.mapping_content_sha256)}</Badge>
              </Tooltip>
            </Group>
          </Stack>
          <Group gap="sm">
            <Button onClick={save} loading={busy === 'save'} disabled={!dirty || hasInvalidApproval}>
              保存审批
            </Button>
            <Button onClick={build} loading={busy === 'build'} disabled={dirty || hasInvalidApproval} variant="light">
              生成迁移包
            </Button>
            <Button onClick={reset} loading={busy === 'reset'} variant="outline" color="red">
              重置本地状态
            </Button>
          </Group>
        </Group>
        {hasInvalidApproval && (
          <Alert color="yellow" variant="light" mt="md">
            Approved mode requires at least one selected target.
          </Alert>
        )}
      </Paper>

      <Summary detail={detail} />
      <MappingReview mappings={detail.mappings} state={state} setState={setState} />
      <BuildResult detail={detail} />
      <ResourcePreview previews={previews} />
      <LineageExplorer detail={detail} lineage={lineage} sourceFilter={sourceFilter} setSourceFilter={setSourceFilter} />
    </Stack>
  )
}
