import { MantineProvider } from '@mantine/core'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import App from '../App'
import type {
  MappingContractCatalog,
  MappingJobResponse,
  MappingReviewPayload,
  MappingReviewSummary,
  MappingScorer,
} from '../lib/mappingJobs'
import { theme } from '../lib/theme'
import { MappingJobView } from '../views/MappingJobView'

const sentinel = 'RAW-SENTINEL-VALUE'

const catalog: MappingContractCatalog = {
  contracts: [
    {
      contract_id: 'generic-customer',
      title: 'Generic Customer Migration Contract',
      domain: 'customer_master',
      version: '1.0.0',
      target_resource_count: 2,
      target_field_count: 12,
      target_fields: [
        'customer.customer_id',
        'customer.customer_name',
        'customer.country',
        'customer.email',
        'customer.phone',
        'customer.tax_number',
        'customer.payment_terms',
        'customer_bank.bank_id',
        'customer_bank.customer_id',
        'customer_bank.routing_number',
        'customer_bank.iban',
        'customer_bank.currency',
      ],
      supported_scorers: ['baseline', 'precision_tiered_v4', 'precision_tiered_v5'],
    },
    {
      contract_id: 'supplier-reference',
      title: 'SAP Supplier Reference Migration Contract',
      domain: 'supplier_master',
      version: '1.0.0',
      target_resource_count: 2,
      target_field_count: 11,
      target_fields: [
        'supplier_general.supplier_id',
        'supplier_general.organization_name',
        'supplier_general.business_partner_category',
        'supplier_general.country_code',
        'supplier_general.language_code',
        'supplier_general.tax_number',
        'supplier_company.assignment_id',
        'supplier_company.supplier_id',
        'supplier_company.company_code',
        'supplier_company.reconciliation_account',
        'supplier_company.payment_terms',
      ],
      supported_scorers: ['baseline', 'precision_tiered_v4', 'precision_tiered_v5'],
    },
    {
      contract_id: 'erpnext-item-price',
      title: 'ERPNext Item and Item Price Reference Contract',
      domain: 'product_and_pricing',
      version: '1.0.0',
      target_resource_count: 2,
      target_field_count: 11,
      target_fields: [
        'item.item_code',
        'item.item_name',
        'item.item_group',
        'item.stock_uom',
        'item.disabled',
        'item_price.item_code',
        'item_price.uom',
        'item_price.price_list',
        'item_price.price_list_rate',
        'item_price.valid_from',
        'item_price.valid_upto',
      ],
      supported_scorers: ['baseline', 'precision_tiered_v4', 'precision_tiered_v5'],
    },
  ],
}

