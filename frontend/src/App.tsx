/**
 * 第 1 步：三层管道连通性验证页。
 *
 * 目的只有一个 —— 证明 React ↔ FastAPI ↔ 现有 JSON 报告 这条链路通了。
 * 页面刻意不做数据可视化：那是第 2 步的事，现在把原始 JSON 显示出来即可。
 */

import { useEffect, useState } from 'react'
import { API_BASE, ApiError, getHealth, getReport, type Health } from './api'

type Status = 'loading' | 'ok' | 'error'

const REPORT_TO_SHOW = 'vendor_profile_report'

function ErrorDetail({ error }: { error: ApiError }) {
  return (
    <div className="panel panel--error">
      <p className="panel__message">{error.message}</p>
      <pre className="code">{JSON.stringify(error.detail, null, 2)}</pre>
    </div>
  )
}

export default function App() {
  const [healthStatus, setHealthStatus] = useState<Status>('loading')
  const [health, setHealth] = useState<Health | null>(null)
  const [healthError, setHealthError] = useState<ApiError | null>(null)

  const [reportStatus, setReportStatus] = useState<Status>('loading')
  const [report, setReport] = useState<unknown>(null)
  const [reportError, setReportError] = useState<ApiError | null>(null)

  useEffect(() => {
    getHealth()
      .then((data) => {
        setHealth(data)
        setHealthStatus('ok')
      })
      .catch((error: ApiError) => {
        setHealthError(error)
        setHealthStatus('error')
      })

    getReport(REPORT_TO_SHOW)
      .then((data) => {
        setReport(data)
        setReportStatus('ok')
      })
      .catch((error: ApiError) => {
        setReportError(error)
        setReportStatus('error')
      })
  }, [])

  return (
    <main className="page">
      <header className="page__header">
        <h1>CarveOps Copilot</h1>
        <p className="page__subtitle">
          第 1 步 · 管道连通性验证 —— React ↔ FastAPI ↔ 现有 JSON 报告
        </p>
      </header>

      <section className="section">
        <h2>
          后端连通性
          <span className={`badge badge--${healthStatus}`}>
            {healthStatus === 'loading' && '检查中…'}
            {healthStatus === 'ok' && '后端已连接'}
            {healthStatus === 'error' && '后端未连接'}
          </span>
        </h2>
        <p className="muted">
          API 基址 <code className="code--inline">{API_BASE}</code>
        </p>

        {healthStatus === 'error' && healthError && <ErrorDetail error={healthError} />}

        {healthStatus === 'ok' && health && (
          <>
            <p className="muted">
              {health.service} v{health.version} · 已生成报告{' '}
              <strong>
                {health.reports_available}/{health.reports_total}
              </strong>
            </p>
            <table className="table">
              <thead>
                <tr>
                  <th>报告</th>
                  <th>模块</th>
                  <th>状态</th>
                  <th>生成方式</th>
                </tr>
              </thead>
              <tbody>
                {health.reports.map((item) => (
                  <tr key={item.name}>
                    <td>
                      <code className="code--inline">{item.name}</code>
                      <div className="muted">{item.title}</div>
                    </td>
                    <td className="muted">{item.module}</td>
                    <td>
                      {item.available ? (
                        <span className="badge badge--ok">已生成</span>
                      ) : (
                        <span className="badge badge--pending">未生成</span>
                      )}
                    </td>
                    <td>
                      <code className="code--inline">{item.generated_by}</code>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </section>

      <section className="section">
        <h2>
          报告读取
          <span className={`badge badge--${reportStatus}`}>
            {reportStatus === 'loading' && '读取中…'}
            {reportStatus === 'ok' && '读取成功'}
            {reportStatus === 'error' && '读取失败'}
          </span>
        </h2>
        <p className="muted">
          <code className="code--inline">GET /api/reports/{REPORT_TO_SHOW}</code> ——
          原始 JSON，第 2 步再做可视化
        </p>

        {reportStatus === 'error' && reportError && <ErrorDetail error={reportError} />}

        {reportStatus === 'ok' && (
          <pre className="code code--scroll">{JSON.stringify(report, null, 2)}</pre>
        )}
      </section>
    </main>
  )
}
