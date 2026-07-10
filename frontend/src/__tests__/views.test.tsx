/**
 * 渲染冒烟测试。
 *
 * 拿【真实的报告 JSON】喂给 fetch mock，把每个视图完整渲染一遍。
 * 目的是证明组件在真实数据形状下不崩、关键信息真的出现在 DOM 里——
 * 「tsc 通过」只说明类型对得上，不说明运行时不炸。
 *
 * 报告直接从 data/synthetic/ 读，所以字段名一旦漂移，测试立刻红。
 */

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { MantineProvider } from '@mantine/core'
import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ProfileView } from '../views/ProfileView'
import { MappingView } from '../views/MappingView'
import { ValidationView } from '../views/ValidationView'
import { DuplicateView } from '../views/DuplicateView'
import { theme } from '../lib/theme'
import { classify } from '../components/CountryVariantsChart'

const SYNTHETIC = resolve(__dirname, '../../../data/synthetic')

const loadReport = (name: string) =>
  JSON.parse(readFileSync(resolve(SYNTHETIC, `${name}.json`), 'utf-8'))

/** 后端对未生成的报告返回 404 + 结构化 detail。原样复刻，才能测到「未生成」分支。 */
const NOT_GENERATED = {
  detail: {
    error: 'report_not_generated',
    message: '报告 `vendor_duplicate_report` 尚未生成。',
    expected_path: 'data\\synthetic\\vendor_duplicate_report.json',
    generated_by: '（尚未实现：Splink 实体解析组件）',
  },
}

function mockFetch() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      const name = url.split('/api/reports/')[1]
      if (name === 'vendor_duplicate_report') {
        return new Response(JSON.stringify(NOT_GENERATED), { status: 404 })
      }
      return new Response(JSON.stringify(loadReport(name)), { status: 200 })
    }),
  )
}

const renderView = (node: React.ReactNode) =>
  render(
    <MantineProvider theme={theme} forceColorScheme="dark">
      {node}
    </MantineProvider>,
  )

beforeEach(mockFetch)
afterEach(() => vi.unstubAllGlobals())

describe('数据质量画像', () => {
  it('渲染摘要、质量问题与 country 的 14 种写法', async () => {
    const report = loadReport('vendor_profile_report')
    renderView(<ProfileView />)

    await waitFor(() => expect(screen.getByText('数据质量画像')).toBeDefined())

    // 摘要数字来自报告，不是硬编码。用 getAllByText——同一个数字会在摘要卡和表格里各出现一次。
    expect(screen.getAllByText(String(report._meta.record_count)).length).toBeGreaterThan(0)
    expect(screen.getAllByText(String(report.quality_flags.length)).length).toBeGreaterThan(0)

    // 三态图例都在。
    // 注意：条形与 Y 轴刻度断言不到——recharts 的 ResponsiveContainer 在无头 DOM 里
    // 宽高为 0，不渲染任何 mark。图例在容器之外，所以能测。
    // 分类逻辑本身由下面的纯函数测试覆盖。
    expect(screen.getByText('合法 ISO 代码')).toBeDefined()
    expect(screen.getByText('长度合格但值非法')).toBeDefined()
    expect(screen.getByText('非法且长度溢出')).toBeDefined()

    // 自由文本字段标注为「不适用」，不是 0 种签名
    expect(screen.getAllByText('自由文本 · 不适用').length).toBeGreaterThan(0)
  })
})

describe('字段映射建议', () => {
  it('四种状态、置信度与 gaps 都渲染出来', async () => {
    const report = loadReport('vendor_field_mapping')
    renderView(<MappingView />)

    await waitFor(() => expect(screen.getByText('字段映射建议')).toBeDefined())

    // 每个 legacy 字段一行
    for (const mapping of report.mappings) {
      expect(screen.getAllByText(mapping.legacy_field).length).toBeGreaterThan(0)
    }

    // 四种状态的中文标签
    expect(screen.getAllByText('可直接采纳').length).toBeGreaterThan(0)
    expect(screen.getAllByText('需人工确认').length).toBeGreaterThan(0)
    expect(screen.getAllByText('疑似假朋友').length).toBeGreaterThan(0)
    expect(screen.getAllByText('无可信落点').length).toBeGreaterThan(0)

    // gaps 的消息原样展示
    for (const gap of report.gaps) {
      expect(screen.getByText(gap.message)).toBeDefined()
    }
  })
})

describe('迁移前校验', () => {
  it('正交拆分、问题类型与未核实免责都渲染出来', async () => {
    const report = loadReport('vendor_validation_report')
    renderView(<ValidationView />)

    await waitFor(() => expect(screen.getByText('迁移前校验')).toBeDefined())

    // 正交说明原样展示
    expect(screen.getByText(report._meta.orthogonality_note_zh)).toBeDefined()

    // 「映射对但不可写」这一节存在（created_date -> CreationDate）
    expect(screen.getByText('映射正确，但不可作为加载目标')).toBeDefined()
    expect(screen.getAllByText('created_date').length).toBeGreaterThan(0)

    // 每个问题类型都有一行
    for (const issueType of Object.keys(report.summary.by_issue_type)) {
      expect(screen.getAllByText(issueType).length).toBeGreaterThan(0)
    }

    // 延后的检查
    for (const deferred of report.deferred_checks) {
      expect(screen.getByText(deferred.check)).toBeDefined()
    }
  })
})

describe('country 三态分类（纯函数）', () => {
  const distribution: { value: string; count: number }[] =
    loadReport('vendor_profile_report').fields.country.value_distribution

  it('把 14 种写法正确切成合法 / 长度合格但非法 / 非法且溢出', () => {
    const grouped = distribution.reduce<Record<string, string[]>>((acc, item) => {
      ;(acc[classify(item.value)] ??= []).push(item.value)
      return acc
    }, {})

    expect(grouped.conforming.sort()).toEqual(['DE', 'JP', 'US'])
    expect(grouped.fits_but_invalid.sort()).toEqual(['GER', 'JPN', 'USA', 'de', 'us'])
    expect(grouped.invalid_and_overflows.sort()).toEqual([
      'Deutschland',
      'Germany',
      'JAPAN',
      'Japan',
      'U.S.A.',
      'United States',
    ])
  })

  it('「长度合格但非法」这一档非空 —— 它就是长度校验与归一化校验正交的证据', () => {
    const fits = distribution.filter((d) => classify(d.value) === 'fits_but_invalid')
    expect(fits.length).toBeGreaterThan(0)
    // 这些值长度 <= 3，塞得进 Country 的 CHAR(3)，只做长度校验会放它们过去
    for (const item of fits) expect(item.value.length).toBeLessThanOrEqual(3)
  })

  it('溢出的记录条数与迁移前校验报告的 max_length_overflow 对得上', () => {
    const overflowRecords = distribution
      .filter((d) => classify(d.value) === 'invalid_and_overflows')
      .reduce((sum, d) => sum + d.count, 0)

    const validation = loadReport('vendor_validation_report')
    expect(overflowRecords).toBe(validation.summary.by_issue_type.max_length_overflow)
  })
})

describe('实体解析占位视图', () => {
  it('把后端的「未生成」原样呈现，而不是报错', async () => {
    renderView(<DuplicateView />)

    await waitFor(() => expect(screen.getByText('该报告尚未生成')).toBeDefined())
    expect(screen.getByText(NOT_GENERATED.detail.generated_by)).toBeDefined()
    expect(screen.queryByText('读取失败')).toBeNull()
  })
})
