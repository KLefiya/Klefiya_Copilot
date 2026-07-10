/** 视图二：字段映射建议（vendor_field_mapping）。 */

import {
  Accordion,
  Alert,
  Badge,
  Box,
  Code,
  Divider,
  Group,
  List,
  Paper,
  Progress,
  Stack,
  Table,
  Text,
  ThemeIcon,
  Title,
  Tooltip,
} from '@mantine/core'
import { ReportGate } from '../components/ReportGate'
import { StatCard } from '../components/StatCard'
import type { Candidate, Mapping, MappingReport } from '../lib/reports'
import { MAPPING_STATUS, STATUS, bandColor } from '../lib/theme'
import { useReport } from '../lib/useReport'

/** 信号条：每一路信号的强度。四路信号并列，读者一眼看出是谁在支撑这条映射。 */
function SignalBar({ label, value, hint }: { label: string; value: number; hint?: string }) {
  return (
    <Group gap="sm" wrap="nowrap">
      <Text size="xs" c="dimmed" w={92} style={{ flexShrink: 0 }}>
        {label}
      </Text>
      <Progress value={value * 100} size="sm" w={140} color="brand" style={{ flexShrink: 0 }} />
      <Text size="xs" ff="monospace" w={48} style={{ flexShrink: 0 }}>
        {value.toFixed(3)}
      </Text>
      {hint && (
        <Text size="xs" c="dimmed">
          {hint}
        </Text>
      )}
    </Group>
  )
}

function CandidateDetail({ candidate, rank }: { candidate: Candidate; rank: number }) {
  const { signals } = candidate

  return (
    <Paper p="md" radius="sm" withBorder bg="dark.8">
      <Group justify="space-between" wrap="nowrap" mb="sm">
        <Group gap={8}>
          <Badge size="xs" variant="default">
            #{rank}
          </Badge>
          <Code>{candidate.qualified}</Code>
        </Group>
        <Group gap={6}>
          <Box w={8} h={8} bg={bandColor(candidate.band)} style={{ borderRadius: 2 }} aria-hidden />
          <Text size="sm" ff="monospace" fw={600}>
            {candidate.confidence.toFixed(3)}
          </Text>
          <Text size="xs" c="dimmed">
            {candidate.band}
          </Text>
        </Group>
      </Group>

      <Text size="xs" c="dimmed" mb="sm">
        {candidate.target_type}
        {candidate.target_max_length !== null && ` · max_length ${candidate.target_max_length}`}
        {' · '}
        {candidate.target_description_zh}
      </Text>

      <Stack gap={6}>
        <SignalBar label="语义相似度" value={signals.semantic} />
        <SignalBar label="模糊匹配" value={signals.fuzzy} />
        <SignalBar label="类型兼容性" value={signals.type} hint="乘性闸门，不能凭空制造匹配" />
      </Stack>

      <Divider my="sm" />

      <Group gap="lg">
        <Group gap={6}>
          <Text size="xs" c="dimmed">
            alias
          </Text>
          {signals.alias ? (
            <Badge size="xs" variant="light" color="green">
              {signals.alias}
            </Badge>
          ) : (
            <Text size="xs" c="dimmed">
              未命中
            </Text>
          )}
        </Group>
        <Group gap={6}>
          <Text size="xs" c="dimmed">
            共享词元
          </Text>
          {signals.lexical_overlap.length > 0 ? (
            signals.lexical_overlap.map((token) => (
              <Code key={token}>{token}</Code>
            ))
          ) : (
            <Tooltip label="零共享词元 —— 匹配完全靠 embedding，没有任何字面锚点" w={280} multiline>
              <Badge size="xs" variant="light" color="orange">
                零重叠
              </Badge>
            </Tooltip>
          )}
        </Group>
      </Group>

      {candidate.warnings.length > 0 && (
        <Alert color="yellow" variant="light" mt="sm" p="xs">
          <List size="xs" spacing={2}>
            {candidate.warnings.map((warning) => (
              <List.Item key={warning}>{warning}</List.Item>
            ))}
          </List>
        </Alert>
      )}
    </Paper>
  )
}

