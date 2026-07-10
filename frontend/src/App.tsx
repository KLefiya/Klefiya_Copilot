/**
 * 应用外壳：侧边栏导航 + 视图切换。
 *
 * 侧边栏用 /api/health 返回的报告目录驱动——未生成的报告在导航里就标出来，
 * 不用点进去才发现是空的。
 */

import { useEffect, useState } from 'react'
import {
  AppShell,
  Badge,
  Box,
  Burger,
  Code,
  Group,
  NavLink,
  ScrollArea,
  Stack,
  Text,
  Title,
  Tooltip,
} from '@mantine/core'
import { useDisclosure } from '@mantine/hooks'
import { getHealth, type Health } from './api'
import { DuplicateView } from './views/DuplicateView'
import { MappingView } from './views/MappingView'
import { ProfileView } from './views/ProfileView'
import { ValidationView } from './views/ValidationView'
import { STATUS } from './lib/theme'

interface NavItem {
  key: string
  label: string
  /** 对应的报告名，用于查 /api/health 里的 available */
  report: string
  element: React.ReactNode
}

const MODULE_ONE: NavItem[] = [
  { key: 'profile', label: '数据质量画像', report: 'vendor_profile_report', element: <ProfileView /> },
  { key: 'duplicate', label: '实体解析', report: 'vendor_duplicate_report', element: <DuplicateView /> },
  { key: 'mapping', label: '字段映射建议', report: 'vendor_field_mapping', element: <MappingView /> },
  { key: 'validation', label: '迁移前校验', report: 'vendor_validation_report', element: <ValidationView /> },
]

export default function App() {
  const [opened, { toggle }] = useDisclosure()
  const [active, setActive] = useState('profile')
  const [health, setHealth] = useState<Health | null>(null)
  const [offline, setOffline] = useState(false)

  useEffect(() => {
    getHealth()
      .then(setHealth)
      .catch(() => setOffline(true))
  }, [])

  const availability = new Map(health?.reports.map((r) => [r.name, r.available]) ?? [])
  const current = MODULE_ONE.find((item) => item.key === active) ?? MODULE_ONE[0]

  return (
    <AppShell
      header={{ height: 56 }}
      navbar={{ width: 264, breakpoint: 'sm', collapsed: { mobile: !opened } }}
      padding="lg"
    >
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Group gap="sm">
            <Burger opened={opened} onClick={toggle} hiddenFrom="sm" size="sm" />
            <Title order={1} size="h5" fw={600}>
              CarveOps Copilot
            </Title>
            <Text size="xs" c="dimmed" visibleFrom="sm">
              SAP 并购剥离辅助
            </Text>
          </Group>

          <Group gap="sm">
            {offline ? (
              <Badge size="sm" variant="light" color="red">
                后端未连接
              </Badge>
            ) : health ? (
              <Tooltip label={`${health.service} v${health.version}`}>
                <Badge
                  size="sm"
                  variant="light"
                  style={{ backgroundColor: `${STATUS.good}22`, color: STATUS.good }}
                >
                  后端已连接 · 报告 {health.reports_available}/{health.reports_total}
                </Badge>
              </Tooltip>
            ) : (
              <Badge size="sm" variant="light" color="gray">
                连接中…
              </Badge>
            )}
          </Group>
        </Group>
      </AppShell.Header>

      <AppShell.Navbar p="md">
        <AppShell.Section grow component={ScrollArea}>
          <Text size="xs" c="dimmed" tt="uppercase" fw={500} mb="xs" style={{ letterSpacing: '0.05em' }}>
            模块一 · 数据迁移映射
          </Text>
          <Stack gap={2}>
            {MODULE_ONE.map((item) => {
              const available = availability.get(item.report)
              return (
                <NavLink
                  key={item.key}
                  active={item.key === active}
                  label={item.label}
                  onClick={() => setActive(item.key)}
                  rightSection={
                    available === false ? (
                      <Badge size="xs" variant="outline" color="gray">
                        未生成
                      </Badge>
                    ) : null
                  }
                />
              )
            })}
          </Stack>

          <Text
            size="xs"
            c="dimmed"
            tt="uppercase"
            fw={500}
            mt="lg"
            mb="xs"
            style={{ letterSpacing: '0.05em' }}
          >
            模块二 · Fit-to-Standard
          </Text>
          <NavLink label="Fit/Gap 判定" disabled rightSection={<Badge size="xs" variant="outline" color="gray">第 3 步</Badge>} />
        </AppShell.Section>

        <AppShell.Section>
          <Box pt="sm" style={{ borderTop: '1px solid var(--mantine-color-dark-4)' }}>
            <Text size="xs" c="dimmed">
              全部为合成数据
            </Text>
            <Text size="xs" c="dimmed" mt={2}>
              不接触任何真实 SAP 系统
            </Text>
            {health && (
              <Text size="xs" c="dimmed" mt={6}>
                <Code>{health.version}</Code>
              </Text>
            )}
          </Box>
        </AppShell.Section>
      </AppShell.Navbar>

      <AppShell.Main>
        <Box maw={1180} mx="auto">
          {current.element}
        </Box>
      </AppShell.Main>
    </AppShell>
  )
}
