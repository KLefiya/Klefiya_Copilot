/**
 * 后端 API 客户端。
 *
 * 后端只读报告，不触发分析。所有失败路径都带上后端给的结构化 detail
 * （例如「报告尚未生成，请跑 xxx.py」），不要吞掉它。
 */

const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://127.0.0.1:8000'

export interface ReportInfo {
  name: string
  title: string
  module: string
  available: boolean
  generated_by: string
  size_bytes?: number
  modified_at?: string
}

export interface Health {
  status: string
  service: string
  version: string
  project_root: string
  synthetic_dir_exists: boolean
  reports_available: number
  reports_total: number
  reports: ReportInfo[]
  notes: {
    read_only: string
    excluded_files: Record<string, string>
  }
}

/**
 * 后端用 HTTPException(detail={...}) 返回结构化错误，这里原样带出来。
 *
 * 不用构造函数参数属性（`constructor(readonly status: number)`）：
 * Vite 的 TS 配置开了 erasableSyntaxOnly，那是需要代码生成的 TS-only 语法，会报 TS1294。
 */
export class ApiError extends Error {
  readonly status: number
  readonly detail: unknown

  constructor(status: number, detail: unknown) {
    super(
      typeof detail === 'object' && detail !== null && 'message' in detail
        ? String((detail as { message: unknown }).message)
        : `请求失败（HTTP ${status}）`,
    )
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

async function request<T>(
  path: string,
  options?: {
    method?: 'GET' | 'PUT' | 'POST'
    body?: unknown
  },
): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method: options?.method ?? 'GET',
      headers: options?.body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: options?.body === undefined ? undefined : JSON.stringify(options.body),
    })
  } catch (cause) {
    // fetch 抛错 = 根本没连上（后端没起、端口不对、CORS 预检失败）
    throw new ApiError(0, {
      message: `连不上后端 ${API_BASE}。后端起来了吗？`,
      cause: String(cause),
    })
  }

  if (!response.ok) {
    let detail: unknown = await response.text()
    try {
      detail = (JSON.parse(detail as string) as { detail?: unknown }).detail ?? detail
    } catch {
      // 不是 JSON，就把原始文本当 detail
    }
    throw new ApiError(response.status, detail)
  }

  return (await response.json()) as T
}

export const getHealth = () => request<Health>('/api/health')

export const getReport = (name: string) => request<unknown>(`/api/reports/${name}`)

export const getMigrationWorkspaces = () => request<unknown>('/api/migration/workspaces')

export const getMigrationWorkspace = (workspaceId: string) =>
  request<unknown>(`/api/migration/workspaces/${encodeURIComponent(workspaceId)}`)

export const saveMigrationDecisions = (workspaceId: string, payload: unknown) =>
  request<unknown>(`/api/migration/workspaces/${encodeURIComponent(workspaceId)}/decisions`, {
    method: 'PUT',
    body: payload,
  })

export const buildMigrationPackage = (workspaceId: string, payload: unknown) =>
  request<unknown>(`/api/migration/workspaces/${encodeURIComponent(workspaceId)}/build`, {
    method: 'POST',
    body: payload,
  })

export const resetMigrationWorkspace = (workspaceId: string) =>
  request<unknown>(`/api/migration/workspaces/${encodeURIComponent(workspaceId)}/reset`, {
    method: 'POST',
  })

export const getMigrationResource = (workspaceId: string, resourceName: string, limit = 20) =>
  request<unknown>(
    `/api/migration/workspaces/${encodeURIComponent(workspaceId)}/resources/${encodeURIComponent(resourceName)}?limit=${encodeURIComponent(String(limit))}`,
  )

export const getMigrationLineage = (
  workspaceId: string,
  filters?: { source_field?: string; target_resource?: string; limit?: number },
) => {
  const params = new URLSearchParams()
  if (filters?.source_field) params.set('source_field', filters.source_field)
  if (filters?.target_resource) params.set('target_resource', filters.target_resource)
  if (filters?.limit) params.set('limit', String(filters.limit))
  const query = params.toString()
  return request<unknown>(
    `/api/migration/workspaces/${encodeURIComponent(workspaceId)}/lineage${query ? `?${query}` : ''}`,
  )
}

export { API_BASE }