function MappingRow({ mapping }: { mapping: Mapping }) {
  const status = MAPPING_STATUS[mapping.status]
  const top = mapping.candidates[0]
  const readOnlyWarning = top?.warnings.find((w) => w.includes('只读'))

  return (
    <Accordion.Item value={mapping.legacy_field}>
      <Accordion.Control>
        <Group justify="space-between" wrap="nowrap" pr="sm">
          <Group gap="md" wrap="nowrap" style={{ minWidth: 0 }}>
            <Code style={{ flexShrink: 0 }}>{mapping.legacy_field}</Code>
            <Text size="sm" c="dimmed" style={{ flexShrink: 0 }}>
              →
            </Text>
            {mapping.recommendation ? (
              <Text size="sm" ff="monospace" truncate>
                {mapping.recommendation}
              </Text>
            ) : (
              <Text size="sm" c="dimmed" fs="italic">
                无可信落点
              </Text>
            )}
          </Group>

          <Group gap="sm" wrap="nowrap" style={{ flexShrink: 0 }}>
            {readOnlyWarning && (
              <Tooltip label={readOnlyWarning} w={280} multiline>
                <Badge size="xs" variant="light" color="yellow">
                  只读目标
                </Badge>
              </Tooltip>
            )}
            <Tooltip label={`置信度 ${mapping.confidence.toFixed(3)} · ${mapping.band}`}>
              <Group gap={6} wrap="nowrap">
                <Box
                  w={8}
                  h={8}
                  bg={bandColor(mapping.band)}
                  style={{ borderRadius: 2 }}
                  aria-hidden
                />
                <Text size="sm" ff="monospace" w={46}>
                  {mapping.confidence.toFixed(3)}
                </Text>
              </Group>
            </Tooltip>
            <Badge
              size="sm"
              variant="light"
              style={{ backgroundColor: `${status.color}22`, color: status.color }}
            >
              {status.label}
            </Badge>
          </Group>
        </Group>
      </Accordion.Control>

      <Accordion.Panel>
        <Stack gap="md">
          <Group gap="lg" wrap="wrap">
            <Text size="xs" c="dimmed">
              观测最大长度{' '}
              <Text span ff="monospace" c="bright">
                {mapping.legacy_profile.observed_max_length}
              </Text>
            </Text>
            <Text size="xs" c="dimmed">
              推断类型{' '}
              <Text span ff="monospace" c="bright">
                {mapping.legacy_profile.inferred_kind}
              </Text>
            </Text>
            <Text size="xs" c="dimmed">
              样例{' '}
              {mapping.legacy_profile.samples.slice(0, 2).map((sample) => (
                <Code key={sample}>{sample}</Code>
              ))}
            </Text>
          </Group>

          <div>
            <Text size="xs" c="dimmed" mb={6}>
              候选目标字段（按置信度排序）
            </Text>
            <Stack gap="sm">
              {mapping.candidates.map((candidate, index) => (
                <CandidateDetail key={candidate.qualified} candidate={candidate} rank={index + 1} />
              ))}
            </Stack>
          </div>
        </Stack>
      </Accordion.Panel>
    </Accordion.Item>
  )
}

