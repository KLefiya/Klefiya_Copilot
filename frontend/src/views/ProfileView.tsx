/** 视图一：数据质量画像（vendor_profile_report）。 */

import {
  Accordion,
  Alert,
  Badge,
  Box,
  Code,
  Group,
  Paper,
  Progress,
  Stack,
  Table,
  Text,
  Title,
  Tooltip,
} from '@mantine/core'
import { CountryVariantsChart } from '../components/CountryVariantsChart'
import { ReportGate } from '../components/ReportGate'
import { StatCard } from '../components/StatCard'
import type { FieldProfile, ProfileReport } from '../lib/reports'
import { ISSUE_TYPE_LABEL, SEVERITY_LABEL } from '../lib/reports'
import { STATUS } from '../lib/theme'
import { useReport } from '../lib/useReport'

const pct = (value: number) => `${(value * 100).toFixed(1)}%`

function MissingRateCell({ profile, threshold }: { profile: FieldProfile; threshold: number }) {
  const over = profile.missing_rate > threshold
  return (
    <Stack gap={4}>
      <Group gap={6} justify="space-between" wrap="nowrap">
        <Text size="sm" ff="monospace" c={over ? undefined : 'dimmed'} fw={over ? 600 : 400}>
          {pct(profile.missing_rate)}
        </Text>
        {over && (
          <Badge size="xs" variant="light" color="red">
            超阈值
          </Badge>
        )}
      </Group>
      <Progress
        value={profile.missing_rate * 100}
        size="xs"
        color={over ? STATUS.critical : 'gray'}
        aria-label={`缺失率 ${pct(profile.missing_rate)}`}
      />
      <Text size="xs" c="dimmed">
        {profile.missing_count} / {profile.record_count}
      </Text>
    </Stack>
  )
}

function FormatCell({ profile }: { profile: FieldProfile }) {
  // format_variants 为 null 意味着这是自由文本字段——格式一致性检测对它不适用。
  if (profile.format_variants === null) {
    return (
      <Tooltip
        label={`自由文本，格式一致性检测跳过（判定来源：${profile.free_text_source}）`}
        multiline
        w={260}
      >
        <Badge size="sm" variant="outline" color="gray">
          自由文本 · 不适用
        </Badge>
      </Tooltip>
    )
  }

  const many = profile.format_variants > 5
  return (
    <Group gap={6}>
      <Text size="sm" ff="monospace" fw={many ? 600 : 400}>
        {profile.format_variants}
      </Text>
      <Text size="xs" c="dimmed">
        种签名
      </Text>
      {many && (
        <Badge size="xs" variant="light" color="red">
          高度不一致
        </Badge>
      )}
    </Group>
  )
}

