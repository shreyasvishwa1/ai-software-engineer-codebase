import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { AxiosAdapter } from 'axios'
import { describe, expect, it, vi } from 'vitest'

import { queryKeys } from '../hooks/queryKeys'
import { apiClient } from '../services/client'
import { mockApi, type MockResponse, PROJECT, ZIP_REPOSITORY } from '../test/mockApi'
import { createTestQueryClient } from '../test/render'
import type { AnalysisJob } from '../types/api'
import { JobProgress } from './JobProgress'

const P = PROJECT.id
const R = ZIP_REPOSITORY.id
const JOBS_PATH = `/projects/${P}/repositories/${R}/jobs`
const cancelKey = (id: string) => `POST ${JOBS_PATH}/${id}/cancel`

function job(id: string, status: AnalysisJob['status'], extra: Partial<AnalysisJob> = {}): AnalysisJob {
  return {
    id,
    project_id: P,
    repository_id: R,
    job_type: 'ingestion',
    status,
    progress: status === 'completed' ? 100 : 0,
    error_message: null,
    created_at: '2026-10-02T00:00:00Z',
    started_at: null,
    completed_at: null,
    ...extra,
  }
}

const MESSAGES = { ingestion: 'Ingestion was cancelled', code_metrics: 'Analysis was cancelled' }

/** A tiny stateful server: GET lists the jobs, POST .../cancel fails a pending job. */
function serve(initial: AnalysisJob[], cancelResponse?: (id: string) => MockResponse) {
  let jobs = initial
  const counts = { list: 0 }
  const routes: Record<string, () => MockResponse> = {
    [`GET ${JOBS_PATH}`]: () => {
      counts.list += 1
      return { status: 200, data: jobs }
    },
  }
  for (const current of initial) {
    routes[cancelKey(current.id)] = () => {
      if (cancelResponse) {
        return cancelResponse(current.id)
      }
      const updated = {
        ...current,
        status: 'failed' as const,
        error_message: MESSAGES[current.job_type],
        completed_at: '2026-10-02T00:01:00Z',
      }
      jobs = jobs.map((j) => (j.id === current.id ? updated : j))
      return { status: 200, data: updated }
    }
  }
  const requests = mockApi(routes)
  return {
    requests,
    counts,
    setJobs: (next: AnalysisJob[]) => {
      jobs = next
    },
  }
}

function renderJobs(pollIntervalMs = 60_000) {
  const queryClient = createTestQueryClient()
  const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
  const view = render(
    <QueryClientProvider client={queryClient}>
      <JobProgress projectId={P} repositoryId={R} pollIntervalMs={pollIntervalMs} />
    </QueryClientProvider>,
  )
  const refreshed = () => invalidate.mock.calls.map((call) => JSON.stringify(call[0]?.queryKey))
  return { ...view, refreshed }
}

const cancelButtons = () => screen.queryAllByRole('button', { name: /^Cancel / })
const posts = (requests: { method: string; url: string; data: unknown }[]) =>
  requests.filter((r) => r.method === 'POST')

describe('Cancel buttons', () => {
  it.each([
    ['ingestion', 'Cancel ingestion job'],
    ['code_metrics', 'Cancel code analysis job'],
  ] as const)('a pending latest %s job can be cancelled', async (type, name) => {
    serve([job('j1', 'pending', { job_type: type })])
    renderJobs()
    expect(await screen.findByRole('button', { name })).toBeTruthy()
    expect(cancelButtons()).toHaveLength(1)
  })

  it('a pending older job can be cancelled', async () => {
    serve([job('j2', 'completed'), job('j1', 'pending', { job_type: 'code_metrics' })])
    renderJobs()
    const recent = within(await screen.findByLabelText('Recent jobs'))
    expect(recent.getByRole('button', { name: 'Cancel code analysis job' })).toBeTruthy()
    expect(cancelButtons()).toHaveLength(1)
  })

  it.each(['processing', 'completed', 'failed'] as const)('a %s job has no Cancel button', async (status) => {
    serve([job('j2', status), job('j1', status)])
    renderJobs()
    await screen.findByTestId('job-type')
    expect(cancelButtons()).toHaveLength(0)
  })
})

