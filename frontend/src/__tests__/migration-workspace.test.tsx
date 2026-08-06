import { MantineProvider } from '@mantine/core'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import App from '../App'
import {
  canonicalReviewState,
  countApprovedLinks,
  countUniqueApprovedSources,
  decisionsToReviewState,
  findTargetConflict,
  reviewStateToDecisions,
  type MigrationDecision,
  type MigrationMapping,
  type MigrationWorkspaceDetail,
} from '../lib/migrationWorkspace'
import { theme } from '../lib/theme'
import { MigrationWorkspaceView } from '../views/MigrationWorkspaceView'

const sources = [
  'article_number',
  'catalogue_caption',
  'merchandise_family',
  'inventory_measure',
  'lifecycle_block',
  'tariff_name',
  'retail_amount',
  'effective_start',
  'effective_end',
  'data_steward',
]

const candidates: Record<string, string[]> = {
  article_number: ['item.item_name', 'item.item_code', 'item_price.item_code'],
  catalogue_caption: ['item_price.price_list', 'item.item_name', 'item.stock_uom'],
  merchandise_family: ['item.item_name', 'item.item_group', 'item.item_code'],
  inventory_measure: ['item.stock_uom', 'item_price.uom', 'item_price.price_list_rate'],
  lifecycle_block: ['item.disabled', 'item.item_code', 'item_price.valid_upto'],
  tariff_name: ['item_price.price_list', 'item.item_name', 'item_price.item_code'],
  retail_amount: ['item_price.price_list_rate', 'item_price.valid_from', 'item_price.valid_upto'],
  effective_start: ['item_price.valid_from', 'item.disabled', 'item.stock_uom'],
  effective_end: ['item_price.valid_upto', 'item_price.valid_from', 'item.item_code'],
  data_steward: ['item_price.valid_from', 'item.item_name', 'item_price.valid_upto'],
}

const seedDecisions: MigrationDecision[] = [
  ['article_number', 'item.item_code'],
  ['article_number', 'item_price.item_code'],
  ['catalogue_caption', 'item.item_name'],
  ['merchandise_family', 'item.item_group'],
  ['inventory_measure', 'item.stock_uom'],
  ['inventory_measure', 'item_price.uom'],
  ['lifecycle_block', 'item.disabled'],
  ['tariff_name', 'item_price.price_list'],
  ['retail_amount', 'item_price.price_list_rate'],
  ['effective_start', 'item_price.valid_from'],
  ['effective_end', 'item_price.valid_upto'],
].map(([source_field, target]) => ({
  source_field,
  target,
  decision: 'approved',
  reason: null,
  transformation: { type: 'copy' },
}))
seedDecisions.push({
  source_field: 'data_steward',
  target: null,
  decision: 'rejected',
  reason: 'No target field exists in this contract after human domain review.',
  transformation: { type: 'copy' },
})

function mapping(source: string): MigrationMapping {
  return {
    source_field: source,
    status: source === 'inventory_measure' || source === 'tariff_name' ? 'needs_review' : 'no_confident_target',
    recommendation: null,
    confidence: 0,
    band: 'low',
    mapping_basis: 'none',
    source_profile: {
      inferred_kind: 'string',
      row_count: 8,
      present_count: 8,
      missing_count: 0,
      missing_ratio: 0,
      distinct_count: source === 'inventory_measure' ? 3 : 8,
      distinct_ratio: 1,
      observed_max_length: 16,
      samples: source === 'article_number' ? ['ART-3001', 'ART-3002'] : ['sample-a', 'sample-b'],
    },
    review_reasons: ['best_score_below_threshold'],
    top_candidates: candidates[source].map((target, index) => ({
      target,
      target_resource: target.split('.')[0],
      target_field: target.split('.')[1],
      rank: index + 1,
      score: 0.3 - index * 0.01,
      semantic_score: 0.2,
      fuzzy_score: 0.4,
      alias_hit: false,
      alias_source: null,
      lexical_overlap: 0,
      type_gate: 1,
      warnings: [],
    })),
  }
}

