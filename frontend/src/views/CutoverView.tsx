import { Fragment, useMemo, useState } from 'react'
import {
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
  Progress,
  SimpleGrid,
  Stack,
  Table,
  Text,
  TextInput,
  Title,
  Tooltip,
} from '@mantine/core'

import { StatCard } from '../components/StatCard'
import { STATUS } from '../lib/theme'
import { notGeneratedInfo, useReport } from '../lib/useReport'
import type {
  CutoverAgentTrace,
  CutoverDailyReport,
  CutoverPlanReport,
  CutoverStatusReport,
} from '../lib/reports'

const EMPTY = '-'

function shortSha(value?: string | null) {
  if (!value) return EMPTY
  return `${value.slice(0, 12)}...${value.slice(-7)}`
}

function shaLabel(value?: string | null) {
  if (!value) return <Code>{EMPTY}</Code>
  return (
    <Tooltip label={value} multiline w={420}>
      <Code>{shortSha(value)}</Code>
    </Tooltip>
  )
}

function statusColor(status: string) {
  if (status === 'Completed' || status === 'Approved' || status === 'Resolved' || status === 'Green') return 'green'
  if (status === 'Blocked' || status === 'Rejected' || status === 'Open' || status === 'Red') return 'red'
  if (status === 'In Progress' || status === 'Ready' || status === 'Mitigating') return 'blue'
  if (status === 'Amber' || status === 'Pending' || status === 'Accepted') return 'yellow'
  return 'gray'
}

function statusAccent(status: string) {
  if (status === 'Red' || status === 'Blocked') return STATUS.critical
  if (status === 'Amber') return STATUS.warning
  if (status === 'Green' || status === 'Completed') return STATUS.good
  return undefined
}

function statusBadge(status: string) {
  return (
    <Badge size="sm" variant="light" color={statusColor(status)}>
      {status}
    </Badge>
  )
}

function missingSection(title: string, message: string) {
  return (
    <Alert color="gray" variant="light" title={title}>
      <Text size="sm">{message}</Text>
    </Alert>
  )
}

function LoadingBlock() {
  return (
    <Paper p="lg" radius="md" withBorder>
      <Group gap="sm">
        <Loader size="sm" />
        <Text size="sm" c="dimmed">
          正在读取模块三报告
        </Text>
      </Group>
    </Paper>
  )
}

function MissingReport({ title, error }: { title: string; error: Error }) {
  const pending = 'detail' in error ? notGeneratedInfo(error as Parameters<typeof notGeneratedInfo>[0]) : null
  if (pending) {
    return (
      <Alert color="gray" variant="light" title={`${title}尚未生成`}>
        <Text size="sm">预计路径：{pending.expectedPath}</Text>
        <Text size="sm">生成命令：{pending.generatedBy}</Text>
      </Alert>
    )
  }
  return (
    <Alert color="red" variant="light" title={`${title}读取失败`}>
      <Text size="sm">{error.message}</Text>
    </Alert>
  )
}

function RuntimeInfo({
  plan,
  status,
  daily,
  trace,
}: {
  plan: CutoverPlanReport | null
  status: CutoverStatusReport | null
  daily: CutoverDailyReport | null
  trace: CutoverAgentTrace | null
}) {
  return (
    <Paper p="lg" radius="md" withBorder>
      <SimpleGrid cols={{ base: 1, sm: 2, md: 4 }} spacing="sm">
        <Text size="sm">
          <Text span c="dimmed">As of: </Text>
          <Code>{daily?.headline.as_of_offset ?? status?.as_of_offset ?? EMPTY}</Code>
        </Text>
        <Text size="sm">
          <Text span c="dimmed">Plan SHA: </Text>
          {shaLabel(plan?._run_info.content_sha256)}
        </Text>
        <Text size="sm">
          <Text span c="dimmed">Status SHA: </Text>
          {shaLabel(status?._run_info.content_sha256)}
        </Text>
        <Text size="sm">
          <Text span c="dimmed">Daily SHA: </Text>
          {shaLabel(daily?._run_info.content_sha256)}
        </Text>
        <Text size="sm">
          <Text span c="dimmed">Agent Trace SHA: </Text>
          {shaLabel(trace?._run_info.content_sha256)}
        </Text>
        <Group gap="xs">
          <Badge variant="light" color="gray">Synthetic</Badge>
          <Badge variant="light" color="gray">Read-only</Badge>
        </Group>
      </SimpleGrid>
      <Alert color="blue" variant="light" mt="md">
        <Stack gap={4}>
          <Text size="sm">全部为合成数据。</Text>
          <Text size="sm">页面只读，不会触发 Agent、MCP rebuild 或状态写入。</Text>
        </Stack>
      </Alert>
    </Paper>
  )
}

