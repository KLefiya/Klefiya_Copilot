import { Fragment, useMemo, useState } from 'react'
import {
  Alert,
  Badge,
  Box,
  Code,
  Group,
  Loader,
  Paper,
  Progress,
  Select,
  SimpleGrid,
  Stack,
  Table,
  Text,
  TextInput,
  Title,
  Tooltip,
} from '@mantine/core'
import type { ApiError } from '../api'
import { StatCard } from '../components/StatCard'
import type {
  DevelopmentBacklogItem,
  EvaluationModelMetrics,
  GapAnalysisEvaluation,
  GapAnalysisReport,
  GapCategory,
  GapDomain,
  GapRequirement,
} from '../lib/reports'
import { STATUS } from '../lib/theme'
import { notGeneratedInfo, useReport } from '../lib/useReport'

const CATEGORY_ORDER: GapCategory[] = ['Fit', 'Configuration', 'Enhancement', 'Development']
const DOMAIN_ORDER: GapDomain[] = ['P2P', 'O2C', 'R2R', 'master_data']

const CATEGORY_COLOR: Record<GapCategory, string> = {
  Fit: 'green',
  Configuration: 'blue',
  Enhancement: 'yellow',
  Development: 'red',
}

const pct = (value: number | null | undefined) => (value == null ? '-' : `${(value * 100).toFixed(2)}%`)

function categoryBadge(category: GapCategory) {
  return (
    <Badge size="sm" variant="light" color={CATEGORY_COLOR[category]}>
      {category}
    </Badge>
  )
}

function MissingReport({ title, error }: { title: string; error: ApiError }) {
  const pending = notGeneratedInfo(error)

  if (pending) {
    return (
      <Alert color="gray" variant="light" title={`${title}未生成`}>
        <Stack gap="xs">
          <Text size="sm">后端白名单已定义该报告；生成后刷新页面即可读取。</Text>
          <Code block>{pending.generatedBy}</Code>
          <Text size="xs" c="dimmed">
            预期位置 <Code>{pending.expectedPath}</Code>
          </Text>
        </Stack>
      </Alert>
    )
  }

  return (
    <Alert color="red" variant="light" title={`${title}读取失败`}>
      <Stack gap="xs">
        <Text size="sm">{error.message}</Text>
        <Code block>{JSON.stringify(error.detail, null, 2)}</Code>
      </Stack>
    </Alert>
  )
}

function LoadingBlock({ label }: { label: string }) {
  return (
    <Paper p="lg" radius="md" withBorder>
      <Group gap="sm">
        <Loader size="sm" />
        <Text size="sm" c="dimmed">
          正在读取{label}
        </Text>
      </Group>
    </Paper>
  )
}

function RunInfo({ report }: { report: GapAnalysisReport }) {
  const sha = report._run_info.content_sha256
  const shortSha = `${sha.slice(0, 12)}...${sha.slice(-6)}`
  const provider = report._meta.provider === 'deepseek' ? 'DeepSeek' : report._meta.provider

  return (
    <Paper p="lg" radius="md" withBorder>
      <Group justify="space-between" align="flex-start" gap="lg">
        <SimpleGrid cols={{ base: 1, sm: 2, md: 5 }} spacing="sm" style={{ flex: 1 }}>
          <Text size="sm">
            <Text span c="dimmed">Provider: </Text>
            {provider}
          </Text>
          <Text size="sm">
            <Text span c="dimmed">Model: </Text>
            <Code>{report._meta.model}</Code>
          </Text>
          <Text size="sm">
            <Text span c="dimmed">Thinking: </Text>
            <Code>{report._meta.thinking ?? '-'}</Code>
          </Text>
          <Text size="sm">
            <Text span c="dimmed">Reasoning effort: </Text>
            <Code>{report._meta.reasoning_effort ?? '-'}</Code>
          </Text>
          <Tooltip label={sha} multiline w={360}>
            <Text size="sm">
              <Text span c="dimmed">Report SHA: </Text>
              <Code>{shortSha}</Code>
            </Text>
          </Tooltip>
        </SimpleGrid>
      </Group>
      <Group gap="sm" mt="md">
        <Badge variant="light" color="gray">全部为合成访谈和自撰知识库</Badge>
        <Badge variant="light" color="gray">报告由离线缓存支持复现</Badge>
      </Group>
    </Paper>
  )
}

