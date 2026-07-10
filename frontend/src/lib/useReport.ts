/** 拉取一份报告，返回 loading / error / data 三态。 */

import { useEffect, useState } from 'react'
import { ApiError, getReport } from '../api'

export interface ReportState<T> {
  loading: boolean
  error: ApiError | null
  data: T | null
}

export function useReport<T>(name: string): ReportState<T> {
  const [state, setState] = useState<ReportState<T>>({
    loading: true,
    error: null,
    data: null,
  })

  useEffect(() => {
    let cancelled = false
    setState({ loading: true, error: null, data: null })

    getReport(name)
      .then((data) => {
        if (!cancelled) setState({ loading: false, error: null, data: data as T })
      })
      .catch((error: ApiError) => {
        if (!cancelled) setState({ loading: false, error, data: null })
      })

    return () => {
      cancelled = true
    }
  }, [name])

  return state
}

/** 报告未生成 ≠ 服务器错误。后端在 detail 里给了该跑哪个脚本。 */
export function notGeneratedInfo(
  error: ApiError,
): { expectedPath: string; generatedBy: string } | null {
  const detail = error.detail
  if (
    typeof detail === 'object' &&
    detail !== null &&
    (detail as { error?: string }).error === 'report_not_generated'
  ) {
    const d = detail as { expected_path: string; generated_by: string }
    return { expectedPath: d.expected_path, generatedBy: d.generated_by }
  }
  return null
}