function ManagementOverview({ daily, status }: { daily: CutoverDailyReport; status: CutoverStatusReport | null }) {
  const summary = daily.progress_summary
  const rag = daily.headline.overall_rag
  return (
    <Stack gap="md">
      <SimpleGrid cols={{ base: 1, sm: 2, md: 6 }} spacing="md">
        <StatCard label="Overall RAG" value={rag} accent={statusAccent(rag)} />
        <StatCard label="活动总数" value={summary.activities_total} />
        <StatCard label="已完成" value={summary.activity_status_counts.Completed} accent={STATUS.good} />
        <StatCard label="被阻塞" value={summary.activity_status_counts.Blocked} accent={STATUS.critical} />
        <StatCard label="尚未开始" value={summary.activity_status_counts['Not Started']} />
        <StatCard label="工作包被阻塞" value={summary.work_package_status_counts.Blocked} accent={STATUS.critical} />
      </SimpleGrid>
      <Paper p="lg" radius="md" withBorder>
        <Group justify="space-between" mb="xs">
          <Text fw={600}>Activity completion percent</Text>
          <Text ff="monospace">{summary.activity_completion_percent.toFixed(2)}%</Text>
        </Group>
        <Progress value={summary.activity_completion_percent} color={statusColor(rag)} />
        {status && (
          <Text size="xs" c="dimmed" mt="sm">
            状态快照应用事件：{status.events_applied_count}
          </Text>
        )}
      </Paper>
    </Stack>
  )
}

function NextGate({ daily }: { daily: CutoverDailyReport }) {
  const gate = daily.headline.next_gate
  return (
    <Paper p="lg" radius="md" withBorder>
      <Title order={3} size="h5" mb="md">下一审批门</Title>
      <SimpleGrid cols={{ base: 1, md: 3 }} spacing="sm">
        <Text size="sm"><Text span c="dimmed">Gate: </Text><Code>{gate.gate_id}</Code></Text>
        <Group gap="xs"><Text size="sm" c="dimmed">Status:</Text>{statusBadge(gate.current_status)}</Group>
        <Text size="sm"><Text span c="dimmed">Due: </Text><Code>{gate.due_offset}</Code></Text>
        <Text size="sm"><Text span c="dimmed">Name: </Text>{gate.name}</Text>
        <Text size="sm"><Text span c="dimmed">Readiness: </Text>{gate.readiness ? 'Ready' : 'Not ready'}</Text>
      </SimpleGrid>
      <Text size="sm" fw={600} mt="md">Missing readiness criteria</Text>
      <Stack gap={4} mt={4}>
        {gate.missing_readiness_criteria.map((item) => (
          <Text key={item} size="sm">- {item}</Text>
        ))}
      </Stack>
    </Paper>
  )
}

function CriticalBlockers({ daily }: { daily: CutoverDailyReport }) {
  return (
    <Paper p="lg" radius="md" withBorder>
      <Title order={3} size="h5" mb="md">关键阻塞 · {daily.critical_blockers.length}</Title>
      {daily.critical_blockers.length === 0 ? (
        <Text size="sm" c="dimmed">当前没有 Day-1 关键路径阻塞。</Text>
      ) : (
        <Stack gap="sm">
          {daily.critical_blockers.map((item) => (
            <Alert key={item.activity_id} color="red" variant="light" title={item.activity_id}>
              <Text size="sm" fw={600}>{item.title}</Text>
              <Text size="sm">{item.blocker}</Text>
              <Text size="xs" c="dimmed">
                {item.owner_role} · {item.end_offset}
                {item.source_requirement_id ? ` · ${item.source_requirement_id}` : ''}
              </Text>
            </Alert>
          ))}
        </Stack>
      )}
    </Paper>
  )
}