function EvaluationMetrics({ evaluation }: { evaluation: GapAnalysisEvaluation }) {
  const precision = evaluation._meta.matched / evaluation._meta.extracted
  const recall = evaluation._meta.matched / evaluation._meta.ground_truth
  const f1 = (2 * precision * recall) / (precision + recall)
  const baseline = evaluation.baseline_no_llm.accuracy
  const llm = evaluation.llm.accuracy
  const delta = evaluation.llm_vs_baseline.accuracy_delta

  return (
    <Stack gap="lg">
      <SimpleGrid cols={{ base: 1, sm: 2, md: 4 }} spacing="md">
        <StatCard label="Ground truth" value={evaluation._meta.ground_truth} />
        <StatCard label="Matched" value={evaluation._meta.matched} accent={STATUS.good} />
        <StatCard label="Spurious" value={evaluation._meta.spurious} accent={STATUS.warning} />
        <StatCard label="Missed" value={evaluation._meta.missed} accent={STATUS.critical} />
        <StatCard label="Strict Precision" value={pct(precision)} hint="严格一对一抽取对齐" />
        <StatCard label="Strict Recall" value={pct(recall)} hint="严格一对一抽取对齐" />
        <StatCard label="Strict F1" value={pct(f1)} hint="严格一对一抽取对齐" />
      </SimpleGrid>

      <Paper p="lg" radius="md" withBorder>
        <Title order={3} size="h5" mb="md">
          模型与基线对比
        </Title>
        <SimpleGrid cols={{ base: 1, md: 3 }} spacing="md">
          <Stack gap={6}>
            <Group justify="space-between">
              <Text size="sm">无 LLM 检索基线</Text>
              <Text size="sm" ff="monospace">{pct(baseline)}</Text>
            </Group>
            <Progress value={baseline * 100} color="gray" />
          </Stack>
          <Stack gap={6}>
            <Group justify="space-between">
              <Text size="sm">DeepSeek matched accuracy</Text>
              <Text size="sm" ff="monospace">{pct(llm)}</Text>
            </Group>
            <Progress value={llm * 100} color="green" />
          </Stack>
          <Stack gap={6}>
            <Group justify="space-between">
              <Text size="sm">提升</Text>
              <Text size="sm" ff="monospace">{(delta * 100).toFixed(2)} 个百分点</Text>
            </Group>
            <Progress value={Math.max(0, delta) * 100} color="blue" />
          </Stack>
        </SimpleGrid>
        <Alert color="blue" variant="light" mt="md">
          <Text size="sm">
            分类准确率仅在成功对齐的 {evaluation.llm.n} 条需求上计算，不等同于端到端抽取准确率。
          </Text>
        </Alert>
      </Paper>

      <PerClassTable title="DeepSeek 分类别表现" metrics={evaluation.llm} />
      <PerClassTable title="无 LLM 基线分类表现" metrics={evaluation.baseline_no_llm} />
    </Stack>
  )
}

function PerClassTable({ title, metrics }: { title: string; metrics: EvaluationModelMetrics }) {
  return (
    <Paper p="lg" radius="md" withBorder>
      <Title order={3} size="h5" mb="md">
        {title}
      </Title>
      <Table striped highlightOnHover verticalSpacing="sm">
        <Table.Thead>
          <Table.Tr>
            <Table.Th>类别</Table.Th>
            <Table.Th>Precision</Table.Th>
            <Table.Th>Recall</Table.Th>
            <Table.Th>Support</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {CATEGORY_ORDER.map((category) => {
            const item = metrics.per_class[category]
            return (
              <Table.Tr key={category}>
                <Table.Td>{categoryBadge(category)}</Table.Td>
                <Table.Td ff="monospace">{pct(item.precision)}</Table.Td>
                <Table.Td ff="monospace">{pct(item.recall)}</Table.Td>
                <Table.Td ff="monospace">{item.support}</Table.Td>
              </Table.Tr>
            )
          })}
        </Table.Tbody>
      </Table>
    </Paper>
  )
}