describe('Cancelling', () => {
  it('sends exactly one POST without a body for the clicked row', async () => {
    const { requests } = serve([
      job('j2', 'pending', { job_type: 'code_metrics' }),
      job('j1', 'pending'),
    ])
    renderJobs()
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel ingestion job' }))
    await waitFor(() => expect(posts(requests)).toHaveLength(1))
    expect(posts(requests)[0].url).toBe(`${JOBS_PATH}/j1/cancel`)
    expect(posts(requests)[0].data).toBeUndefined()
  })

  it('disables only the clicked button while its request is in flight', async () => {
    const { requests } = serve([
      job('j2', 'pending', { job_type: 'code_metrics' }),
      job('j1', 'pending'),
    ])
    renderJobs()
    const analysis = (await screen.findByRole('button', {
      name: 'Cancel code analysis job',
    })) as HTMLButtonElement
    const ingestion = screen.getByRole('button', { name: 'Cancel ingestion job' }) as HTMLButtonElement
    // Hold POSTs at the transport until released.
    const mocked = apiClient.defaults.adapter as AxiosAdapter
    let release: () => void = () => undefined
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    apiClient.defaults.adapter = async (config) => {
      if (config.method?.toUpperCase() === 'POST') {
        await gate
      }
      return mocked(config)
    }

    fireEvent.click(analysis)
    await waitFor(() => expect(analysis.disabled).toBe(true))
    expect(ingestion.disabled).toBe(false) // unrelated button stays usable
    fireEvent.click(ingestion)
    await waitFor(() => expect(ingestion.disabled).toBe(true))
    release()
    await waitFor(() => expect(posts(requests).map((r) => r.url)).toEqual([
      `${JOBS_PATH}/j2/cancel`,
      `${JOBS_PATH}/j1/cancel`,
    ]))
    await waitFor(() => expect(cancelButtons()).toHaveLength(0))
  })

  it.each(['ingestion', 'code_metrics'] as const)(
    'shows the server state after cancelling a %s job and refreshes jobs and metrics',
    async (type) => {
      serve([job('j1', 'pending', { job_type: type })])
      const { refreshed } = renderJobs()
      fireEvent.click(await screen.findByRole('button', { name: /^Cancel / }))
      expect((await screen.findByRole('alert')).textContent).toBe(MESSAGES[type])
      expect(screen.getByTestId('status-badge').textContent).toBe('failed')
      expect(cancelButtons()).toHaveLength(0)
      await waitFor(() => {
        expect(refreshed()).toContain(JSON.stringify(queryKeys.jobs(P, R)))
        expect(refreshed()).toContain(JSON.stringify(queryKeys.codeMetrics(P, R)))
      })
    },
  )

  it('shows the exact 409 detail, refreshes the jobs and keeps the error visible', async () => {
    const detail = 'Only pending jobs can be cancelled'
    const server = serve([job('j1', 'pending')], () => {
      // The worker claimed the job just before the cancel arrived.
      server.setJobs([job('j1', 'processing')])
      return { status: 409, data: { detail } }
    })
    renderJobs()
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel ingestion job' }))
    expect((await screen.findByRole('alert')).textContent).toBe(detail)
    await waitFor(() => expect(screen.getByTestId('status-badge').textContent).toBe('processing'))
    expect(server.counts.list).toBeGreaterThanOrEqual(2) // refreshed after the failure
    expect(cancelButtons()).toHaveLength(0) // the job is no longer pending
    expect(screen.getByRole('alert').textContent).toBe(detail) // still visible
  })

  it('shows the exact 404 detail', async () => {
    serve([job('j1', 'pending')], () => ({
      status: 404,
      data: { detail: 'Analysis job not found' },
    }))
    renderJobs()
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel ingestion job' }))
    expect((await screen.findByRole('alert')).textContent).toBe('Analysis job not found')
  })

  it('renders server messages as text only', async () => {
    const detail = '<img src=x onerror=alert(1)><script>alert(2)</script>'
    serve([job('j1', 'pending')], () => ({ status: 409, data: { detail } }))
    const { container } = renderJobs()
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel ingestion job' }))
    expect((await screen.findByRole('alert')).textContent).toBe(detail)
    expect(container.querySelector('img, script')).toBeNull()
  })

  it('stops polling through the existing finish handling', async () => {
    const server = serve([job('j1', 'pending')])
    const { refreshed } = renderJobs(20)
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel ingestion job' }))
    await screen.findByText('Ingestion was cancelled')
    await waitFor(() => expect(refreshed()).toContain(JSON.stringify(queryKeys.tree(P, R))))
    const listCalls = server.counts.list
    await new Promise((resolve) => setTimeout(resolve, 150))
    expect(server.counts.list).toBe(listCalls) // polling stopped
  })
})

describe('Existing JobProgress behavior', () => {
  it('keeps the Step 24 labels, status and progress', async () => {
    serve([job('j2', 'pending', { job_type: 'code_metrics' }), job('j1', 'completed')])
    renderJobs()
    expect((await screen.findByTestId('job-type')).textContent).toBe('Code analysis')
    expect(screen.getByLabelText('Ingestion progress')).toBeTruthy()
    expect(screen.getAllByTestId('status-badge').map((b) => b.textContent)).toEqual([
      'pending',
      'completed',
    ])
    expect(screen.getByTestId('job-progress').textContent).toBe('0%')
    expect(within(screen.getByLabelText('Recent jobs')).getByText('Ingestion')).toBeTruthy()
  })
})