function ManagementActions({ daily }: { daily: CutoverDailyReport }) {
  return (
    <Paper p="lg" radius="md" withBorder>
      <Group justify="space-between" mb="md">
        <Title order={3} size="h5">管理行动 · {daily.management_actions.length}</Title>
        <Badge variant="light" color="gray">由确定性日报规则生成</Badge>
      </Group>
      <Stack gap="sm">
        {daily.management_actions.map((item) => (
          <Paper key={`${item.priority}-${item.source_id}`} p="sm" radius="sm" withBorder>
            <Group justify="space-between" align="flex-start">
              <Stack gap={2}>
                <Text size="sm" fw={600}>{item.action}</Text>
                <Text size="xs" c="dimmed">
                  {item.source_type} · {item.source_id} · {item.owner_role}
                </Text>
              </Stack>
              <Badge color="blue" variant="light">P{item.priority}</Badge>
            </Group>
          </Paper>
        ))}
      </Stack>
    </Paper>
  )
}

function ActivitiesTable({ plan, status }: { plan: CutoverPlanReport | null; status: CutoverStatusReport }) {
  const [statusFilter, setStatusFilter] = useState('all')
  const [ownerFilter, setOwnerFilter] = useState('all')
  const [workstreamFilter, setWorkstreamFilter] = useState('all')
  const [criticalOnly, setCriticalOnly] = useState(false)
  const [query, setQuery] = useState('')
  const [expanded, setExpanded] = useState<string | null>(null)

  const planById = useMemo(() => new Map(plan?.activities.map((item) => [item.activity_id, item]) ?? []), [plan])
  const ownerRoles = Array.from(new Set(status.activities.map((item) => item.owner_role))).sort()
  const workstreams = Array.from(new Set(status.activities.map((item) => item.workstream))).sort()

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return status.activities.filter((activity) => {
      const planActivity = planById.get(activity.activity_id)
      const text = [
        activity.activity_id,
        activity.title,
        activity.owner_role,
        activity.workstream,
        planActivity?.source_requirement_id ?? '',
        activity.blocker,
      ].join(' ').toLowerCase()
      return (
        (statusFilter === 'all' || activity.current_status === statusFilter) &&
        (ownerFilter === 'all' || activity.owner_role === ownerFilter) &&
        (workstreamFilter === 'all' || activity.workstream === workstreamFilter) &&
        (!criticalOnly || activity.is_critical_to_day1) &&
        (!needle || text.includes(needle))
      )
    })
  }, [criticalOnly, ownerFilter, planById, query, status.activities, statusFilter, workstreamFilter])

  return (
    <Paper p="lg" radius="md" withBorder>
      <Group justify="space-between" mb="md">
        <Title order={3} size="h5">活动状态 · {filtered.length}</Title>
        <Text size="xs" c="dimmed">{filtered.length} / {status.activities.length}</Text>
      </Group>
      <SimpleGrid cols={{ base: 1, sm: 2, md: 5 }} spacing="sm" mb="md">
        <NativeSelect label="Status" value={statusFilter} onChange={(event) => setStatusFilter(event.currentTarget.value)} data={['all', 'Completed', 'Blocked', 'In Progress', 'Not Started', 'Cancelled']} />
        <NativeSelect label="Owner Role" value={ownerFilter} onChange={(event) => setOwnerFilter(event.currentTarget.value)} data={['all', ...ownerRoles]} />
        <NativeSelect label="Workstream" value={workstreamFilter} onChange={(event) => setWorkstreamFilter(event.currentTarget.value)} data={['all', ...workstreams]} />
        <TextInput label="文本搜索" value={query} onChange={(event) => setQuery(event.currentTarget.value)} placeholder="Activity / title / blocker" />
        <Checkbox label="Critical only" checked={criticalOnly} onChange={(event) => setCriticalOnly(event.currentTarget.checked)} mt={30} />
      </SimpleGrid>
      <Box style={{ overflowX: 'auto' }}>
        <Table striped highlightOnHover verticalSpacing="sm" miw={1100}>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Activity</Table.Th>
              <Table.Th>Title</Table.Th>
              <Table.Th>Workstream</Table.Th>
              <Table.Th>Owner</Table.Th>
              <Table.Th>Window</Table.Th>
              <Table.Th>Status</Table.Th>
              <Table.Th>Progress</Table.Th>
              <Table.Th>Critical</Table.Th>
              <Table.Th />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {filtered.length === 0 && (
              <Table.Tr>
                <Table.Td colSpan={9}>
                  <Text size="sm" c="dimmed" ta="center" py="lg">没有符合当前筛选条件的 Cutover 活动。</Text>
                </Table.Td>
              </Table.Tr>
            )}
            {filtered.map((activity) => {
              const planActivity = planById.get(activity.activity_id)
              const isExpanded = expanded === activity.activity_id
              return (
                <Fragment key={activity.activity_id}>
                  <Table.Tr data-testid="activity-row">
                    <Table.Td><Code>{activity.activity_id}</Code></Table.Td>
                    <Table.Td>{activity.title}</Table.Td>
                    <Table.Td>{activity.workstream}</Table.Td>
                    <Table.Td>{activity.owner_role}</Table.Td>
                    <Table.Td><Code>{activity.start_offset} → {activity.end_offset}</Code></Table.Td>
                    <Table.Td>{statusBadge(activity.current_status)}</Table.Td>
                    <Table.Td miw={140}>
                      <Progress value={activity.progress_percent} color={statusColor(activity.current_status)} />
                      <Text size="xs" ff="monospace" mt={2}>{activity.progress_percent}%</Text>
                    </Table.Td>
                    <Table.Td>{activity.is_critical_to_day1 ? 'Yes' : 'No'}</Table.Td>
                    <Table.Td>
                      <Button size="xs" variant="subtle" onClick={() => setExpanded(isExpanded ? null : activity.activity_id)}>
                        {isExpanded ? '收起' : '展开'}
                      </Button>
                    </Table.Td>
                  </Table.Tr>
                  {isExpanded && (
                    <Table.Tr>
                      <Table.Td colSpan={9}>
                        <Stack gap={4} py="sm">
                          <Text size="sm">depends_on: {(activity.depends_on.length ? activity.depends_on : [EMPTY]).join(', ')}</Text>
                          <Text size="sm">blocker: {activity.blocker || EMPTY}</Text>
                          <Text size="sm">status_note: {activity.last_note || EMPTY}</Text>
                          <Text size="sm">last_event_id: {activity.last_event_id || EMPTY}</Text>
                          <Text size="sm">last_update_offset: {activity.last_update_offset || EMPTY}</Text>
                          <Text size="sm">source_requirement_id: {planActivity?.source_requirement_id ?? EMPTY}</Text>
                          <Text size="sm">rollback_required: {planActivity?.rollback_required ? 'Yes' : 'No'}</Text>
                          <Text size="sm">rollback_action: {planActivity?.rollback_action || EMPTY}</Text>
                          <Text size="sm">approval_gate: {activity.approval_gate ?? EMPTY}</Text>
                        </Stack>
                      </Table.Td>
                    </Table.Tr>
                  )}
                </Fragment>
              )
            })}
          </Table.Tbody>
        </Table>
      </Box>
    </Paper>
  )
}

