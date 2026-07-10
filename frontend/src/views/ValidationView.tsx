/** 视图三：迁移前校验（vendor_validation_report）。 */

import {
  Accordion,
  Alert,
  Badge,
  Box,
  Code,
  Group,
  List,
  Paper,
  Stack,
  Table,
  Text,
  Title,
  Tooltip,
} from '@mantine/core'
import { ReportGate } from '../components/ReportGate'
import { StatCard } from '../components/StatCard'
import type { FieldIssue, RecordIssue, ValidationReport } from '../lib/reports'
import { ISSUE_TYPE_LABEL, SEVERITY_LABEL } from '../lib/reports'
import { STATUS, VERDICT, severityColor } from '../lib/theme'
import { useReport } from '../lib/useReport'

/** 三态：true / false / null（不确定）。null 不是 false —— needs_review 的语义是「说不准」。 */
function TriState({ value }: { value: boolean | null }) {
  if (value === true) return <Badge size="xs" variant="light" color="green">是</Badge>
  if (value === false) return <Badge size="xs" variant="light" color="red">否</Badge>
  return (
    <Tooltip label="不确定 —— 映射置信度未达 high，压成「否」会冤枉它">
      <Badge size="xs" variant="outline" color="gray">?</Badge>
    </Tooltip>
  )
}

function IssueTypeCard({
  issueType,
  count,
  fieldIssues,
  recordIssues,
}: {
  issueType: string
  count: number
  fieldIssues: FieldIssue[]
  recordIssues: RecordIssue[]
}) {
  const sample = fieldIssues[0] ?? recordIssues[0]
  const severity = sample?.severity ?? 'low'
  const color = severityColor(severity)

  // 归一化需求带 non_conforming_values；长度溢出带具体越界值。两者呈现方式不同。
  const normalization = fieldIssues.find((i) => i.non_conforming_values)
  const offenders = [...new Set(recordIssues.map((i) => i.value).filter(Boolean))] as string[]
  const disclaimer = recordIssues.find((i) => i.unverified_disclaimer_zh)?.unverified_disclaimer_zh

  return (
    <Accordion.Item value={issueType}>
      <Accordion.Control>
        <Group justify="space-between" wrap="nowrap" pr="sm">
          <Group gap={10}>
            <Box w={8} h={8} bg={color} style={{ borderRadius: 2 }} aria-hidden />
            <Text size="sm" fw={500}>
              {ISSUE_TYPE_LABEL[issueType] ?? issueType}
            </Text>
            <Code>{issueType}</Code>
          </Group>
          <Group gap="sm" wrap="nowrap">
            <Badge size="xs" variant="light" style={{ backgroundColor: `${color}22`, color }}>
              {SEVERITY_LABEL[severity]}
            </Badge>
            <Text size="sm" ff="monospace" fw={600}>
              {count}
            </Text>
            <Text size="xs" c="dimmed">
              条
            </Text>
          </Group>
        </Group>
      </Accordion.Control>

      <Accordion.Panel>
        <Stack gap="sm">
          {sample && <Text size="sm">{sample.detail_zh}</Text>}
          {sample && (
            <Alert variant="light" color="gray" p="xs" title="建议处理">
              <Text size="sm">{sample.suggestion_zh}</Text>
            </Alert>
          )}

          {disclaimer && (
            <Alert variant="light" color="yellow" p="xs" title="基于未核实的字段标注">
              <Text size="sm">{disclaimer}</Text>
            </Alert>
          )}

          {normalization?.non_conforming_values && (
            <div>
              <Text size="xs" c="dimmed" mb={6}>
                不符合规范形态的取值（{normalization.non_conforming_values.length} 种）
              </Text>
              <Group gap={6} wrap="wrap">
                {normalization.non_conforming_values.map((item) => (
                  <Badge key={item.value} size="sm" variant="outline" color="yellow">
                    <Text span ff="monospace" size="xs">
                      {item.value}
                    </Text>
                    <Text span c="dimmed" size="xs">
                      {' '}
                      ×{item.count}
                    </Text>
                  </Badge>
                ))}
              </Group>
            </div>
          )}

          {offenders.length > 0 && (
            <div>
              <Text size="xs" c="dimmed" mb={6}>
                触发该问题的具体取值（{offenders.length} 种，共 {recordIssues.length} 条记录）
              </Text>
              <Group gap={6} wrap="wrap">
                {offenders.map((value) => {
                  const hits = recordIssues.filter((i) => i.value === value).length
                  return (
                    <Badge key={value} size="sm" variant="outline" color="red">
                      <Text span ff="monospace" size="xs">
                        {value}
                      </Text>
                      <Text span c="dimmed" size="xs">
                        {' '}
                        len {value.length} ×{hits}
                      </Text>
                    </Badge>
                  )
                })}
              </Group>
            </div>
          )}
        </Stack>
      </Accordion.Panel>
    </Accordion.Item>
  )
}