function detail(decisions = seedDecisions, build = false): MigrationWorkspaceDetail {
  const state = decisionsToReviewState(decisions)
  return {
    workspace: {
      workspace_id: 'erpnext-item-price',
      title: 'ERPNext Item + Item Price',
      description: 'Human-approved ERPNext Item and Item Price mapping review workspace.',
      contract_id: 'erpnext-item-price-reference-v1',
      contract_version: '1.0.0',
      contract_sha256: '0'.repeat(64),
      domain: 'product_and_pricing',
      source_path: 'data/examples/blind/erpnext_item_price/source_product_catalog.csv',
      source_sha256: '1'.repeat(64),
      mapping_content_sha256: '99007ad5da580b6e764b01e3a9739840bcfcff1b1a16c29cf708124ebbc56703',
      mapping_report_sha256: '2'.repeat(64),
      decision_source: decisions === seedDecisions ? 'seed' : 'runtime',
      decision_sha256: decisions === seedDecisions ? 'seedsha' : 'runtimesha',
      runtime_state: decisions !== seedDecisions,
    },
    summary: {
      source_rows: 8,
      source_fields: 10,
      target_fields: 11,
      approved_links: countApprovedLinks(state),
      unique_approved_sources: countUniqueApprovedSources(state),
      rejected_sources: Object.values(state).filter((item) => item.mode === 'rejected').length,
      deferred_sources: Object.values(state).filter((item) => item.mode === 'deferred').length,
      multi_target_sources: Object.values(state).filter((item) => item.targets.length > 1).length,
    },
    mappings: sources.map(mapping),
    decisions,
    build: build
      ? {
          available: true,
          summary: {
            build_status: 'completed',
            resources_generated: 2,
            rows_generated: 16,
            rejected_rows: 0,
            lineage_entries: 88,
          },
          validation: { valid: true, finding_count: 0 },
          manifest: { content_sha256: '5c8f6d523a60887ce2b0173e3a89cae94cf484f0212b9cc247c7bd56738d0dfe', resource_count: 2 },
          build_report_sha256: '02e79b6cf55d898475fd145107da94204cbebed9ddb79d141659cb64862b7af9',
        }
      : { available: false },
    resources: [
      { name: 'item', fields: ['item_code', 'item_name', 'item_group', 'stock_uom', 'disabled'] },
      { name: 'item_price', fields: ['item_code', 'uom', 'price_list', 'price_list_rate', 'valid_from', 'valid_upto'] },
    ],
  }
}

const itemPreview = {
  resource: 'item',
  available: true,
  columns: ['item_code', 'item_name', 'item_group', 'stock_uom', 'disabled'],
  rows: [{ item_code: 'ART-3001', item_name: 'Synthetic Atlas Gear', item_group: 'Components', stock_uom: 'Nos', disabled: 'false' }],
  total_rows: 8,
  returned_rows: 8,
  content_sha256: '3'.repeat(64),
}

const itemPricePreview = {
  resource: 'item_price',
  available: true,
  columns: ['item_code', 'uom', 'price_list', 'price_list_rate', 'valid_from', 'valid_upto'],
  rows: [{ item_code: 'ART-3001', uom: 'Nos', price_list: 'Standard Selling', price_list_rate: '129.50', valid_from: '2026-01-01', valid_upto: '2026-12-31' }],
  total_rows: 8,
  returned_rows: 8,
  content_sha256: '4'.repeat(64),
}

function lineage(source = '') {
  const entries = Array.from({ length: source ? 16 : 88 }, (_, index) => ({
    source_row_number: (index % 8) + 1,
    source_record_id: `ART-300${(index % 8) + 1}`,
    source_field: source || (index % 2 === 0 ? 'article_number' : 'inventory_measure'),
    source_value_sha256: 'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789',
    target_resource: index % 2 === 0 ? 'item' : 'item_price',
    target_row_number: (index % 8) + 1,
    target_field: index % 2 === 0 ? 'item_code' : 'uom',
    transformation_type: 'copy',
    status: 'written',
  }))
  return { available: true, total_entries: 88, matched_entries: entries.length, returned_entries: entries.length, entries }
}

let currentDetail: MigrationWorkspaceDetail
let lastPutPayload: unknown
let lastPostPayload: unknown