function mappingJob(filename = 'customers.csv', scorer: MappingScorer = 'precision_tiered_v4'): MappingJobResponse {
  const job: MappingJobResponse = {
    job: {
      schema: 'carveops.mapping_job',
      version: '1.0.0',
      job_id: '1234567890abcdef1234567890abcdef',
      status: 'completed',
      original_filename: filename,
      contract_registry_id: 'generic-customer',
      contract: {
        contract_id: 'generic-customer-v1',
        title: 'Generic Customer Migration Contract',
        domain: 'customer_master',
        version: '1.0.0',
        contract_sha256: 'c'.repeat(64),
        target_resource_count: 2,
        target_field_count: 12,
      },
      scorer,
      source: {
        path: 'data/runtime/mapping_jobs/1234567890abcdef1234567890abcdef/source.csv',
        sha256: 's'.repeat(64),
        hash_mode: 'raw_file_bytes_sha256',
        row_count: 2,
        field_count: 2,
      },
      mapping_report: {
        path: 'data/runtime/mapping_jobs/1234567890abcdef1234567890abcdef/mapping_report.json',
        content_sha256: 'm'.repeat(64),
      },
    },
    summary: {
      suggested: 1,
      needs_review: 1,
      possible_false_friend: 0,
      no_confident_target: 1,
      target_coverage: 0.25,
    },
    mappings: [
      {
        source_field: 'routing_number',
        status: 'suggested',
        recommendation: 'customer_bank.routing_number',
        confidence: 0.91,
        band: 'high',
        mapping_basis: 'precision_tiered_v4',
        review_reasons: ['top_candidate_clear'],
        source_profile: {
          name: 'routing_number',
          inferred_kind: 'identifier',
          row_count: 2,
          present_count: 2,
          missing_count: 0,
          missing_ratio: 0,
          distinct_count: 2,
          distinct_ratio: 1,
          observed_min_length: 9,
          observed_mean_length: 9,
          observed_max_length: 9,
        },
        top_candidates: [
          {
            target: 'customer_bank.routing_number',
            rank: 1,
            score: 0.91,
            semantic_score: 0.72,
            fuzzy_score: 0.88,
            alias_hit: true,
            lexical_overlap: ['routing'],
            type_gate: 1,
            value_pattern_evidence: { pattern: 'nine_digit_identifier' },
            resource_context_evidence: { resource: 'customer_bank' },
            activated_interactions: ['routing_to_routing'],
            interaction_evidence: { routing_to_routing: 'leaf match' },
            diagnostic_bonus: 0.12,
            supportive_bonus: 0.04,
            top1_selection_reason: 'precision tier and interaction support',
            warnings: [],
          },
          {
            target: 'customer.tax_number',
            rank: 2,
            score: 0.44,
            semantic_score: 0.4,
            fuzzy_score: 0.36,
            alias_hit: false,
            lexical_overlap: [],
            type_gate: 1,
            warnings: ['review required'],
          },
          {
            target: 'customer.payment_terms',
            rank: 3,
            score: 0.31,
            semantic_score: 0.28,
            fuzzy_score: 0.22,
            alias_hit: false,
            lexical_overlap: [],
            type_gate: 0.8,
            warnings: [],
          },
        ],
      },
      {
        source_field: 'notes',
        status: 'no_confident_target',
        recommendation: null,
        confidence: 0.12,
        band: 'low',
        mapping_basis: 'none',
        review_reasons: ['best_score_below_threshold'],
        source_profile: { inferred_kind: 'text', missing_ratio: 0, distinct_ratio: 1, observed_max_length: 24 },
        top_candidates: [],
      },
    ],
  }
  if (scorer === 'precision_tiered_v5') {
    const candidate = job.mappings[0].top_candidates?.[0]
    if (candidate) {
      candidate.v4_score = 0.73
      candidate.identifier_bonus = 0.14
      candidate.identifier_adjusted_score = 0.87
      candidate.v5_top1_eligible = true
      candidate.v5_top1_selection_reason = 'identifier_adjusted_score_strictly_exceeded_v4_top1'
      candidate.identifier_interaction_evidence = [
        {
          interaction_id: 'entity_identifier_support',
          tier: 'entity_identifier',
          source_concepts: ['client', 'identifier'],
          target_concepts: ['customer', 'identifier'],
          matched_entity_concepts: ['customer'],
          bonus_weight: 0.4,
          bonus: 0.14,
          may_displace_v4_top1: true,
        },
      ]
    }
  }
  return job
}

function mappingJobWithClientNumber(): MappingJobResponse {
  const job = mappingJob('client-number.csv', 'precision_tiered_v5')
  job.mappings[0] = {
    ...job.mappings[0],
    source_field: 'client_number',
    recommendation: 'customer_bank.routing_number',
    top_candidates: job.mappings[0].top_candidates?.map((candidate) => ({ ...candidate })),
  }
  return job
}

let postPayload: unknown
let reviewPayload: MappingReviewPayload | null = null
let postCalls = 0
let getJobCalls = 0
let reviewCalls = 0
let exportCalls: string[] = []
let pendingPost: { resolve: (value: Response) => void } | null = null
let pendingReview: { resolve: (value: Response) => void } | null = null
let localSetItem: ReturnType<typeof vi.fn>
let sessionSetItem: ReturnType<typeof vi.fn>
let createObjectURL: ReturnType<typeof vi.fn>
let revokeObjectURL: ReturnType<typeof vi.fn>

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status })
}

function reviewSummary(payload: MappingReviewPayload): MappingReviewSummary {
  const decisions = payload.decisions
  return {
    mapping_report_sha256: payload.mapping_report_sha256,
    reviewed_fields: decisions.length,
    total_fields: 2,
    pending_fields: 2 - decisions.length,
    accepted_count: decisions.filter((decision) => decision.action === 'accept_suggestion').length,
    overridden_count: decisions.filter((decision) => decision.action === 'select_target').length,
    unmapped_count: decisions.filter((decision) => decision.action === 'mark_unmapped').length,
    export_ready: decisions.length === 2,
    updated_at: '2026-08-18T08:00:00Z',
    decisions,
  }
}

function mappingJobWithReview() {
  const job = mappingJob('loaded.csv')
  job.review = reviewSummary({
    mapping_report_sha256: 'm'.repeat(64),
    decisions: [
      {
        source_field: 'routing_number',
        action: 'select_target',
        target_fields: ['customer.customer_id', 'customer_bank.routing_number'],
        note: 'saved multi',
      },
      { source_field: 'notes', action: 'mark_unmapped' },
    ],
  })
  return job
}

