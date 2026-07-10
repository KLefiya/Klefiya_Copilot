/**
 * country 字段的 14 种写法分布。
 *
 * 【取形】14 个名义类别比大小 → 排序后的横向条形图。不是饼图，不是 14 个身份色。
 *
 * 【配色】这 14 个值其实只有 3 个真实国家，颜色编码的是【合法性状态】而非身份：
 *   good     符合 ISO 3166-1 alpha-2（DE / US / JP）
 *   warning  长度塞得进 Country 的 CHAR(3)，但值非法（de / us / GER / JPN / USA）
 *   critical 值非法且长度溢出（Germany / Deutschland / United States / …）
 * 中间那一档正是本项目的核心洞察：长度检查与归一化检查是【正交】的，
 * GER/JPN/USA 三个值长度合格却依然是错的——只做长度校验会放它们过去。
 *
 * 【校验】状态三色取自 dataviz 的 status palette，已用 validate_palette.py
 * 对照本项目画布 #171a21 实测：对比度全部 ≥3:1，最差相邻对色盲区分度 ΔE 16.9。
 * 状态色绝不单独传意——图例带文字、每根条带数值标签。
 */

import { Box, Group, Paper, Stack, Text } from '@mantine/core'
import {
  Bar,
  BarChart,
  Cell,
  LabelList,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { STATUS } from '../lib/theme'
import type { ValueCount } from '../lib/reports'

/** A_BusinessPartnerAddress.Country 的 max_length，来自 business_partner_target_schema.json。 */
const TARGET_MAX_LENGTH = 3

/** ISO 3166-1 alpha-2 的规范形态。与 pre_migration_validation.py 的 NORMALIZATION_DOMAINS 一致。 */
const ISO_ALPHA2 = /^[A-Z]{2}$/

export type Legality = 'conforming' | 'fits_but_invalid' | 'invalid_and_overflows'

export const LEGALITY: Record<Legality, { color: string; label: string; hint: string }> = {
  conforming: {
    color: STATUS.good,
    label: '合法 ISO 代码',
    hint: '符合 ^[A-Z]{2}$，可直接加载',
  },
  fits_but_invalid: {
    color: STATUS.warning,
    label: `长度合格但值非法`,
    hint: `长度 ≤ ${TARGET_MAX_LENGTH}，塞得进目标字段——但只做长度校验就会漏掉它们`,
  },
  invalid_and_overflows: {
    color: STATUS.critical,
    label: '非法且长度溢出',
    hint: `长度 > ${TARGET_MAX_LENGTH}，加载时会被截断`,
  },
}

/**
 * 三态判定。导出以便单独做单元测试——图表本身在无头 DOM 里没有布局、
 * ResponsiveContainer 宽高为 0，条形和轴标签都不会渲染，断言不到。
 */
export function classify(value: string): Legality {
  if (ISO_ALPHA2.test(value)) return 'conforming'
  if (value.length <= TARGET_MAX_LENGTH) return 'fits_but_invalid'
  return 'invalid_and_overflows'
}

interface Row extends ValueCount {
  legality: Legality
}

function ChartTooltip({
  active,
  payload,
}: {
  active?: boolean
  payload?: { payload: Row }[]
}) {
  if (!active || !payload?.length) return null
  const row = payload[0].payload
  const meta = LEGALITY[row.legality]

  return (
    <Paper p="xs" radius="sm" withBorder shadow="md">
      <Group gap={6} align="center">
        <Box w={8} h={8} bg={meta.color} style={{ borderRadius: 2 }} aria-hidden />
        <Text size="sm" ff="monospace" fw={600}>
          {row.value}
        </Text>
      </Group>
      <Text size="xs" c="dimmed" mt={4}>
        {row.count} 条记录 · 长度 {row.value.length}
      </Text>
      <Text size="xs" mt={2} style={{ color: meta.color }}>
        {meta.label}
      </Text>
    </Paper>
  )
}

export function CountryVariantsChart({ distribution }: { distribution: ValueCount[] }) {
  const rows: Row[] = [...distribution]
    .sort((a, b) => b.count - a.count)
    .map((item) => ({ ...item, legality: classify(item.value) }))

  const counts = rows.reduce<Record<Legality, number>>(
    (acc, row) => ({ ...acc, [row.legality]: (acc[row.legality] ?? 0) + 1 }),
    { conforming: 0, fits_but_invalid: 0, invalid_and_overflows: 0 },
  )

  return (
    <Stack gap="sm">
      {/* 图例：≥2 个系列必须有图例，且状态色一律配文字。 */}
      <Group gap="lg" wrap="wrap">
        {(Object.keys(LEGALITY) as Legality[]).map((key) => (
          <Group key={key} gap={6} align="center" wrap="nowrap">
            <Box
              w={10}
              h={10}
              bg={LEGALITY[key].color}
              style={{ borderRadius: 2, flexShrink: 0 }}
              aria-hidden
            />
            <Text size="xs">
              {LEGALITY[key].label}
              <Text span c="dimmed" ff="monospace">
                {' '}
                ×{counts[key]}
              </Text>
            </Text>
          </Group>
        ))}
      </Group>

      <Box h={rows.length * 26 + 24}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart
            data={rows}
            layout="vertical"
            margin={{ top: 0, right: 44, bottom: 0, left: 8 }}
            barCategoryGap={2}
          >
            <XAxis type="number" hide />
            <YAxis
              type="category"
              dataKey="value"
              width={104}
              tickLine={false}
              axisLine={false}
              tick={{ fill: 'var(--mantine-color-dimmed)', fontSize: 12, fontFamily: 'monospace' }}
            />
            <Tooltip
              cursor={{ fill: 'rgba(255,255,255,0.04)' }}
              content={<ChartTooltip />}
            />
            <Bar dataKey="count" radius={[0, 4, 4, 0]} isAnimationActive={false}>
              {rows.map((row) => (
                <Cell key={row.value} fill={LEGALITY[row.legality].color} />
              ))}
              <LabelList
                dataKey="count"
                position="right"
                offset={8}
                style={{ fill: 'var(--mantine-color-dimmed)', fontSize: 11, fontFamily: 'monospace' }}
              />
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </Box>

      <Text size="xs" c="dimmed">
        {LEGALITY.fits_but_invalid.hint}。这正是长度校验与归一化校验必须分开的原因——
        <Text span ff="monospace">
          GER
        </Text>{' '}
        /{' '}
        <Text span ff="monospace">
          JPN
        </Text>{' '}
        /{' '}
        <Text span ff="monospace">
          USA
        </Text>{' '}
        长度合格，值却是错的。
      </Text>
    </Stack>
  )
}
