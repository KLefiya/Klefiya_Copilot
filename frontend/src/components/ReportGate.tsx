/**
 * 报告的三态门：加载中 / 未生成 / 出错。数据就绪才渲染子内容。
 *
 * 「未生成」被单独拎出来，因为它不是错误——是还没跑那个脚本。
 * 后端在 404 的 detail 里带了 generated_by，这里原样展示，而不是显示一个红叉。
 */

import { Alert, Center, Code, Loader, Stack, Text } from '@mantine/core'
import type { ApiError } from '../api'
import { notGeneratedInfo } from '../lib/useReport'

interface Props<T> {
  loading: boolean
  error: ApiError | null
  data: T | null
  children: (data: T) => React.ReactNode
}

export function ReportGate<T>({ loading, error, data, children }: Props<T>) {
  if (loading) {
    return (
      <Center h={280}>
        <Stack align="center" gap="xs">
          <Loader size="sm" />
          <Text size="sm" c="dimmed">
            读取报告中…
          </Text>
        </Stack>
      </Center>
    )
  }

  if (error) {
    const pending = notGeneratedInfo(error)

    if (pending) {
      return (
        <Alert color="gray" variant="light" title="该报告尚未生成">
          <Stack gap="xs">
            <Text size="sm">
              这不是错误——只是还没跑生成它的脚本。跑完刷新本页即可。
            </Text>
            <Code block>{pending.generatedBy}</Code>
            <Text size="xs" c="dimmed">
              预期落盘位置 <Code>{pending.expectedPath}</Code>
            </Text>
          </Stack>
        </Alert>
      )
    }

    return (
      <Alert color="red" variant="light" title="读取失败">
        <Stack gap="xs">
          <Text size="sm">{error.message}</Text>
          <Code block>{JSON.stringify(error.detail, null, 2)}</Code>
        </Stack>
      </Alert>
    )
  }

  return <>{data && children(data)}</>
}
