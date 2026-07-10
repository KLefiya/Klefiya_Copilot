/**
 * 视图四：实体解析 · 重复供应商（vendor_duplicate_report）。
 *
 * 该报告尚未生成（Splink 实体解析组件还没做）。这里不写第二套「敬请期待」逻辑，
 * 直接复用 ReportGate 的「未生成」分支——它会读后端 404 里的 generated_by 并展示。
 * Splink 那步跑完，本视图自动就有数据了，届时再补可视化。
 */

import { Code, Paper, Stack, Text, Title } from '@mantine/core'
import { ReportGate } from '../components/ReportGate'
import { useReport } from '../lib/useReport'

export function DuplicateView() {
  const { loading, error, data } = useReport<unknown>('vendor_duplicate_report')

  return (
    <Stack gap="xl">
      <div>
        <Title order={2} size="h3">
          实体解析 · 重复供应商
        </Title>
        <Text size="sm" c="dimmed" mt={4}>
          用 Splink 找出同一供应商的名称拼写变体与完全重复记录
        </Text>
      </div>

      <ReportGate loading={loading} error={error} data={data}>
        {(report) => (
          <Paper p="lg" radius="md" withBorder>
            <Text size="sm" c="dimmed" mb="sm">
              报告已生成，但本视图的可视化还没做。先把原始 JSON 放这里。
            </Text>
            <Code block>{JSON.stringify(report, null, 2).slice(0, 4000)}</Code>
          </Paper>
        )}
      </ReportGate>
    </Stack>
  )
}
