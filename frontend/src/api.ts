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

async function request<T>(path: string): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`)
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

export { API_BASE }