function WorkPackages({ status }: { status: CutoverStatusReport }) {
  return (
    <Paper p="lg" radius="md" withBorder>
      <Group justify="space-between" mb="md">
        <Title order={3} size="h5">工作包 · {status.work_packages.length}</Title>
        <Text size="xs" c="dimmed">
          Blocked = {status.work_package_status_counts.Blocked ?? 0} · In Progress = {status.work_package_status_counts['In Progress'] ?? 0}
        </Text>
      </Group>
      <Box style={{ overflowX: 'auto' }}>
        <Table striped highlightOnHover verticalSpacing="sm" miw={980}>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>work_package_id</Table.Th>
              <Table.Th>source_requirement_id</Table.Th>
              <Table.Th>owner_role</Table.Th>
              <Table.Th>business_owner_role</Table.Th>
              <Table.Th>current_status</Table.Th>
              <Table.Th>progress_percent</Table.Th>
              <Table.Th>completed_activity_count</Table.Th>
              <Table.Th>blocked_activity_ids</Table.Th>
              <Table.Th>next_activity_id</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {status.work_packages.map((item) => {
              const blockedIds = status.activities
                .filter((activity) => activity.work_package_id === item.work_package_id && activity.current_status === 'Blocked')
                .map((activity) => activity.activity_id)
              return (
                <Table.Tr key={item.work_package_id}>
                  <Table.Td><Code>{item.work_package_id}</Code></Table.Td>
                  <Table.Td><Code>{item.source_requirement_id}</Code></Table.Td>
                  <Table.Td>{item.owner_role}</Table.Td>
                  <Table.Td>{item.business_owner_role}</Table.Td>
                  <Table.Td>{statusBadge(item.current_status)}</Table.Td>
                  <Table.Td ff="monospace">{item.progress_percent.toFixed(2)}%</Table.Td>
                  <Table.Td>{item.activity_status_counts.Completed}</Table.Td>
                  <Table.Td>{blockedIds.join(', ') || EMPTY}</Table.Td>
                  <Table.Td>{item.next_activity_id ?? EMPTY}</Table.Td>
                </Table.Tr>
              )
            })}
          </Table.Tbody>
        </Table>
      </Box>
    </Paper>
  )
}