export function ValidationView() {
  const { loading, error, data } = useReport<ValidationReport>('vendor_validation_report')

  return (
    <ReportGate loading={loading} error={error} data={data}>
      {(report) => {
        // 把 field_issues 与 record_view 里的问题都按 issue_type 归拢。
        const fieldIssuesByType: Record<string, FieldIssue[]> = {}
        for (const entry of report.field_view) {
          for (const issue of entry.field_issues) {
            ;(fieldIssuesByType[issue.issue_type] ??= []).push(issue)
          }
        }
        const recordIssuesByType: Record<string, RecordIssue[]> = {}
        for (const record of report.record_view) {
          for (const issue of record.issues) {
            ;(recordIssuesByType[issue.issue_type] ??= []).push(issue)
          }
        }

        const types = Object.entries(report.summary.by_issue_type).sort((a, b) => b[1] - a[1])
        const notLoadable = report.field_view.filter(
          (f) => f.verdict === 'mapping_ok_but_not_loadable',
        )
        const unverifiedCount = report.field_view.filter((f) => f.based_on_unverified).length

        return (
          <Stack gap="xl">
            <div>
              <Title order={2} size="h3">
                迁移前校验
              </Title>
              <Text size="sm" c="dimmed" mt={4}>
                映射后的数据能否真正加载进目标 SAP 实体 · schema{' '}
                <Code>{report._meta.sources.schema_version}</Code>
              </Text>
            </div>

            <Alert color="gray" variant="light" title="两个正交维度，分开表达">
              <Text size="sm">{report._meta.orthogonality_note_zh}</Text>
            </Alert>

            <Group grow align="stretch" wrap="wrap">
              <StatCard label="记录总数" value={report._meta.record_count} />
              <StatCard
                label="有问题的记录"
                value={report._meta.records_with_issues}
                accent={STATUS.critical}
              />
              <StatCard label="干净记录" value={report._meta.records_clean} accent={STATUS.good} />
              <StatCard
                label="高severity 问题"
                value={report.summary.by_severity.high}
                accent={STATUS.critical}
              />
              {unverifiedCount > 0 && (
                <StatCard
                  label="基于未核实标注"
                  value={unverifiedCount}
                  accent={STATUS.warning}
                  hint="结论需以官方 metadata 复核"
                />
              )}
            </Group>

            {notLoadable.length > 0 && (
              <Paper p="lg" radius="md" withBorder>
                <Title order={3} size="h5" mb="xs">
                  映射正确，但不可作为加载目标
                </Title>
                <Text size="sm" c="dimmed" mb="md">
                  <Code>semantic_match=true</Code> 且 <Code>loadable=false</Code>
                  ——这类字段只能作 lineage / 参考，把它简单标成「通过」是错的。
                </Text>
                <Stack gap="sm">
                  {notLoadable.map((entry) => (
                    <Alert
                      key={entry.legacy_field}
                      variant="light"
                      color="yellow"
                      p="sm"
                      title={
                        <Group gap={8}>
                          <Code>{entry.legacy_field}</Code>
                          <Text size="xs" c="dimmed">
                            →
                          </Text>
                          <Code>{entry.target}</Code>
                        </Group>
                      }
                    >
                      <Text size="sm">{entry.field_issues[0]?.detail_zh}</Text>
                      <Text size="xs" c="dimmed" mt={6}>
                        {entry.field_issues[0]?.suggestion_zh}
                      </Text>
                    </Alert>
                  ))}
                </Stack>
              </Paper>
            )}

            <Paper p="lg" radius="md" withBorder>
              <Title order={3} size="h5" mb="xs">
                按问题类型
              </Title>
              <Text size="sm" c="dimmed" mb="md">
                <Text span fw={500} c="bright">
                  长度溢出
                </Text>{' '}
                与{' '}
                <Text span fw={500} c="bright">
                  归一化需求
                </Text>{' '}
                是两件事：<Code>GER</Code> / <Code>JPN</Code> / <Code>USA</Code> 长度恰好是 3，
                塞得进 <Code>Country</Code> 的 <Code>CHAR(3)</Code>，
                <Text span fw={500}>
                  却依然是非法值
                </Text>
                。只做长度校验会放它们过去。
              </Text>

              <Accordion variant="separated" radius="md" chevronPosition="left">
                {types.map(([issueType, count]) => (
                  <IssueTypeCard
                    key={issueType}
                    issueType={issueType}
                    count={count}
                    fieldIssues={fieldIssuesByType[issueType] ?? []}
                    recordIssues={recordIssuesByType[issueType] ?? []}
                  />
                ))}
              </Accordion>
            </Paper>

            <Paper p="lg" radius="md" withBorder>
              <Title order={3} size="h5" mb="md">
                字段级裁决
              </Title>
              <Box style={{ overflowX: 'auto' }}>
                <Table striped highlightOnHover verticalSpacing="sm" miw={860}>
                  <Table.Thead>
                    <Table.Tr>
                      <Table.Th>legacy 字段</Table.Th>
                      <Table.Th>目标字段</Table.Th>
                      <Table.Th w={90}>语义正确</Table.Th>
                      <Table.Th w={90}>可写入</Table.Th>
                      <Table.Th w={190}>裁决</Table.Th>
                    </Table.Tr>
                  </Table.Thead>
                  <Table.Tbody>
                    {report.field_view.map((entry, index) => {
                      const verdict = VERDICT[entry.verdict]
                      return (
                        <Table.Tr key={`${entry.legacy_field ?? 'unmapped'}-${index}`}>
                          <Table.Td>
                            {entry.legacy_field ? (
                              <Code>{entry.legacy_field}</Code>
                            ) : (
                              <Text size="sm" c="dimmed" fs="italic">
                                —
                              </Text>
                            )}
                          </Table.Td>
                          <Table.Td>
                            <Group gap={6}>
                              {entry.target ? (
                                <Text size="sm" ff="monospace">
                                  {entry.target}
                                </Text>
                              ) : (
                                <Text size="sm" c="dimmed" fs="italic">
                                  —
                                </Text>
                              )}
                              {entry.based_on_unverified && (
                                <Tooltip label="该目标字段 verification_status=unverified">
                                  <Badge size="xs" variant="outline" color="yellow">
                                    未核实
                                  </Badge>
                                </Tooltip>
                              )}
                            </Group>
                          </Table.Td>
                          <Table.Td>
                            <TriState value={entry.semantic_match} />
                          </Table.Td>
                          <Table.Td>
                            <TriState value={entry.loadable} />
                          </Table.Td>
                          <Table.Td>
                            <Tooltip label={verdict.hint} w={280} multiline>
                              <Badge
                                size="sm"
                                variant="light"
                                style={{
                                  backgroundColor: `${verdict.color}22`,
                                  color: verdict.color,
                                }}
                              >
                                {verdict.label}
                              </Badge>
                            </Tooltip>
                          </Table.Td>
                        </Table.Tr>
                      )
                    })}
                  </Table.Tbody>
                </Table>
              </Box>
            </Paper>

            <Paper p="lg" radius="md" withBorder>
              <Title order={3} size="h5" mb="sm">
                已登记但未实现的检查
              </Title>
              <Stack gap="sm">
                {report.deferred_checks.map((check) => (
                  <Alert key={check.check} variant="light" color="gray" p="sm">
                    <Group gap={8} mb={6}>
                      <Code>{check.check}</Code>
                      <Badge size="xs" variant="outline" color="gray">
                        {check.status}
                      </Badge>
                      {check.fields.map((field) => (
                        <Code key={field}>{field}</Code>
                      ))}
                    </Group>
                    <Text size="sm">{check.reason_zh}</Text>
                    <Text size="xs" c="dimmed" mt={4}>
                      阻塞于：{check.blocked_by_zh}
                    </Text>
                  </Alert>
                ))}
              </Stack>
            </Paper>

            <Paper p="lg" radius="md" withBorder>
              <Title order={3} size="h5" mb="sm">
                已实现的检查
              </Title>
              <List size="sm" spacing={4}>
                {Object.entries(report._meta.checks_implemented).map(([key, value]) => (
                  <List.Item key={key}>
                    <Text span c="dimmed" size="xs" ff="monospace">
                      {key}
                    </Text>{' '}
                    <Code>{value}</Code>
                  </List.Item>
                ))}
              </List>
              <Text size="xs" c="dimmed" mt="sm">
                部分检查在当前数据上命中 0 条（无主键映射、无 nullable=false 目标、无 allowed_values），
                这是数据特征，不是检查缺失。
              </Text>
            </Paper>

            <Text size="xs" c="dimmed" ta="right">
              内容哈希 <Code>{report._run_info.content_sha256.slice(0, 16)}</Code>
            </Text>
          </Stack>
        )
      }}
    </ReportGate>
  )
}