function mappingJobWithV5Review() {
  const job = mappingJob('loaded-v5.csv', 'precision_tiered_v5')
  job.review = reviewSummary({
    mapping_report_sha256: 'm'.repeat(64),
    decisions: [
      {
        source_field: 'routing_number',
        action: 'accept_suggestion',
      },
      { source_field: 'notes', action: 'mark_unmapped' },
    ],
  })
  return job
}

function installFetch(
  mode:
    | 'ok'
    | 'pending'
    | 'error'
    | 'reviewPending'
    | 'reviewError'
    | 'downloadError'
    | 'clientNumber'
    | 'existingV5'
    | 'contractsPending' = 'ok',
) {
  postPayload = null
  reviewPayload = null
  postCalls = 0
  getJobCalls = 0
  reviewCalls = 0
  exportCalls = []
  pendingPost = null
  pendingReview = null
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init?: RequestInit) => {
      const path = url.replace('http://127.0.0.1:8000', '')
      if (path === '/api/health') {
        return Promise.resolve(response({ status: 'ok', service: 'api', version: '0.2.0', reports_available: 10, reports_total: 10, reports: [] }))
      }
      if (path === '/api/mapping/contracts') {
        if (mode === 'contractsPending') return new Promise<Response>(() => undefined)
        return Promise.resolve(response(catalog))
      }
      if (path === '/api/mapping/jobs' && init?.method === 'POST') {
        postCalls += 1
        postPayload = JSON.parse(String(init.body))
        if (mode === 'error') {
          return Promise.resolve(response({ detail: { error: 'mapping_model_unavailable', message: 'Local model unavailable.' } }, 503))
        }
        if (mode === 'pending') {
          return new Promise<Response>((resolve) => {
            pendingPost = { resolve }
          })
        }
        if (mode === 'clientNumber') return Promise.resolve(response(mappingJobWithClientNumber(), 201))
        const scorer = (postPayload as { scorer?: MappingScorer } | null)?.scorer ?? 'precision_tiered_v5'
        return Promise.resolve(response(mappingJob('customers.csv', scorer), 201))
      }
      if (path === '/api/mapping/jobs/1234567890abcdef1234567890abcdef/review' && init?.method === 'PUT') {
        reviewCalls += 1
        reviewPayload = JSON.parse(String(init.body))
        if (mode === 'reviewError') {
          return Promise.resolve(response({ detail: { error: 'mapping_review_stale', message: 'Mapping report SHA does not match the current job.' } }, 409))
        }
        if (mode === 'reviewPending') {
          return new Promise<Response>((resolve) => {
            pendingReview = { resolve }
          })
        }
        return Promise.resolve(response({ review: reviewSummary(reviewPayload as MappingReviewPayload) }))
      }
      if (path.startsWith('/api/mapping/jobs/1234567890abcdef1234567890abcdef/export')) {
        exportCalls.push(path)
        if (mode === 'downloadError') {
          return Promise.resolve(response({ detail: { error: 'mapping_review_incomplete', message: 'All source fields must be reviewed before export.' } }, 409))
        }
        const format = path.endsWith('format=csv') ? 'csv' : 'json'
        return Promise.resolve(new Response(format === 'csv' ? 'source_field,action\n' : '{"ok":true}\n', {
          status: 200,
          headers: {
            'Content-Type': format === 'csv' ? 'text/csv; charset=utf-8' : 'application/json',
            'Content-Disposition': `attachment; filename="mapping-review-1234567890abcdef1234567890abcdef.${format}"`,
          },
        }))
      }
      if (path === '/api/mapping/jobs/1234567890abcdef1234567890abcdef') {
        getJobCalls += 1
        return Promise.resolve(response(mode === 'existingV5' ? mappingJobWithV5Review() : mappingJobWithReview()))
      }
      if (path === '/api/migration/workspaces/erpnext-item-price') {
        return Promise.resolve(response({ workspace: { title: 'ERPNext Item + Item Price' }, summary: {}, mappings: [], decisions: [], build: { available: false }, resources: [] }))
      }
      return Promise.resolve(response({ detail: { error: 'not_mocked', message: path } }, 404))
    }),
  )
}

function renderView(node = <MappingJobView />) {
  return render(
    <MantineProvider theme={theme} forceColorScheme="dark">
      {node}
    </MantineProvider>,
  )
}