function RaidTable({ plan, status }: { plan: CutoverPlanReport | null; status: CutoverStatusReport }) {
  const [type, setType] = useState('all')
  const [raidStatus, setRaidStatus] = useState('all')
  const [severity, setSeverity] = useState('all')
  const [owner, setOwner] = useState('all')
  const planById = useMemo(() => new Map(plan?.raid_register.map((item) => [item.raid_id, item]) ?? []), [plan])
  const owners = Array.from(new Set(status.raid_register.map((item) => item.owner_role))).sort()
  const filtered = status.raid_register.filter((item) => (
    (type === 'all' || item.type === type) &&
    (raidStatus === 'all' || item.current_status === raidStatus) &&
    (severity === 'all' || item.severity === severity) &&
    (owner === 'all' || item.owner_role === owner)
  ))
  return (
    <Paper p="lg" radius="md" withBorder>
      <Group justify="space-between" mb="md">
        <Title order={3} size="h5">RAID · {filtered.length}</Title>
        <Text size="xs" c="dimmed">
          Dependency {status.raid_status_counts.by_type.Dependency ? Object.values(status.raid_status_counts.by_type.Dependency).reduce((a, b) => a + b, 0) : 0} · Risk {status.raid_status_counts.by_type.Risk ? Object.values(status.raid_status_counts.by_type.Risk).reduce((a, b) => a + b, 0) : 0} · Mitigating = {status.raid_status_counts.by_status.Mitigating ?? 0} · Resolved = {status.raid_status_counts.by_status.Resolved ?? 0} · Open = {status.raid_status_counts.by_status.Open ?? 0}
        </Text>
      </Group>
      <SimpleGrid cols={{ base: 1, sm: 2, md: 4 }} spacing="sm" mb="md">
        <NativeSelect label="Type" value={type} onChange={(event) => setType(event.currentTarget.value)} data={['all', 'Dependency', 'Risk']} />
        <NativeSelect label="Status" value={raidStatus} onChange={(event) => setRaidStatus(event.currentTarget.value)} data={['all', 'Open', 'Mitigating', 'Resolved', 'Accepted', 'Closed']} />
        <NativeSelect label="Severity" value={severity} onChange={(event) => setSeverity(event.currentTarget.value)} data={['all', 'High', 'Medium', 'Low']} />
        <NativeSelect label="Owner Role" value={owner} onChange={(event) => setOwner(event.currentTarget.value)} data={['all', ...owners]} />
      </SimpleGrid>
      <Box style={{ overflowX: 'auto' }}>
        <Table striped highlightOnHover verticalSpacing="sm" miw={1120}>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>raid_id</Table.Th>
              <Table.Th>type</Table.Th>
              <Table.Th>title</Table.Th>
              <Table.Th>owner_role</Table.Th>
              <Table.Th>severity</Table.Th>
              <Table.Th>current_status</Table.Th>
              <Table.Th>mitigation</Table.Th>
              <Table.Th>trigger</Table.Th>
              <Table.Th>linked_requirement_ids</Table.Th>
              <Table.Th>linked_activity_ids</Table.Th>
              <Table.Th>last_event_id</Table.Th>
              <Table.Th>last_update_offset</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {filtered.map((item) => {
              const base = planById.get(item.raid_id)
              return (
                <Table.Tr key={item.raid_id} data-testid="raid-row">
                  <Table.Td><Code>{item.raid_id}</Code></Table.Td>
                  <Table.Td>{item.type}</Table.Td>
                  <Table.Td>{base?.title ?? item.description}</Table.Td>
                  <Table.Td>{item.owner_role}</Table.Td>
                  <Table.Td>{item.severity}</Table.Td>
                  <Table.Td>{statusBadge(item.current_status)}</Table.Td>
                  <Table.Td>{base?.mitigation ?? EMPTY}</Table.Td>
                  <Table.Td>{base?.trigger ?? EMPTY}</Table.Td>
                  <Table.Td>{base?.linked_requirement_ids.join(', ') ?? item.source_requirement_id ?? EMPTY}</Table.Td>
                  <Table.Td>{base?.linked_activity_ids.join(', ') ?? EMPTY}</Table.Td>
                  <Table.Td>{item.last_event_id ?? EMPTY}</Table.Td>
                  <Table.Td>{item.last_update_offset ?? EMPTY}</Table.Td>
                </Table.Tr>
              )
            })}
          </Table.Tbody>
        </Table>
      </Box>
    </Paper>
  )
}