export function ProfileView() {
  const { loading, error, data } = useReport<ProfileReport>('vendor_profile_report')

  return (
    <ReportGate loading={loading} error={error} data={data}>
      {(report) => {
        const threshold = report._meta.thresholds.missing_rate_flag_threshold
        const fields = Object.entries(report.fields)
        const freeTextCount = fields.filter(([, f]) => f.is_free_text).length
        const country = report.fields.country

        return (
          <Stack gap="xl">
            <div>
              <Title order={2} size="h3">
                数据质量画像
              </Title>
              <Text size="sm" c="dimmed" mt={4}>
                对合成的遗留供应商主数据做自动化体检 · 源{' '}
                <Code>{report._meta.source_file}</Code>
              </Text>
            </div>

            <Group grow align="stretch" wrap="wrap">
              <StatCard label="记录数" value={report._meta.record_count} hint="含刻意注入的重复与脏数据" />
              <StatCard label="字段数" value={report._meta.field_count} />
              <StatCard
                label="质量问题"
                value={report.quality_flags.length}
                accent={STATUS.critical}
                hint="quality_flags"
              />
              <StatCard
                label="自由文本字段"
                value={freeTextCount}
                accent={STATUS.warning}
                hint="已跳过格式一致性检测"
              />
            </Group>

            {country?.value_distribution && (
              <Paper p="lg" radius="md" withBorder>
                <Stack gap="xs" mb="md">
                  <Title order={3} size="h5">
                    <Code>country</Code> 字段的 {country.distinct_count} 种写法
                  </Title>
                  <Text size="sm" c="dimmed">
                    格式签名只有 {country.format_variants} 种——因为 <Code>DE</Code> /{' '}
                    <Code>de</Code> / <Code>Germany</Code> / <Code>GER</Code> 在签名里全都是{' '}
                    <Code>A</Code>。签名对大小写与拼写变体天生不敏感，真正暴露问题的是取值分布。
                    两个维度互补，不冗余。
                  </Text>
                </Stack>
                <CountryVariantsChart distribution={country.value_distribution} />
              </Paper>
            )}

            <Paper p="lg" radius="md" withBorder>
              <Title order={3} size="h5" mb="md">
                质量问题清单
              </Title>
              <Stack gap="sm">
                {report.quality_flags.map((flag, index) => (
                  <Alert
                    key={`${flag.field}-${flag.issue_type}-${index}`}
                    variant="light"
                    color={flag.severity === 'high' ? 'red' : 'yellow'}
                    p="sm"
                    title={
                      <Group gap={8}>
                        <Code>{flag.field}</Code>
                        <Badge size="xs" variant="light" color={flag.severity === 'high' ? 'red' : 'yellow'}>
                          {SEVERITY_LABEL[flag.severity]}
                        </Badge>
                        <Text size="xs" c="dimmed">
                          {ISSUE_TYPE_LABEL[flag.issue_type] ?? flag.issue_type}
                        </Text>
                      </Group>
                    }
                  >
                    <Text size="sm">{flag.message}</Text>
                  </Alert>
                ))}
              </Stack>
            </Paper>

            <Paper p="lg" radius="md" withBorder>
              <Title order={3} size="h5" mb="xs">
                字段级明细
              </Title>
              <Text size="sm" c="dimmed" mb="md">
                缺失率超过 {pct(threshold)} 的标红；自由文本字段的格式一致性检测标为「不适用」。
              </Text>

              <Box style={{ overflowX: 'auto' }}>
                <Table striped highlightOnHover verticalSpacing="sm" miw={880}>
                  <Table.Thead>
                    <Table.Tr>
                      <Table.Th>字段</Table.Th>
                      <Table.Th w={170}>缺失率</Table.Th>
                      <Table.Th w={150}>唯一性</Table.Th>
                      <Table.Th w={190}>格式一致性</Table.Th>
                      <Table.Th w={110}>平均长度</Table.Th>
                    </Table.Tr>
                  </Table.Thead>
                  <Table.Tbody>
                    {fields.map(([name, profile]) => (
                      <Table.Tr key={name}>
                        <Table.Td>
                          <Group gap={6}>
                            <Code>{name}</Code>
                            {profile.is_probable_identifier && (
                              <Tooltip label={`distinct_ratio ${profile.distinct_ratio} —— 疑似唯一标识符`}>
                                <Badge size="xs" variant="light" color="blue">
                                  ID
                                </Badge>
                              </Tooltip>
                            )}
                            {profile.is_free_text && (
                              <Badge size="xs" variant="outline" color="gray">
                                自由文本
                              </Badge>
                            )}
                          </Group>
                        </Table.Td>
                        <Table.Td>
                          <MissingRateCell profile={profile} threshold={threshold} />
                        </Table.Td>
                        <Table.Td>
                          <Text size="sm" ff="monospace">
                            {profile.distinct_count}
                          </Text>
                          <Text size="xs" c="dimmed">
                            ratio {profile.distinct_ratio.toFixed(3)}
                          </Text>
                        </Table.Td>
                        <Table.Td>
                          <FormatCell profile={profile} />
                        </Table.Td>
                        <Table.Td>
                          <Text size="sm" ff="monospace" c="dimmed">
                            {profile.avg_length.toFixed(1)}
                          </Text>
                        </Table.Td>
                      </Table.Tr>
                    ))}
                  </Table.Tbody>
                </Table>
              </Box>
            </Paper>

            <Paper p="lg" radius="md" withBorder>
              <Title order={3} size="h5" mb="md">
                格式签名明细
              </Title>
              <Accordion variant="separated" radius="md">
                {fields
                  .filter(([, f]) => f.format_signatures !== null)
                  .map(([name, profile]) => (
                    <Accordion.Item key={name} value={name}>
                      <Accordion.Control>
                        <Group gap={10}>
                          <Code>{name}</Code>
                          <Badge
                            size="xs"
                            variant="light"
                            color={(profile.format_variants ?? 0) > 5 ? 'red' : 'gray'}
                          >
                            {profile.format_variants} 种签名
                          </Badge>
                        </Group>
                      </Accordion.Control>
                      <Accordion.Panel>
                        <Table verticalSpacing="xs" fz="sm">
                          <Table.Thead>
                            <Table.Tr>
                              <Table.Th w={160}>签名</Table.Th>
                              <Table.Th w={80}>条数</Table.Th>
                              <Table.Th>示例</Table.Th>
                            </Table.Tr>
                          </Table.Thead>
                          <Table.Tbody>
                            {profile.format_signatures?.map((sig) => (
                              <Table.Tr key={sig.signature}>
                                <Table.Td>
                                  <Code>{sig.signature}</Code>
                                </Table.Td>
                                <Table.Td ff="monospace">{sig.count}</Table.Td>
                                <Table.Td>
                                  <Code>{sig.example}</Code>
                                </Table.Td>
                              </Table.Tr>
                            ))}
                          </Table.Tbody>
                        </Table>
                      </Accordion.Panel>
                    </Accordion.Item>
                  ))}
              </Accordion>
              <Text size="xs" c="dimmed" mt="sm">
                签名规则：数字→<Code>D</Code>，字母→<Code>A</Code>，其它符号保留，连续同类压缩。
                自由文本字段不参与——它们天然格式多样，强行检测只会产生噪音。
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
