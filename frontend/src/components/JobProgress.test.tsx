import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { queryKeys } from '../hooks/queryKeys'
import type { AnalysisJob } from '../types/api'
import { mockApi } from '../test/mockApi'
import { createTestQueryClient } from '../test/render'
import { JobProgress } from './JobProgress'

const P = 'p-1'
const R = 'r-1'
const JOBS_KEY = `GET /projects/${P}/repositories/${R}/jobs`

function job(status: AnalysisJob['status'], extra: Partial<AnalysisJob> = {}): AnalysisJob {
  return {
    id: 'job-1',
    project_id: P,
    repository_id: R,
    job_type: 'ingestion',
    status,
    progress: status === 'completed' ? 100 : 0,
    error_message: null,
    created_at: '2026-10-01T00:00:00Z',
    started_at: null,
    completed_at: null,
    ...extra,
  }
}

function renderProgress() {
  const queryClient = createTestQueryClient()
  const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
  render(
    <QueryClientProvider client={queryClient}>
      <JobProgress projectId={P} repositoryId={R} pollIntervalMs={20} />
    </QueryClientProvider>,
  )
  return { invalidate }
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

describe('JobProgress', () => {
  it('polls while active, stops when completed and refreshes repository data', async () => {
    let calls = 0
    mockApi({
      [JOBS_KEY]: () => {
        calls += 1
        return {
          status: 200,
          data: [calls < 3 ? job(calls === 1 ? 'pending' : 'processing') : job('completed')],
        }
      },
    })
    const { invalidate } = renderProgress()

    await waitFor(() => expect(screen.getByTestId('status-badge').textContent).toBe('completed'))
    expect(screen.getByTestId('job-progress').textContent).toBe('100%')
    const callsWhenDone = calls
    await sleep(150)
    expect(calls).toBe(callsWhenDone) // polling stopped

    const refreshed = invalidate.mock.calls.map((call) => JSON.stringify(call[0]?.queryKey))
    for (const key of [
      queryKeys.repository(P, R),
      queryKeys.tree(P, R),
      queryKeys.languages(P, R),
      queryKeys.jobs(P, R),
    ]) {
      expect(refreshed).toContain(JSON.stringify(key))
    }
  })

  it('stops polling on failure and shows the error as text', async () => {
    let calls = 0
    const message = '<script>alert(1)</script> Archive contains an unsafe path'
    mockApi({
      [JOBS_KEY]: () => {
        calls += 1
        return { status: 200, data: [calls < 2 ? job('processing') : job('failed', { error_message: message })] }
      },
    })
    renderProgress()
    expect((await screen.findByRole('alert')).textContent).toBe(message)
    expect(document.querySelector('script')).toBeNull()
    const callsWhenDone = calls
    await sleep(150)
    expect(calls).toBe(callsWhenDone)
  })

  it('does not poll when the latest job already finished', async () => {
    let calls = 0
    mockApi({
      [JOBS_KEY]: () => {
        calls += 1
        return { status: 200, data: [job('completed'), job('failed', { id: 'older' })] }
      },
    })
    renderProgress()
    await screen.findByLabelText('Recent jobs')
    await sleep(150)
    expect(calls).toBe(1)
  })

  it('shows an empty state', async () => {
    mockApi({ [JOBS_KEY]: { status: 200, data: [] } })
    renderProgress()
    expect(await screen.findByText('No ingestion jobs yet.')).toBeTruthy()
  })
})