function GatesAndFreeze({ plan, status }: { plan: CutoverPlanReport | null; status: CutoverStatusReport | null }) {
  const gates = status?.approval_gates ?? plan?.approval_gates ?? []
  return (
    <SimpleGrid cols={{ base: 1, lg: 2 }} spacing="md">
      <Paper p="lg" radius="md" withBorder>
        <Group justify="space-between" mb="md">
          <Title order={3} size="h5">审批门 · {gates.length}</Title>
          {status && (
            <Text size="xs" c="dimmed">
              Approved = {status.approval_gate_status_counts.Approved ?? 0} · Blocked = {status.approval_gate_status_counts.Blocked ?? 0} · Pending = {status.approval_gate_status_counts.Pending ?? 0}
            </Text>
          )}
        </Group>
        <Stack gap="sm">
          {gates.map((gate) => {
            const currentStatus = 'current_status' in gate ? String(gate.current_status) : null
            const readiness = 'readiness' in gate ? Boolean(gate.readiness) : null
            const missing =
              'missing_readiness_criteria' in gate && Array.isArray(gate.missing_readiness_criteria)
                ? gate.missing_readiness_criteria
                : []
            return (
              <Paper key={gate.gate_id} p="sm" radius="sm" withBorder>
                <Group justify="space-between">
                  <Text fw={600}>{gate.gate_id}</Text>
                  {currentStatus ? statusBadge(currentStatus) : <Badge variant="outline">Baseline</Badge>}
                </Group>
                <Text size="sm">{gate.name} · {gate.due_offset}</Text>
                {readiness !== null && <Text size="sm">readiness: {readiness ? 'Ready' : 'Not ready'}</Text>}
                {missing.map((item) => (
                  <Text key={item} size="xs" c="dimmed">- {item}</Text>
                ))}
                <Text size="xs" c="dimmed">approver_roles: {gate.approver_roles.join(', ')}</Text>
                <Text size="xs" c="dimmed">entry_criteria: {gate.entry_criteria.join('; ')}</Text>
              </Paper>
            )
          })}
        </Stack>
      </Paper>
      <Paper p="lg" radius="md" withBorder>
        <Title order={3} size="h5" mb="md">冻结窗口 · {plan?.freeze_windows.length ?? 0}</Title>
        <Stack gap="sm">
          {plan?.freeze_windows.map((item) => (
            <Paper key={item.freeze_id} p="sm" radius="sm" withBorder>
              <Group justify="space-between">
                <Text fw={600}>{item.freeze_id}</Text>
                <Code>{item.start_offset} → {item.end_offset}</Code>
              </Group>
              <Text size="sm">{item.name}</Text>
              <Text size="xs" c="dimmed">{item.owner_role} · exception: {item.exception_approval_role}</Text>
              <Text size="xs" c="dimmed">{item.description}</Text>
            </Paper>
          )) ?? <Text size="sm" c="dimmed">计划基线尚未生成。</Text>}
        </Stack>
      </Paper>
    </SimpleGrid>
  )
}