function RequirementsTable({ requirements }: { requirements: GapRequirement[] }) {
  const [domain, setDomain] = useState<string>('all')
  const [category, setCategory] = useState<string>('all')
  const [review, setReview] = useState<string>('all')
  const [query, setQuery] = useState('')
  const [expanded, setExpanded] = useState<string | null>(null)

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return requirements.filter((requirement) => {
      const domainOk = domain === 'all' || requirement.domain === domain
      const categoryOk = category === 'all' || requirement.llm.category === category
      const reviewOk =
        review === 'all' ||
        (review === 'needs_review' ? requirement.llm.needs_review : !requirement.llm.needs_review)
      const textOk =
        !needle ||
        requirement.extracted_id.toLowerCase().includes(needle) ||
        requirement.source_note_id.toLowerCase().includes(needle) ||
        requirement.requirement_description.toLowerCase().includes(needle)
      return domainOk && categoryOk && reviewOk && textOk
    })
  }, [category, domain, query, requirements, review])

  return (
    <Paper p="lg" radius="md" withBorder>
      <Group justify="space-between" mb="md">
        <Title order={3} size="h5">
          需求结果明细
        </Title>
        <Text size="xs" c="dimmed">
          {filtered.length} / {requirements.length}
        </Text>
      </Group>

      <SimpleGrid cols={{ base: 1, sm: 2, md: 4 }} spacing="sm" mb="md">
        <Select
          label="业务域"
          value={domain}
          onChange={(value) => setDomain(value ?? 'all')}
          data={[{ value: 'all', label: '全部' }, ...DOMAIN_ORDER.map((item) => ({ value: item, label: item }))]}
        />
        <Select
          label="分类"
          value={category}
          onChange={(value) => setCategory(value ?? 'all')}
          data={[{ value: 'all', label: '全部' }, ...CATEGORY_ORDER.map((item) => ({ value: item, label: item }))]}
        />
        <Select
          label="状态"
          value={review}
          onChange={(value) => setReview(value ?? 'all')}
          data={[
            { value: 'all', label: '全部' },
            { value: 'normal', label: '正常' },
            { value: 'needs_review', label: 'needs_review' },
          ]}
        />
        <TextInput
          label="搜索"
          value={query}
          onChange={(event) => setQuery(event.currentTarget.value)}
          placeholder="ID / 描述 / 来源"
        />
      </SimpleGrid>

      <Box style={{ overflowX: 'auto' }}>
        <Table striped highlightOnHover verticalSpacing="sm" miw={1120}>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>ID</Table.Th>
              <Table.Th>来源</Table.Th>
              <Table.Th>业务域</Table.Th>
              <Table.Th>需求</Table.Th>
              <Table.Th>Baseline</Table.Th>
              <Table.Th>DeepSeek</Table.Th>
              <Table.Th>Confidence</Table.Th>
              <Table.Th>Review</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {filtered.length === 0 && (
              <Table.Tr>
                <Table.Td colSpan={8}>
                  <Text size="sm" c="dimmed" ta="center" py="lg">
                    没有符合当前筛选条件的需求。
                  </Text>
                </Table.Td>
              </Table.Tr>
            )}
            {filtered.map((requirement) => {
              const isExpanded = expanded === requirement.extracted_id
              return (
                <Fragment key={requirement.extracted_id}>
                  <Table.Tr
                    onClick={() => setExpanded(isExpanded ? null : requirement.extracted_id)}
                    aria-expanded={isExpanded}
                    style={{ cursor: 'pointer' }}
                  >
                    <Table.Td>
                      <Code>{requirement.extracted_id}</Code>
                    </Table.Td>
                    <Table.Td>
                      <Code>{requirement.source_note_id}</Code>
                    </Table.Td>
                    <Table.Td>{requirement.domain}</Table.Td>
                    <Table.Td maw={420}>
                      <Text size="sm" lineClamp={2}>
                        {requirement.requirement_description}
                      </Text>
                    </Table.Td>
                    <Table.Td>{categoryBadge(requirement.baseline.category)}</Table.Td>
                    <Table.Td>{categoryBadge(requirement.llm.category)}</Table.Td>
                    <Table.Td ff="monospace">{requirement.llm.confidence.toFixed(2)}</Table.Td>
                    <Table.Td>
                      {requirement.llm.needs_review ? (
                        <Badge size="sm" variant="light" color="yellow">
                          需要人工确认
                        </Badge>
                      ) : (
                        <Badge size="sm" variant="outline" color="gray">
                          正常
                        </Badge>
                      )}
                    </Table.Td>
                  </Table.Tr>
                  {isExpanded && (
                    <Table.Tr>
                      <Table.Td colSpan={8}>
                        <RequirementDetail requirement={requirement} />
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

function RequirementDetail({ requirement }: { requirement: GapRequirement }) {
  return (
    <Stack gap="sm" py="sm">
      <Text size="sm">
        <Text span fw={600}>原始引用：</Text>
        {requirement.source_quote}
      </Text>
      <Group gap="xs">
        <Text size="sm" fw={600}>检索到的知识条目</Text>
        {requirement.llm.retrieved_entry_ids.map((entry) => (
          <Code key={entry}>{entry}</Code>
        ))}
      </Group>
      <Group gap="xs">
        <Text size="sm" fw={600}>模型证据</Text>
        {requirement.llm.evidence.map((entry) => (
          <Code key={entry}>{entry}</Code>
        ))}
      </Group>
      <Text size="sm">
        <Text span fw={600}>判定理由：</Text>
        {requirement.llm.rationale}
      </Text>
      {requirement.llm.needs_review && (
        <Alert color="yellow" variant="light" title="需要人工确认">
          <Stack gap={4}>
            {requirement.llm.needs_review_reasons.map((reason) => (
              <Text key={reason} size="sm">
                {reason}
              </Text>
            ))}
          </Stack>
        </Alert>
      )}
    </Stack>
  )
}

function DevelopmentBacklog({ items }: { items: DevelopmentBacklogItem[] }) {
  return (
    <Paper p="lg" radius="md" withBorder>
      <Title order={3} size="h5" mb={4}>
        Development Backlog · {items.length}
      </Title>
      <Text size="sm" c="dimmed" mb="md">
        这些条目可以作为模块三 Cutover / RAID 治理的输入。
      </Text>
      <Box style={{ overflowX: 'auto' }}>
        <Table striped highlightOnHover verticalSpacing="sm" miw={980}>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Backlog</Table.Th>
            <Table.Th>需求</Table.Th>
            <Table.Th>来源</Table.Th>
            <Table.Th>业务域</Table.Th>
            <Table.Th>描述</Table.Th>
            <Table.Th>Confidence</Table.Th>
            <Table.Th>Review</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {items.map((item) => (
            <Table.Tr key={item.backlog_id}>
              <Table.Td><Code>{item.backlog_id}</Code></Table.Td>
              <Table.Td><Code>{item.requirement_id}</Code></Table.Td>
              <Table.Td><Code>{item.source_note_id}</Code></Table.Td>
              <Table.Td>{item.domain}</Table.Td>
              <Table.Td>
                <Text size="sm">{item.description}</Text>
                <Text size="xs" c="dimmed" mt={4}>
                  证据 {item.evidence.join(', ')} · {item.rationale}
                </Text>
              </Table.Td>
              <Table.Td ff="monospace">{item.confidence.toFixed(2)}</Table.Td>
              <Table.Td>{item.needs_review ? '需要人工确认' : '正常'}</Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
        </Table>
      </Box>
    </Paper>
  )
}

function NeedsReview({ requirements }: { requirements: GapRequirement[] }) {
  const reviewItems = requirements.filter((requirement) => requirement.llm.needs_review)

  return (
    <Paper p="lg" radius="md" withBorder>
      <Title order={3} size="h5" mb={4}>
        需要人工确认 · {reviewItems.length}
      </Title>
      <Text size="sm" c="dimmed" mb="md">
        needs_review 表示需要人工确认，不等同于判定错误。
      </Text>
      <Stack gap="sm">
        {reviewItems.map((requirement) => (
          <Alert
            key={requirement.extracted_id}
            color="yellow"
            variant="light"
            title={
              <Group gap="sm">
                <Code>{requirement.extracted_id}</Code>
                {categoryBadge(requirement.llm.category)}
                <Text size="sm" ff="monospace">
                  {requirement.llm.confidence.toFixed(2)}
                </Text>
              </Group>
            }
          >
            <Text size="sm" mb={6}>{requirement.requirement_description}</Text>
            <Text size="xs" c="dimmed">
              复核原因：{requirement.llm.needs_review_reasons.join('; ') || '未提供'}
            </Text>
            <Text size="xs" c="dimmed">
              证据条目：{requirement.llm.evidence.join(', ')}
            </Text>
          </Alert>
        ))}
      </Stack>
    </Paper>
  )
}

export function FitGapView() {
  const report = useReport<GapAnalysisReport>('gap_analysis_report')
  const evaluation = useReport<GapAnalysisEvaluation>('gap_analysis_evaluation')

  if (report.loading) return <LoadingBlock label="Fit/Gap 判定报告" />
  if (report.error) return <MissingReport title="Fit/Gap 判定报告" error={report.error} />
  if (!report.data) return null

  const categoryCounts = report.data.requirements.reduce<Record<GapCategory, number>>(
    (acc, requirement) => {
      acc[requirement.llm.category] += 1
      return acc
    },
    { Fit: 0, Configuration: 0, Enhancement: 0, Development: 0 },
  )
  const needsReviewCount = report.data.requirements.filter((requirement) => requirement.llm.needs_review).length

  return (
    <Stack gap="xl">
      <div>
        <Title order={2} size="h3">
          Fit-to-Standard 差异分析
        </Title>
        <Text size="sm" c="dimmed" mt={4}>
          从合成访谈中抽取业务需求，基于标准流程知识库判定 Fit、Configuration、Enhancement 或 Development
        </Text>
      </div>

      <RunInfo report={report.data} />

      <SimpleGrid cols={{ base: 1, sm: 2, md: 6 }} spacing="md">
        <StatCard label="抽取需求" value={report.data._meta.extracted_requirement_count} />
        {CATEGORY_ORDER.map((category) => (
          <StatCard
            key={category}
            label={category}
            value={categoryCounts[category]}
            accent={category === 'Fit' ? STATUS.good : category === 'Development' ? STATUS.critical : undefined}
          />
        ))}
        <StatCard label="需要复核" value={needsReviewCount} accent={STATUS.warning} />
      </SimpleGrid>

      {evaluation.loading && <LoadingBlock label="独立评估报告" />}
      {evaluation.error && <MissingReport title="独立评估报告" error={evaluation.error} />}
      {evaluation.data && <EvaluationMetrics evaluation={evaluation.data} />}

      <RequirementsTable requirements={report.data.requirements} />
      <DevelopmentBacklog items={report.data.dev_backlog} />
      <NeedsReview requirements={report.data.requirements} />

      <Alert color="gray" variant="light" title="已知局限">
        <Stack gap={6}>
          <Text size="sm">
            当前严格评估使用同一访谈内的词元 Jaccard 贪心一对一匹配。
          </Text>
          <Text size="sm">
            一条审批矩阵需求因词法重叠略低于阈值被计为 spurious/missed，但人工诊断确认该需求已被语义正确抽取。
          </Text>
          <Text size="sm">
            另有一条标准 R2R 能力清单未被正式抽取。这些结果保留为项目的公开局限，不针对 ground truth 继续调参。
          </Text>
        </Stack>
      </Alert>
    </Stack>
  )
}
