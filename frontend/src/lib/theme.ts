/**
 * 主题与状态色。
 *
 * 状态色取自 dataviz 参考调色板的 status palette（固定，不随主题变化）。
 * 已用 scripts/validate_palette.py 对照本项目的暗色画布 #171a21 实测：
 *   - 三色与画布的对比度全部 >= 3.0:1        [PASS]
 *   - 色盲区分度最差相邻对 ΔE 16.9 (protan)  [PASS，目标线 12]
 * 校验器另报 `Lightness band FAIL`（warning #fab219 亮度 0.811 超带），
 * 但该检查的适用范围是「仅分类调色板」，且在校验器自己的参考画布 #1a1a19 上
 * 同样复现——warning 是刻意做亮的。不是本项目引入的缺陷。
 *
 * 规矩：状态色必须配文字标签，绝不单靠颜色传意。
 */

import { createTheme, type MantineColorsTuple } from '@mantine/core'

/** dataviz status palette —— 保留色，绝不当作「第 4 个系列」使用。 */
export const STATUS = {
  good: '#0ca30c',
  warning: '#fab219',
  serious: '#ec835a',
  critical: '#d03b3b',
} as const

/** 图表画布。校验器就是拿这个值做的对比度检查。 */
export const CHART_SURFACE = '#171a21'

const brand: MantineColorsTuple = [
  '#eef3ff', '#dce4f5', '#b9c7e2', '#94a8d0', '#748dc1',
  '#5f7cb8', '#5474b4', '#44639f', '#39588f', '#2d4b81',
]

export const theme = createTheme({
  primaryColor: 'brand',
  colors: { brand },
  fontFamily:
    'ui-sans-serif, system-ui, "Segoe UI", "Microsoft YaHei", sans-serif',
  fontFamilyMonospace: 'ui-monospace, "Cascadia Code", Consolas, monospace',
  headings: { fontWeight: '600' },
  defaultRadius: 'md',
})

/** 置信度分档 → 颜色。阈值与 field_mapping.py 的 HIGH/MEDIUM 一致。 */
export function bandColor(band: string): string {
  if (band === 'high') return STATUS.good
  if (band === 'medium') return STATUS.warning
  return STATUS.critical
}

/** 严重程度 → 颜色。 */
export function severityColor(severity: string): string {
  if (severity === 'high') return STATUS.critical
  if (severity === 'medium') return STATUS.warning
  return STATUS.serious
}

/** 映射状态 → 颜色与中文标签。四态互斥，见 field_mapping.py 的 classify_status。 */
export const MAPPING_STATUS: Record<string, { color: string; label: string }> = {
  suggested: { color: STATUS.good, label: '可直接采纳' },
  needs_review: { color: STATUS.warning, label: '需人工确认' },
  possible_false_friend: { color: STATUS.serious, label: '疑似假朋友' },
  no_confident_target: { color: STATUS.critical, label: '无可信落点' },
}

/** 校验裁决 → 颜色与中文标签。semantic_match × loadable 的正交结果。 */
export const VERDICT: Record<string, { color: string; label: string; hint: string }> = {
  loadable_ok: { color: STATUS.good, label: '可加载', hint: '语义正确且目标字段可写入' },
  mapping_ok_but_not_loadable: {
    color: STATUS.warning,
    label: '映射对但不可写',
    hint: '语义正确，但目标字段只读——只能作 lineage / 参考',
  },
  needs_human_decision: { color: STATUS.serious, label: '需人工决策', hint: '映射语义存疑' },
  no_target: { color: STATUS.critical, label: '无落点', hint: '目标 schema 中没有对应字段' },
  no_source: { color: STATUS.critical, label: '无源字段', hint: '目标主键没有任何 legacy 字段映射' },
}