function DueItems({ daily }: { daily: CutoverDailyReport }) {
  const renderList = (items: typeof daily.due_now, empty: string) => (
    items.length === 0 ? <Text size="sm" c="dimmed">{empty}</Text> : (
      <Stack gap="xs">
        {items.map((item) => (
          <Group key={item.activity_id} gap="xs" align="center">
            <Code>{item.activity_id}</Code>
            <Text size="sm">{item.title} · {item.end_offset}</Text>
            {statusBadge(item.current_status)}
          </Group>
        ))}
      </Stack>
    )
  )
  return (
    <SimpleGrid cols={{ base: 1, md: 3 }} spacing="md">
      <Paper p="lg" radius="md" withBorder>
        <Title order={3} size="h5" mb="md">Due now · {daily.due_now.length}</Title>
        {renderList(daily.due_now, '当前没有今日到期活动。')}
      </Paper>
      <Paper p="lg" radius="md" withBorder>
        <Title order={3} size="h5" mb="md">Overdue · {daily.overdue.length}</Title>
        {renderList(daily.overdue, '当前没有逾期活动。')}
      </Paper>
      <Paper p="lg" radius="md" withBorder>
        <Title order={3} size="h5" mb="md">Due next · {daily.due_next.length}</Title>
        {renderList(daily.due_next, '当前没有后续到期活动。')}
      </Paper>
    </SimpleGrid>
  )
}

