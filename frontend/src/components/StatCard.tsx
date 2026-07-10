/**
 * 摘要数字卡。
 *
 * 按 dataviz 的取形规则：单个标量的工作是「一个头条数字」，不是图表。
 * 因此这里是 stat tile，没有 sparkline、没有环形图。
 */

import { Group, Paper, Text, ThemeIcon } from '@mantine/core'

interface Props {
  label: string
  value: string | number
  hint?: string
  /** 状态色。给了就在数字旁放一个色点——颜色永不单独传意，旁边一定有文字。 */
  accent?: string
}

export function StatCard({ label, value, hint, accent }: Props) {
  return (
    <Paper p="md" radius="md" withBorder style={{ flex: 1, minWidth: 160 }}>
      <Text size="xs" c="dimmed" tt="uppercase" fw={500} style={{ letterSpacing: '0.05em' }}>
        {label}
      </Text>
      <Group gap={8} align="baseline" mt={6}>
        {accent && (
          <ThemeIcon size={9} radius="xl" style={{ backgroundColor: accent }} aria-hidden>
            <span />
          </ThemeIcon>
        )}
        <Text fz={28} fw={600} lh={1.1} ff="monospace">
          {value}
        </Text>
      </Group>
      {hint && (
        <Text size="xs" c="dimmed" mt={4}>
          {hint}
        </Text>
      )}
    </Paper>
  )
}
