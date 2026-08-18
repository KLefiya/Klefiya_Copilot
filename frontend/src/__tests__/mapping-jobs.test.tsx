import { MantineProvider } from '@mantine/core'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import App from '../App'
import type { MappingContractCatalog, MappingJobResponse } from '../lib/mappingJobs'
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
      supported_scorers: ['baseline', 'precision_tiered_v4'],
    },
    {
      contract_id: 'supplier-reference',
      title: 'SAP Supplier Reference Migration Contract',
      domain: 'supplier_master',
      version: '1.0.0',
      target_resource_count: 2,
      target_field_count: 15,
      supported_scorers: ['baseline', 'precision_tiered_v4'],
    },
    {
      contract_id: 'erpnext-item-price',
      title: 'ERPNext Item and Item Price Reference Contract',
      domain: 'product_and_pricing',
      version: '1.0.0',
      target_resource_count: 2,
      target_field_count: 11,
      supported_scorers: ['baseline', 'precision_tiered_v4'],
    },
  ],
}

function mappingJob(filename = 'customers.csv'): MappingJobResponse {
  return {
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
      scorer: 'precision_tiered_v4',
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
            target: 'customer.customer_id',
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
}

let postPayload: unknown
let postCalls = 0
let getJobCalls = 0
let pendingPost: { resolve: (value: Response) => void } | null = null
let localSetItem: ReturnType<typeof vi.fn>
let sessionSetItem: ReturnType<typeof vi.fn>

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status })
}

function installFetch(mode: 'ok' | 'pending' | 'error' = 'ok') {
  postPayload = null
  postCalls = 0
  getJobCalls = 0
  pendingPost = null
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init?: RequestInit) => {
      const path = url.replace('http://127.0.0.1:8000', '')
      if (path === '/api/health') {
        return Promise.resolve(response({ status: 'ok', service: 'api', version: '0.2.0', reports_available: 10, reports_total: 10, reports: [] }))
      }
      if (path === '/api/mapping/contracts') return Promise.resolve(response(catalog))
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
        return Promise.resolve(response(mappingJob('customers.csv'), 201))
      }
      if (path === '/api/mapping/jobs/1234567890abcdef1234567890abcdef') {
        getJobCalls += 1
        return Promise.resolve(response(mappingJob('loaded.csv')))
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

beforeEach(() => {
  installFetch()
  localSetItem = vi.fn()
  sessionSetItem = vi.fn()
  vi.stubGlobal('localStorage', { getItem: vi.fn(), setItem: localSetItem, removeItem: vi.fn(), clear: vi.fn() })
  vi.stubGlobal('sessionStorage', { getItem: vi.fn(), setItem: sessionSetItem, removeItem: vi.fn(), clear: vi.fn() })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('MappingJobView', () => {
  it('loads the catalog, shows three contracts, and defaults to explicit V4', async () => {
    renderView()
    await waitFor(() => expect(screen.getByText('Generic Customer Migration Contract')).toBeDefined())
    expect(screen.getByText('SAP Supplier Reference Migration Contract')).toBeDefined()
    expect(screen.getByText('ERPNext Item and Item Price Reference Contract')).toBeDefined()
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
    pendingPost?.resolve(response(mappingJob('customers.csv'), 201))
    await waitFor(() => expect(screen.getByText('Mapping Results')).toBeDefined())
    expect(postPayload).toMatchObject({
      contract_id: 'generic-customer',
      filename: 'customers.csv',
      csv_text: `legacy_id,name\n1,${sentinel}\n`,
      scorer: 'precision_tiered_v4',
    })
    expect(screen.getByText('Target coverage')).toBeDefined()
    expect(screen.getByText('customer_master · v1.0.0')).toBeDefined()
  })

  it('renders Top-3 candidates, ranking score, and V4 evidence without raw sentinel data', async () => {
    renderView()
    await waitFor(() => expect(screen.getByText('Generic Customer Migration Contract')).toBeDefined())
    upload(makeFile('customers.csv', `legacy_id,name\n1,${sentinel}\n`))
    fireEvent.click(screen.getByText('Run mapping job'))
    await waitFor(() => expect(screen.getByText('routing_number')).toBeDefined())
    fireEvent.click(screen.getByText('routing_number'))
    expect(screen.getAllByText('Ranking Score').length).toBeGreaterThan(0)
    expect(screen.getAllByText('customer_bank.routing_number').length).toBeGreaterThan(0)
    expect(screen.getByText('routing_to_routing')).toBeDefined()
    expect(screen.getByText('diagnostic 0.120 · supportive 0.040')).toBeDefined()
    expect(screen.getByText('Top-1 reason: precision tier and interaction support')).toBeDefined()
    expect(screen.queryByText(sentinel)).toBeNull()
    expect(screen.queryByText('ground_truth')).toBeNull()
    expect(screen.queryByText('expected_targets')).toBeNull()
  })

  it('shows a manual review prompt when recommendation is null', async () => {
    renderView()
    await waitFor(() => expect(screen.getByText('Generic Customer Migration Contract')).toBeDefined())
    upload(makeFile('customers.csv', 'notes\nreview\n'))
    fireEvent.click(screen.getByText('Run mapping job'))
    await waitFor(() => expect(screen.getByText('notes')).toBeDefined())
    expect(screen.getByText('No automatic recommendation, manual review required')).toBeDefined()
  })

  it('loads an existing 32-hex job with GET and reuses the result UI', async () => {
    renderView()
    await waitFor(() => expect(screen.getByText('Generic Customer Migration Contract')).toBeDefined())
    fireEvent.change(screen.getByLabelText('Load existing job'), { target: { value: '1234567890abcdef1234567890abcdef' } })
    fireEvent.click(screen.getByText('Load job'))
    await waitFor(() => expect(screen.getByText('loaded.csv')).toBeDefined())
    expect(getJobCalls).toBe(1)
    expect(screen.getByText('customer_bank.routing_number')).toBeDefined()
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