function AgentTrace({ trace }: { trace: CutoverAgentTrace | null }) {
  if (!trace) return missingSection('Agent 示例 trace 尚未生成。', 'Plan、Status 和 Daily 仍可正常展示。')
  return (
    <Paper p="lg" radius="md" withBorder>
      <Title order={3} size="h5" mb={4}>Cutover Copilot · 示例审计轨迹</Title>
      <Text size="sm" c="dimmed" mb="md">
        这是已生成的只读示例 trace，不是网页内的实时聊天或 Agent 执行。Agent 通过 stdio MCP 调用确定性工具。
      </Text>
      <SimpleGrid cols={{ base: 1, md: 3 }} spacing="sm" mb="md">
        <Text size="sm"><Text span c="dimmed">用户问题: </Text>{trace.request.user_query}</Text>
        <Text size="sm"><Text span c="dimmed">Planner intent: </Text><Code>{trace.plan.intent}</Code></Text>
        <Text size="sm"><Text span c="dimmed">Planner confidence: </Text>{trace.plan.confidence.toFixed(2)}</Text>
        <Text size="sm"><Text span c="dimmed">Policy: </Text>{trace.policy.allowed ? 'allowed' : `denied · ${trace.policy.denied_reason}`}</Text>
        <Text size="sm"><Text span c="dimmed">Output validation: </Text>{trace.validation.valid ? 'valid' : trace.validation.reasons.join('; ')}</Text>
        <Text size="sm"><Text span c="dimmed">Offline: </Text>{String(trace._run_info.offline ?? EMPTY)}</Text>
        <Text size="sm"><Text span c="dimmed">Planner cache: </Text>{trace._run_info.planner_cache ? `${trace._run_info.planner_cache.hit} hit / ${trace._run_info.planner_cache.miss} miss` : EMPTY}</Text>
        <Text size="sm"><Text span c="dimmed">Trace SHA: </Text>{shaLabel(trace._run_info.content_sha256)}</Text>
      </SimpleGrid>
      <Box style={{ overflowX: 'auto' }}>
        <Table striped verticalSpacing="sm" miw={760}>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Tool</Table.Th>
              <Table.Th>Arguments</Table.Th>
              <Table.Th>OK</Table.Th>
              <Table.Th>Report SHA</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {trace.tool_calls.map((call) => (
              <Table.Tr key={call.tool_name}>
                <Table.Td><Code>{call.tool_name}</Code></Table.Td>
                <Table.Td><Code>{JSON.stringify(call.arguments)}</Code></Table.Td>
                <Table.Td>{call.ok ? 'yes' : 'no'}</Table.Td>
                <Table.Td>{shaLabel(call.source_content_sha256)}</Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </Box>
      <Text size="sm" fw={600} mt="md">最终确定性回答</Text>
      <Text size="sm" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{trace.final_answer}</Text>
    </Paper>
  )
}

export function CutoverView() {
  const plan = useReport<CutoverPlanReport>('cutover_plan_report')
  const status = useReport<CutoverStatusReport>('cutover_status_report')
  const daily = useReport<CutoverDailyReport>('cutover_daily_report')
  const trace = useReport<CutoverAgentTrace>('cutover_agent_trace')

  const stillLoading = plan.loading || status.loading || daily.loading || trace.loading
  const planData = plan.data
  const statusData = status.data
  const dailyData = daily.data
  const traceData = trace.data

  return (
    <Stack gap="xl">
      <div>
        <Title order={2} size="h3">Cutover / RAID 治理</Title>
        <Text size="sm" c="dimmed" mt={4}>
          从模块二 Development Backlog 生成计划基线，通过追加式状态事件形成执行快照、管理日报和可审计 Agent 查询。
        </Text>
      </div>

      {stillLoading && <LoadingBlock />}
      {plan.error && <MissingReport title="Cutover 计划基线" error={plan.error} />}
      {status.error && <MissingReport title="Cutover 执行状态" error={status.error} />}
      {daily.error && <MissingReport title="Cutover 管理日报" error={daily.error} />}

      <RuntimeInfo plan={planData} status={statusData} daily={dailyData} trace={traceData} />

      {dailyData && <ManagementOverview daily={dailyData} status={statusData} />}
      {!dailyData && missingSection('管理总览尚不可用', 'Cutover 管理日报缺失，因此隐藏 RAG、关键阻塞、管理行动和到期事项。')}
      {dailyData && <NextGate daily={dailyData} />}
      {dailyData && <CriticalBlockers daily={dailyData} />}
      {dailyData && <ManagementActions daily={dailyData} />}

      {statusData ? (
        <>
          <ActivitiesTable plan={planData} status={statusData} />
          <WorkPackages status={statusData} />
          <RaidTable plan={planData} status={statusData} />
        </>
      ) : (
        missingSection('执行状态尚不可用', '活动、工作包和 RAID 当前状态区块已隐藏，计划基线或日报仍可展示。')
      )}

      <GatesAndFreeze plan={planData} status={statusData} />
      {dailyData && <DueItems daily={dailyData} />}
      <AgentTrace trace={traceData} />
    </Stack>
  )
}