function installFetch(status?: 'stale' | 'invalid') {
  currentDetail = detail()
  lastPutPayload = null
  lastPostPayload = null
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const path = url.replace('http://127.0.0.1:8000', '')
      if (path === '/api/health') {
        return new Response(JSON.stringify({ status: 'ok', service: 'api', version: '0.2.0', reports_available: 10, reports_total: 10, reports: [] }), { status: 200 })
      }
      if (path === '/api/migration/workspaces/erpnext-item-price' && (!init || init.method === 'GET')) {
        return new Response(JSON.stringify(currentDetail), { status: 200 })
      }
      if (path.endsWith('/decisions') && init?.method === 'PUT') {
        lastPutPayload = JSON.parse(String(init.body))
        if (status === 'stale') {
          return new Response(JSON.stringify({ detail: { error: 'stale_decision', message: 'Decision state changed.' } }), { status: 409 })
        }
        if (status === 'invalid') {
          return new Response(JSON.stringify({ detail: { error: 'invalid_decisions', decision_error: { code: 'duplicate_approved_target' }, message: 'bad' } }), { status: 422 })
        }
        currentDetail = detail((lastPutPayload as { decisions: MigrationDecision[] }).decisions)
        return new Response(JSON.stringify(currentDetail), { status: 200 })
      }
      if (path.endsWith('/build') && init?.method === 'POST') {
        lastPostPayload = JSON.parse(String(init.body))
        currentDetail = detail(currentDetail.decisions, true)
        return new Response(JSON.stringify(currentDetail), { status: 200 })
      }
      if (path.endsWith('/reset') && init?.method === 'POST') {
        currentDetail = detail()
        return new Response(JSON.stringify(currentDetail), { status: 200 })
      }
      if (path.includes('/resources/item_price')) return new Response(JSON.stringify(itemPricePreview), { status: 200 })
      if (path.includes('/resources/item')) return new Response(JSON.stringify(itemPreview), { status: 200 })
      if (path.includes('/lineage')) {
        const parsed = new URL(url)
        return new Response(JSON.stringify(lineage(parsed.searchParams.get('source_field') ?? '')), { status: 200 })
      }
      return new Response(JSON.stringify({ detail: { error: 'not_mocked', message: path } }), { status: 404 })
    }),
  )
}

function renderView(node = <MigrationWorkspaceView />) {
  return render(
    <MantineProvider theme={theme} forceColorScheme="dark">
      {node}
    </MantineProvider>,
  )
}

function checkbox(label: string) {
  return screen.getByLabelText(label) as HTMLInputElement
}

function button(label: string) {
  return screen.getByText(label).closest('button') as HTMLButtonElement
}

function openSource(source: string) {
  fireEvent.click(screen.getAllByText(source)[0])
}

function segmented(label: string, source: string) {
  return screen.getAllByText(label)[sources.indexOf(source)]
}