function makeFile(name: string, text: string, sizeOverride?: number) {
  const file = new File([text], name, { type: 'text/csv' })
  Object.defineProperty(file, 'text', { configurable: true, value: vi.fn(async () => text) })
  if (sizeOverride !== undefined) {
    Object.defineProperty(file, 'size', { configurable: true, value: sizeOverride })
  }
  return file
}

function upload(file: File) {
  const input = document.querySelector('input[type="file"]')
  if (!(input instanceof HTMLInputElement)) throw new Error('file input not found')
  fireEvent.change(input, { target: { files: [file] } })
}

async function runMappingJob() {
  renderView()
  await waitFor(() => expect(screen.getByText('Generic Customer Migration Contract')).toBeDefined())
  upload(makeFile('customers.csv', `legacy_id,name\n1,${sentinel}\n`))
  fireEvent.click(screen.getByText('Run mapping job'))
  await waitFor(() => expect(screen.getByText('Mapping Results')).toBeDefined())
}

beforeEach(() => {
  installFetch()
  localSetItem = vi.fn()
  sessionSetItem = vi.fn()
  createObjectURL = vi.fn(() => 'blob:review-export')
  revokeObjectURL = vi.fn()
  vi.stubGlobal('localStorage', { getItem: vi.fn(), setItem: localSetItem, removeItem: vi.fn(), clear: vi.fn() })
  vi.stubGlobal('sessionStorage', { getItem: vi.fn(), setItem: sessionSetItem, removeItem: vi.fn(), clear: vi.fn() })
  Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: createObjectURL })
  Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: revokeObjectURL })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('MappingJobView', () => {
  it('loads the catalog, shows three contracts, and defaults to explicit V5', async () => {
    renderView()
    await waitFor(() => expect(screen.getByText('Generic Customer Migration Contract')).toBeDefined())
    expect(screen.getByText('SAP Supplier Reference Migration Contract')).toBeDefined()
    expect(screen.getByText('ERPNext Item and Item Price Reference Contract')).toBeDefined()
    expect(catalog.contracts[0].target_fields).toContain('customer.customer_id')
    expect(screen.getByText('Precision Tiered V5 — Identifier-aware')).toBeDefined()
    expect(screen.getAllByText('Precision Tiered V4').length).toBeGreaterThan(0)
    expect(screen.getByText('Baseline')).toBeDefined()
    expect((screen.getByLabelText('Scorer') as HTMLSelectElement).value).toBe('precision_tiered_v5')
    fireEvent.change(screen.getByLabelText('Scorer'), { target: { value: 'precision_tiered_v4' } })
    expect((screen.getByLabelText('Scorer') as HTMLSelectElement).value).toBe('precision_tiered_v4')
    fireEvent.change(screen.getByLabelText('Scorer'), { target: { value: 'baseline' } })
    expect((screen.getByLabelText('Scorer') as HTMLSelectElement).value).toBe('baseline')
  })

  it('keeps run disabled without a file and rejects non CSV files before POST', async () => {
    renderView()
    await waitFor(() => expect(screen.getByText('Generic Customer Migration Contract')).toBeDefined())
    expect((screen.getByText('Run mapping job').closest('button') as HTMLButtonElement).disabled).toBe(true)
    upload(makeFile('customers.txt', 'a,b\n1,2\n'))
    fireEvent.click(screen.getByText('Run mapping job'))
    expect(await screen.findByText('invalid_mapping_filename · Filename must end with .csv')).toBeDefined()
    expect(postCalls).toBe(0)
  })

  it('rejects files larger than 1 MiB before reading text or posting', async () => {
    renderView()
    await waitFor(() => expect(screen.getByText('Generic Customer Migration Contract')).toBeDefined())
    const file = makeFile('customers.csv', 'a,b\n1,2\n', 1024 * 1024 + 1)
    upload(file)
    fireEvent.click(screen.getByText('Run mapping job'))
    expect(await screen.findByText('mapping_csv_too_large · CSV must be at most 1 MiB')).toBeDefined()
    expect(file.text).not.toHaveBeenCalled()
    expect(postCalls).toBe(0)
  })

  it('posts a valid CSV as JSON, disables during loading, and renders summary', async () => {
    installFetch('pending')
    renderView()
    await waitFor(() => expect(screen.getByText('Generic Customer Migration Contract')).toBeDefined())
    upload(makeFile('customers.csv', `legacy_id,name\n1,${sentinel}\n`))
    fireEvent.click(screen.getByText('Run mapping job'))
    expect((screen.getByText('Run mapping job').closest('button') as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText('正在分析字段并生成 Top-3...')).toBeDefined()
    await waitFor(() => expect(pendingPost).not.toBeNull())
    pendingPost?.resolve(response(mappingJob('customers.csv', 'precision_tiered_v5'), 201))
    await waitFor(() => expect(screen.getByText('Mapping Results')).toBeDefined())
    expect(postPayload).toMatchObject({
      contract_id: 'generic-customer',
      filename: 'customers.csv',
      csv_text: `legacy_id,name\n1,${sentinel}\n`,
      scorer: 'precision_tiered_v5',
    })
    expect(screen.getByText('Target coverage')).toBeDefined()
    expect(screen.getByText('customer_master · v1.0.0')).toBeDefined()
  })

  it('renders Top-3 candidates, ranking score, and V5 evidence without raw sentinel data', async () => {
    renderView()
    await waitFor(() => expect(screen.getByText('Generic Customer Migration Contract')).toBeDefined())
    upload(makeFile('customers.csv', `legacy_id,name\n1,${sentinel}\n`))
    fireEvent.click(screen.getByText('Run mapping job'))
    await waitFor(() => expect(screen.getAllByText('routing_number').length).toBeGreaterThan(0))
    expect(screen.getAllByText('Ranking Score').length).toBeGreaterThan(0)
    expect(screen.getAllByText('customer_bank.routing_number').length).toBeGreaterThan(0)
    expect(screen.getAllByText('routing_to_routing').length).toBeGreaterThan(0)
    expect(screen.getAllByText('diagnostic 0.120 · supportive 0.040').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Top-1 reason: precision tier and interaction support').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Identifier interaction: entity_identifier_support').length).toBeGreaterThan(0)
    expect(screen.getAllByText('tier entity_identifier 路 matched entity concepts customer').length).toBeGreaterThan(0)
    expect(screen.getAllByText('V4 score 0.730 路 identifier bonus 0.140').length).toBeGreaterThan(0)
    expect(screen.getAllByText('adjusted V5 score 0.870 路 Top-1 eligibility yes').length).toBeGreaterThan(0)
    expect(screen.getAllByText('V5 selection reason: identifier_adjusted_score_strictly_exceeded_v4_top1').length).toBeGreaterThan(0)
    expect(screen.queryByText(sentinel)).toBeNull()
    expect(screen.queryByText('ground_truth')).toBeNull()
    expect(screen.queryByText('expected_targets')).toBeNull()
    expect(screen.queryByText('_internal_rank_key')).toBeNull()
  })

  it('does not show a fake V5 evidence panel for V4 jobs without identifier interaction', async () => {
    renderView()
    await waitFor(() => expect(screen.getByText('Generic Customer Migration Contract')).toBeDefined())
    fireEvent.change(screen.getByLabelText('Scorer'), { target: { value: 'precision_tiered_v4' } })
    upload(makeFile('customers.csv', `legacy_id,name\n1,${sentinel}\n`))
    fireEvent.click(screen.getByText('Run mapping job'))
    await waitFor(() => expect(screen.getAllByText('routing_number').length).toBeGreaterThan(0))
    expect(postPayload).toMatchObject({ scorer: 'precision_tiered_v4' })
    expect(screen.queryByText(/Identifier interaction:/)).toBeNull()
    expect(screen.getAllByText('Top-1 reason: precision tier and interaction support').length).toBeGreaterThan(0)
  })

  it('shows a manual review prompt when recommendation is null', async () => {
    renderView()
    await waitFor(() => expect(screen.getByText('Generic Customer Migration Contract')).toBeDefined())
    upload(makeFile('customers.csv', 'notes\nreview\n'))
    fireEvent.click(screen.getByText('Run mapping job'))
    await waitFor(() => expect(screen.getAllByText('notes').length).toBeGreaterThan(0))
    expect(screen.getByText('No automatic recommendation, manual review required')).toBeDefined()
  })

  it('loads an existing 32-hex job with GET and reuses the result UI', async () => {
    renderView()
    await waitFor(() => expect(screen.getByText('Generic Customer Migration Contract')).toBeDefined())
    fireEvent.change(screen.getByLabelText('Load existing job'), { target: { value: '1234567890abcdef1234567890abcdef' } })
    fireEvent.click(screen.getByText('Load job'))
    await waitFor(() => expect(screen.getByText('loaded.csv')).toBeDefined())
    expect(getJobCalls).toBe(1)
    expect(screen.getAllByText('customer_bank.routing_number').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Precision Tiered V4').length).toBeGreaterThan(0)
  })

  it('loads an existing V5 job without overwriting its scorer', async () => {
    installFetch('existingV5')
    renderView()
    await waitFor(() => expect(screen.getByText('Generic Customer Migration Contract')).toBeDefined())
    fireEvent.change(screen.getByLabelText('Scorer'), { target: { value: 'baseline' } })
    fireEvent.change(screen.getByLabelText('Load existing job'), { target: { value: '1234567890abcdef1234567890abcdef' } })
    fireEvent.click(screen.getByText('Load job'))
    await waitFor(() => expect(screen.getByText('loaded-v5.csv')).toBeDefined())
    expect(screen.getAllByText('Precision Tiered V5').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Identifier interaction: entity_identifier_support').length).toBeGreaterThan(0)
  })

  it('rejects invalid job IDs without sending GET', async () => {
    renderView()
    await waitFor(() => expect(screen.getByText('Generic Customer Migration Contract')).toBeDefined())
    fireEvent.change(screen.getByLabelText('Load existing job'), { target: { value: 'ABC' } })
    expect(screen.getByText('invalid_mapping_job_id · Job ID must be 32 lowercase hex characters')).toBeDefined()
    expect((screen.getByText('Load job').closest('button') as HTMLButtonElement).disabled).toBe(true)
    expect(getJobCalls).toBe(0)
  })

  it('displays structured backend errors without raw JSON or traceback', async () => {
    installFetch('error')
    renderView()
    await waitFor(() => expect(screen.getByText('Generic Customer Migration Contract')).toBeDefined())
    upload(makeFile('customers.csv', 'legacy_id,name\n1,Alice\n'))
    fireEvent.click(screen.getByText('Run mapping job'))
    expect(await screen.findByText('mapping_model_unavailable · Local model unavailable.')).toBeDefined()
    expect(screen.queryByText('Traceback')).toBeNull()
    expect(screen.queryByText('{ detail')).toBeNull()
  })

  it('does not write uploaded content to browser storage', async () => {
    renderView()
    await waitFor(() => expect(screen.getByText('Generic Customer Migration Contract')).toBeDefined())
    upload(makeFile('customers.csv', `legacy_id,name\n1,${sentinel}\n`))
    fireEvent.click(screen.getByText('Run mapping job'))
    await waitFor(() => expect(screen.getByText('customers.csv')).toBeDefined())
    expect(localSetItem).not.toHaveBeenCalled()
    expect(sessionSetItem).not.toHaveBeenCalled()
    expect(screen.queryByText(sentinel)).toBeNull()
  })

  it('shows pending manual review after initial results', async () => {
    await runMappingJob()
    expect(screen.getByText('人工复核')).toBeDefined()
    expect(screen.getByText('0 / 2')).toBeDefined()
    expect(screen.getByText('还剩 2 个字段待复核')).toBeDefined()
    expect((screen.getByText('下载 JSON').closest('button') as HTMLButtonElement).disabled).toBe(true)
  })

  it('accepts an algorithm suggestion and sends a partial save with report SHA', async () => {
    await runMappingJob()
    fireEvent.click(screen.getByLabelText('接受算法建议 customer_bank.routing_number'))
    expect(screen.getByText(/有未保存修改/)).toBeDefined()
    fireEvent.click(screen.getByText('保存复核'))
    await waitFor(() => expect(reviewCalls).toBe(1))
    expect(reviewPayload).toEqual({
      mapping_report_sha256: 'm'.repeat(64),
      decisions: [{ source_field: 'routing_number', action: 'accept_suggestion' }],
    })
    expect(await screen.findByText('复核已保存')).toBeDefined()
  })

  it('disables accept suggestion when recommendation is unavailable', async () => {
    await runMappingJob()
    const disabledAccept = screen.getByLabelText('接受算法建议不可用') as HTMLInputElement
    expect(disabledAccept.disabled).toBe(true)
    expect(screen.getByText('该字段没有可接受的 recommendation')).toBeDefined()
  })

  it('supports override target selection and multi-target selection', async () => {
    await runMappingJob()
    expect(mappingJob().mappings[0].top_candidates?.map((candidate) => candidate.target)).not.toContain('customer.customer_id')
    fireEvent.click(screen.getAllByLabelText('改选目标')[0])
    expect(screen.getByLabelText('customer.country')).toBeDefined()
    expect(screen.getByLabelText('customer_bank.currency')).toBeDefined()
    fireEvent.click(screen.getByLabelText('customer.customer_id'))
    fireEvent.click(screen.getByLabelText('customer_bank.routing_number'))
    fireEvent.click(screen.getByText('保存复核'))
    await waitFor(() => expect(reviewCalls).toBe(1))
    expect(reviewPayload?.decisions[0]).toEqual({
      source_field: 'routing_number',
      action: 'select_target',
      target_fields: ['customer.customer_id', 'customer_bank.routing_number'],
    })
  })

  it('lets client_number select customer.customer_id even when it is outside Top-3', async () => {
    installFetch('clientNumber')
    renderView()
    await waitFor(() => expect(screen.getByText('Generic Customer Migration Contract')).toBeDefined())
    upload(makeFile('client-number.csv', `client_number,name\nC-1,${sentinel}\n`))
    fireEvent.click(screen.getByText('Run mapping job'))
    await waitFor(() => expect(screen.getAllByText('client_number').length).toBeGreaterThan(0))
    expect(mappingJobWithClientNumber().mappings[0].top_candidates?.map((candidate) => candidate.target)).not.toContain('customer.customer_id')
    fireEvent.click(screen.getAllByLabelText('改选目标')[0])
    fireEvent.click(screen.getByLabelText('customer.customer_id'))
    fireEvent.click(screen.getByText('保存复核'))
    await waitFor(() => expect(reviewPayload?.decisions[0]).toEqual({
      source_field: 'client_number',
      action: 'select_target',
      target_fields: ['customer.customer_id'],
    }))
  })

  it('mark unmapped clears selected targets', async () => {
    await runMappingJob()
    fireEvent.click(screen.getAllByLabelText('改选目标')[0])
    fireEvent.click(screen.getByLabelText('customer.customer_id'))
    fireEvent.click(screen.getAllByLabelText('标记不映射')[0])
    fireEvent.click(screen.getByText('保存复核'))
    await waitFor(() => expect(reviewPayload?.decisions[0]).toEqual({
      source_field: 'routing_number',
      action: 'mark_unmapped',
      target_fields: [],
    }))
  })

  it('prevents duplicate save requests while saving', async () => {
    installFetch('reviewPending')
    await runMappingJob()
    fireEvent.click(screen.getByLabelText('接受算法建议 customer_bank.routing_number'))
    fireEvent.click(screen.getByText('保存复核'))
    expect((screen.getByText('保存复核').closest('button') as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getByText('保存复核'))
    expect(reviewCalls).toBe(1)
    pendingReview?.resolve(response({ review: reviewSummary(reviewPayload as MappingReviewPayload) }))
    await waitFor(() => expect(screen.getByText('复核已保存')).toBeDefined())
  })

  it('restores saved review from an existing job including multi-target decisions', async () => {
    renderView()
    await waitFor(() => expect(screen.getByText('Generic Customer Migration Contract')).toBeDefined())
    fireEvent.change(screen.getByLabelText('Load existing job'), { target: { value: '1234567890abcdef1234567890abcdef' } })
    fireEvent.click(screen.getByText('Load job'))
    await waitFor(() => expect(screen.getByText('2 / 2')).toBeDefined())
    expect(screen.getByText('saved multi')).toBeDefined()
    expect(mappingJobWithReview().mappings[0].top_candidates?.map((candidate) => candidate.target)).not.toContain('customer.customer_id')
    expect((screen.getByText('下载 JSON').closest('button') as HTMLButtonElement).disabled).toBe(false)
    expect((screen.getAllByLabelText('customer.customer_id')[0] as HTMLInputElement).checked).toBe(true)
    expect((screen.getAllByLabelText('customer_bank.routing_number')[0] as HTMLInputElement).checked).toBe(true)
  })

  it('disables target override safely until the matching contract allowlist is loaded', async () => {
    installFetch('contractsPending')
    renderView()
    fireEvent.change(screen.getByLabelText('Load existing job'), { target: { value: '1234567890abcdef1234567890abcdef' } })
    fireEvent.click(screen.getByText('Load job'))
    await waitFor(() => expect(screen.getByText('Mapping Results')).toBeDefined())
    const override = screen.getAllByLabelText('改选目标')[0] as HTMLInputElement
    expect(override.disabled).toBe(true)
    expect(screen.getAllByText('当前 job 的 contract target allowlist 尚未加载或不匹配，不能猜测目标字段。').length).toBeGreaterThan(0)
  })

  it('tracks unsaved changes and blocks export until saved', async () => {
    renderView()
    await waitFor(() => expect(screen.getByText('Generic Customer Migration Contract')).toBeDefined())
    fireEvent.change(screen.getByLabelText('Load existing job'), { target: { value: '1234567890abcdef1234567890abcdef' } })
    fireEvent.click(screen.getByText('Load job'))
    await waitFor(() => expect(screen.getByText('2 / 2')).toBeDefined())
    fireEvent.change(screen.getAllByLabelText('Note')[0], { target: { value: 'changed' } })
    expect(screen.getByText('有未保存修改 · 请先保存复核后再导出')).toBeDefined()
    expect((screen.getByText('下载 CSV').closest('button') as HTMLButtonElement).disabled).toBe(true)
  })

  it('downloads JSON and CSV exports and revokes object URLs', async () => {
    renderView()
    await waitFor(() => expect(screen.getByText('Generic Customer Migration Contract')).toBeDefined())
    fireEvent.change(screen.getByLabelText('Load existing job'), { target: { value: '1234567890abcdef1234567890abcdef' } })
    fireEvent.click(screen.getByText('Load job'))
    await waitFor(() => expect(screen.getByText('2 / 2')).toBeDefined())
    fireEvent.click(screen.getByText('下载 JSON'))
    await waitFor(() => expect(exportCalls).toContain('/api/mapping/jobs/1234567890abcdef1234567890abcdef/export?format=json'))
    await waitFor(() => expect(revokeObjectURL).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByText('下载 CSV'))
    await waitFor(() => expect(exportCalls).toContain('/api/mapping/jobs/1234567890abcdef1234567890abcdef/export?format=csv'))
    expect(createObjectURL).toHaveBeenCalledTimes(2)
    expect(revokeObjectURL).toHaveBeenCalledTimes(2)
  })

  it('shows structured stale review errors without raw response text', async () => {
    installFetch('reviewError')
    await runMappingJob()
    fireEvent.click(screen.getByLabelText('接受算法建议 customer_bank.routing_number'))
    fireEvent.click(screen.getByText('保存复核'))
    expect(await screen.findByText('mapping_review_stale · Mapping report SHA does not match the current job.')).toBeDefined()
    expect(screen.queryByText('Traceback')).toBeNull()
  })

  it('shows structured download errors without raw response text', async () => {
    installFetch('downloadError')
    renderView()
    await waitFor(() => expect(screen.getByText('Generic Customer Migration Contract')).toBeDefined())
    fireEvent.change(screen.getByLabelText('Load existing job'), { target: { value: '1234567890abcdef1234567890abcdef' } })
    fireEvent.click(screen.getByText('Load job'))
    await waitFor(() => expect(screen.getByText('2 / 2')).toBeDefined())
    fireEvent.click(screen.getByText('下载 JSON'))
    expect(await screen.findByText('mapping_review_incomplete · All source fields must be reviewed before export.')).toBeDefined()
    expect(screen.queryByText('Traceback')).toBeNull()
  })

  it('clears old review draft and errors when switching jobs or files', async () => {
    await runMappingJob()
    fireEvent.click(screen.getByLabelText('接受算法建议 customer_bank.routing_number'))
    expect(screen.getByText(/有未保存修改/)).toBeDefined()
    fireEvent.change(screen.getByLabelText('Load existing job'), { target: { value: '1234567890abcdef1234567890abcdea' } })
    expect(screen.queryByText(/有未保存修改/)).toBeNull()
    upload(makeFile('next.csv', 'a,b\n1,2\n'))
    expect(screen.queryByText('人工复核')).toBeNull()
  })

  it('does not use browser storage or console logging for review and export content', async () => {
    const log = vi.spyOn(console, 'log').mockImplementation(() => undefined)
    await runMappingJob()
    fireEvent.click(screen.getByLabelText('接受算法建议 customer_bank.routing_number'))
    fireEvent.click(screen.getByText('保存复核'))
    await waitFor(() => expect(reviewCalls).toBe(1))
    expect(localSetItem).not.toHaveBeenCalled()
    expect(sessionSetItem).not.toHaveBeenCalled()
    expect(log).not.toHaveBeenCalled()
  })
})

describe('App mapping job navigation', () => {
  it('contains the new page and keeps the migration workspace reachable', async () => {
    renderView(<App />)
    await waitFor(() => expect(screen.getByText('新建字段映射')).toBeDefined())
    fireEvent.click(screen.getByText('新建字段映射'))
    expect(screen.getByText('Upload CSV · Contract-aware Top-3 Ranking')).toBeDefined()
    fireEvent.click(screen.getByText('迁移工作台'))
    await waitFor(() => expect(screen.getAllByText('迁移工作台').length).toBeGreaterThan(0))
  })
})