export function MappingView() {
  const { loading, error, data } = useReport<MappingReport>('vendor_field_mapping')

  return (
    <ReportGate loading={loading} error={error} data={data}>
      {(report) => {
        const byStatus = report.mappings.reduce<Record<string, number>>((acc, m) => {
          acc[m.status] = (acc[m.status] ?? 0) + 1
          return acc
        }, {})

        return (
          <Stack gap="xl">
            <div>
              <Title order={2} size="h3">
                字段映射建议
              </Title>
              <Text size="sm" c="dimmed" mt={4}>
                {report._meta.legacy_record_count} 条遗留记录的 {report.mappings.length} 个字段 ×{' '}
                {report._meta.target_field_count} 个目标字段
              </Text>
            </div>

            <Alert color="gray" variant="light" title="全部为建议，不是既成事实">
              <Text size="sm">{report._meta.disclaimer}</Text>
            </Alert>

            <Group grow align="stretch" wrap="wrap">
              {(Object.keys(MAPPING_STATUS) as (keyof typeof MAPPING_STATUS)[]).map((key) => (
                <StatCard
                  key={key}
                  label={MAPPING_STATUS[key].label}
                  value={byStatus[key] ?? 0}
                  accent={MAPPING_STATUS[key].color}
                />
              ))}
            </Group>

            {report.gaps.length > 0 && (
              <Paper p="lg" radius="md" withBorder>
                <Title order={3} size="h5" mb="md">
                  落点缺口
                </Title>
                <Stack gap="sm">
                  {report.gaps.map((gap) => {
                    const meta = MAPPING_STATUS[gap.status] ?? MAPPING_STATUS.no_confident_target
                    return (
                      <Alert
                        key={gap.legacy_field}
                        variant="light"
                        color={gap.status === 'possible_false_friend' ? 'orange' : 'red'}
                        p="sm"
                        title={
                          <Group gap={8}>
                            <Code>{gap.legacy_field}</Code>
                            <Badge
                              size="xs"
                              variant="light"
                              style={{ backgroundColor: `${meta.color}22`, color: meta.color }}
                            >
                              {meta.label}
                            </Badge>
                          </Group>
                        }
                      >
                        <Text size="sm">{gap.message}</Text>
                      </Alert>
                    )
                  })}
                </Stack>
              </Paper>
            )}

            <Paper p="lg" radius="md" withBorder>
              <Group justify="space-between" mb="md">
                <Title order={3} size="h5">
                  逐字段映射
                </Title>
                <Text size="xs" c="dimmed">
                  展开可见完整 signals
                </Text>
              </Group>

              <Accordion variant="separated" radius="md" chevronPosition="left">
                {report.mappings.map((mapping) => (
                  <MappingRow key={mapping.legacy_field} mapping={mapping} />
                ))}
              </Accordion>
            </Paper>

            <Paper p="lg" radius="md" withBorder>
              <Title order={3} size="h5" mb="sm">
                打分口径
              </Title>
              <Table fz="sm" verticalSpacing="xs">
                <Table.Tbody>
                  <Table.Tr>
                    <Table.Td w={140} c="dimmed">
                      公式
                    </Table.Td>
                    <Table.Td>
                      <Code>{String(report._meta.scoring.formula)}</Code>
                    </Table.Td>
                  </Table.Tr>
                  <Table.Tr>
                    <Table.Td c="dimmed">类型闸门</Table.Td>
                    <Table.Td>
                      <Code>{String(report._meta.scoring.type_gate)}</Code>
                    </Table.Td>
                  </Table.Tr>
                  <Table.Tr>
                    <Table.Td c="dimmed">说明</Table.Td>
                    <Table.Td>
                      <Text size="sm">{String(report._meta.scoring.note)}</Text>
                    </Table.Td>
                  </Table.Tr>
                  <Table.Tr>
                    <Table.Td c="dimmed">阈值</Table.Td>
                    <Table.Td>
                      <Group gap="md">
                        <Group gap={6}>
                          <ThemeIcon size={9} radius="xl" style={{ backgroundColor: STATUS.good }}>
                            <span />
                          </ThemeIcon>
                          <Text size="xs">high ≥ {report._meta.thresholds.high}</Text>
                        </Group>
                        <Group gap={6}>
                          <ThemeIcon size={9} radius="xl" style={{ backgroundColor: STATUS.warning }}>
                            <span />
                          </ThemeIcon>
                          <Text size="xs">medium ≥ {report._meta.thresholds.medium}</Text>
                        </Group>
                        <Group gap={6}>
                          <ThemeIcon size={9} radius="xl" style={{ backgroundColor: STATUS.critical }}>
                            <span />
                          </ThemeIcon>
                          <Text size="xs">no-match &lt; {report._meta.thresholds.no_match}</Text>
                        </Group>
                        <Text size="xs" c="dimmed">
                          alias 命中下限 {report._meta.thresholds.alias_confidence_floor}
                        </Text>
                      </Group>
                    </Table.Td>
                  </Table.Tr>
                  <Table.Tr>
                    <Table.Td c="dimmed">embedding</Table.Td>
                    <Table.Td>
                      <Code>{report._meta.embedding_model}</Code>
                    </Table.Td>
                  </Table.Tr>
                </Table.Tbody>
              </Table>
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