beforeEach(() => {
  installFetch()
  Object.defineProperty(window, 'confirm', {
    configurable: true,
    value: vi.fn(() => true),
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('migration workspace pure functions', () => {
  it('converts decisions into a canonical review state', () => {
    const state = decisionsToReviewState(seedDecisions)
    expect(state.article_number.targets).toEqual(['item.item_code', 'item_price.item_code'])
    expect(state.inventory_measure.targets).toEqual(['item.stock_uom', 'item_price.uom'])
    expect(state.data_steward.mode).toBe('rejected')
  })

  it('converts review state back to multi-target decisions', () => {
    const decisions = reviewStateToDecisions(decisionsToReviewState(seedDecisions))
    expect(decisions.filter((item) => item.source_field === 'article_number')).toHaveLength(2)
    expect(decisions.filter((item) => item.source_field === 'inventory_measure')).toHaveLength(2)
  })

  it('counts approved links and unique approved sources', () => {
    const state = decisionsToReviewState(seedDecisions)
    expect(countApprovedLinks(state)).toBe(11)
    expect(countUniqueApprovedSources(state)).toBe(9)
  })

  it('detects target conflicts across sources', () => {
    const state = decisionsToReviewState(seedDecisions)
    expect(findTargetConflict(state, 'article_number', 'item.item_name')).toBe('catalogue_caption')
  })

  it('canonicalizes target order and removes duplicates', () => {
    const state = canonicalReviewState({ source: { mode: 'approved', targets: ['b', 'a', 'a'], reason: '' } })
    expect(state.source.targets).toEqual(['a', 'b'])
  })
})

describe('MigrationWorkspaceView', () => {
  it('renders the workspace summary', async () => {
    renderView()
    await waitFor(() => expect(screen.getByText('迁移工作台')).toBeDefined())
    expect(screen.getByText('ERPNext Item + Item Price · Human-in-the-loop Review')).toBeDefined()
    expect(screen.getAllByText('8').length).toBeGreaterThan(0)
    expect(screen.getAllByText('11').length).toBeGreaterThan(0)
    expect(screen.getByText('Seed')).toBeDefined()
  })

  it('renders ten source fields', async () => {
    renderView()
    await waitFor(() => expect(screen.getAllByText('article_number').length).toBeGreaterThan(0))
    for (const source of sources) expect(screen.getAllByText(source).length).toBeGreaterThan(0)
  })

  it('shows two selected article number targets', async () => {
    renderView()
    await waitFor(() => expect(screen.getAllByText('article_number').length).toBeGreaterThan(0))
    openSource('article_number')
    expect(checkbox('article_number item.item_code').checked).toBe(true)
    expect(checkbox('article_number item_price.item_code').checked).toBe(true)
  })

  it('shows two selected inventory measure targets', async () => {
    renderView()
    await waitFor(() => expect(screen.getAllByText('inventory_measure').length).toBeGreaterThan(0))
    openSource('inventory_measure')
    expect(checkbox('inventory_measure item.stock_uom').checked).toBe(true)
    expect(checkbox('inventory_measure item_price.uom').checked).toBe(true)
  })

  it('only renders top-3 candidates for a source', async () => {
    renderView()
    await waitFor(() => expect(screen.getAllByText('article_number').length).toBeGreaterThan(0))
    openSource('article_number')
    expect(checkbox('article_number item.item_name')).toBeDefined()
    expect(checkbox('article_number item.item_code')).toBeDefined()
    expect(checkbox('article_number item_price.item_code')).toBeDefined()
    expect(screen.queryByLabelText('article_number item_price.price_list_rate')).toBeNull()
  })

  it('prevents selecting a target already approved by another source', async () => {
    renderView()
    await waitFor(() => expect(screen.getAllByText('article_number').length).toBeGreaterThan(0))
    openSource('article_number')
    expect(checkbox('article_number item.item_name').disabled).toBe(true)
    expect(screen.getAllByText('conflict: catalogue_caption').length).toBeGreaterThan(0)
  })

  it('reject mode clears selected targets', async () => {
    renderView()
    await waitFor(() => expect(screen.getAllByText('article_number').length).toBeGreaterThan(0))
    openSource('article_number')
    fireEvent.click(segmented('Reject: no target', 'article_number'))
    expect(checkbox('article_number item.item_code').checked).toBe(false)
    expect(button('保存审批').disabled).toBe(false)
  })

  it('defer mode clears selected targets', async () => {
    renderView()
    await waitFor(() => expect(screen.getAllByText('inventory_measure').length).toBeGreaterThan(0))
    openSource('inventory_measure')
    fireEvent.click(segmented('Defer review', 'inventory_measure'))
    expect(checkbox('inventory_measure item.stock_uom').checked).toBe(false)
  })

  it('does not allow saving approved source with no targets', async () => {
    renderView()
    await waitFor(() => expect(screen.getAllByText('data_steward').length).toBeGreaterThan(0))
    openSource('data_steward')
    fireEvent.click(segmented('Approve selected targets', 'data_steward'))
    expect(button('保存审批').disabled).toBe(true)
  })

  it('sends the correct PUT payload', async () => {
    renderView()
    await waitFor(() => expect(screen.getAllByText('article_number').length).toBeGreaterThan(0))
    openSource('article_number')
    fireEvent.click(screen.getByLabelText('article_number item_price.item_code'))
    fireEvent.click(screen.getByText('保存审批'))
    await waitFor(() => expect(lastPutPayload).toBeTruthy())
    expect((lastPutPayload as { expected_mapping_content_sha256: string }).expected_mapping_content_sha256).toBe(currentDetail.workspace.mapping_content_sha256)
  })

  it('preserves multi-target decisions in save payload', async () => {
    renderView()
    await waitFor(() => expect(screen.getAllByText('inventory_measure').length).toBeGreaterThan(0))
    fireEvent.click(screen.getByText('保存审批'))
    expect(lastPutPayload).toBeNull()
    openSource('inventory_measure')
    fireEvent.click(screen.getByLabelText('inventory_measure item.stock_uom'))
    fireEvent.click(screen.getByText('保存审批'))
    await waitFor(() => expect(lastPutPayload).toBeTruthy())
    const payload = lastPutPayload as { decisions: MigrationDecision[] }
    expect(payload.decisions.filter((item) => item.source_field === 'article_number')).toHaveLength(2)
  })

  it('disables build while unsaved changes exist', async () => {
    renderView()
    await waitFor(() => expect(screen.getAllByText('article_number').length).toBeGreaterThan(0))
    openSource('article_number')
    fireEvent.click(screen.getByLabelText('article_number item_price.item_code'))
    expect(button('生成迁移包').disabled).toBe(true)
  })

  it('enables build after save', async () => {
    renderView()
    await waitFor(() => expect(screen.getAllByText('article_number').length).toBeGreaterThan(0))
    openSource('article_number')
    fireEvent.click(screen.getByLabelText('article_number item_price.item_code'))
    fireEvent.click(screen.getByText('保存审批'))
    await waitFor(() => expect(button('生成迁移包').disabled).toBe(false))
  })

  it('sends the correct POST build payload', async () => {
    renderView()
    await waitFor(() => expect(button('生成迁移包').disabled).toBe(false))
    fireEvent.click(screen.getByText('生成迁移包'))
    await waitFor(() => expect(lastPostPayload).toBeTruthy())
    expect((lastPostPayload as { expected_decision_sha256: string }).expected_decision_sha256).toBe('seedsha')
  })

  it('shows validation valid state after build', async () => {
    renderView()
    await waitFor(() => expect(button('生成迁移包').disabled).toBe(false))
    fireEvent.click(screen.getByText('生成迁移包'))
    await waitFor(() => expect(screen.getByText('valid')).toBeDefined())
    expect(screen.getByText('Validation Findings')).toBeDefined()
  })

  it('shows item preview dimensions', async () => {
    currentDetail = detail(seedDecisions, true)
    renderView()
    await waitFor(() => expect(screen.getByText('8 rows · 5 columns · 3333333333...333333')).toBeDefined())
  })

  it('shows item price preview dimensions', async () => {
    currentDetail = detail(seedDecisions, true)
    renderView()
    await waitFor(() => expect(screen.getByText('Item Price')).toBeDefined())
    fireEvent.click(screen.getByText('Item Price'))
    expect(screen.getByText('8 rows · 6 columns · 4444444444...444444')).toBeDefined()
  })

  it('shows article number lineage count', async () => {
    currentDetail = detail(seedDecisions, true)
    renderView()
    await waitFor(() => expect(screen.getByLabelText('Source Field Filter')).toBeDefined())
    fireEvent.change(screen.getByLabelText('Source Field Filter'), { target: { value: 'article_number' } })
    await waitFor(() => expect(screen.getByText('16 matched · 88 total')).toBeDefined())
  })

  it('reset restores seed state', async () => {
    currentDetail = detail(reviewStateToDecisions({ article_number: { mode: 'approved', targets: ['item.item_code'], reason: '' } }))
    renderView()
    await waitFor(() => expect(screen.getByText('Runtime')).toBeDefined())
    fireEvent.click(screen.getByText('重置本地状态'))
    await waitFor(() => expect(screen.getByText('Seed')).toBeDefined())
  })

  it('shows stale decision error', async () => {
    installFetch('stale')
    renderView()
    await waitFor(() => expect(screen.getAllByText('article_number').length).toBeGreaterThan(0))
    openSource('article_number')
    fireEvent.click(screen.getByLabelText('article_number item_price.item_code'))
    fireEvent.click(screen.getByText('保存审批'))
    await waitFor(() => expect(screen.getByText(/stale_decision/)).toBeDefined())
  })

  it('shows decision loader error code', async () => {
    installFetch('invalid')
    renderView()
    await waitFor(() => expect(screen.getAllByText('article_number').length).toBeGreaterThan(0))
    openSource('article_number')
    fireEvent.click(screen.getByLabelText('article_number item_price.item_code'))
    fireEvent.click(screen.getByText('保存审批'))
    await waitFor(() => expect(screen.getByText(/duplicate_approved_target/)).toBeDefined())
  })

  it('does not render ground truth or expected targets', async () => {
    renderView()
    await waitFor(() => expect(screen.getByText('迁移工作台')).toBeDefined())
    expect(document.body.textContent).not.toContain('ground_truth')
    expect(document.body.textContent).not.toContain('expected_targets')
  })

  it('does not render full source values in lineage', async () => {
    currentDetail = detail(seedDecisions, true)
    renderView()
    await waitFor(() => expect(screen.getAllByText('abcdef01...456789').length).toBeGreaterThan(0))
    expect(document.body.textContent).not.toContain('source_value')
    expect(document.body.textContent).not.toContain('ART-3001 raw')
  })
})

describe('App navigation', () => {
  it('opens the migration workspace by default', async () => {
    renderView(<App />)
    await waitFor(() => expect(screen.getByText('迁移工作台')).toBeDefined())
    expect(screen.getByText('企业迁移准备与 Cutover 治理')).toBeDefined()
  })
})
