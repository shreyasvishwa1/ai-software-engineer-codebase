import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { AxiosAdapter, InternalAxiosRequestConfig } from 'axios'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import { queryKeys } from '../hooks/queryKeys'
import { RepositoryPage } from '../pages/RepositoryPage'
import { apiClient } from '../services/client'
import {
  mockApi,
  type MockResponse,
  PROJECT,
  type RecordedRequest,
  ZIP_REPOSITORY,
} from '../test/mockApi'
import { createTestQueryClient } from '../test/render'
import type {
  AnalysisJob,
  CodeMetrics,
  CodeMetricsSummary,
  FileMetric,
  RepositoryStatus,
} from '../types/api'
import { CodeMetricsPanel, largestFiles } from './CodeMetricsPanel'
import { JobProgress } from './JobProgress'

const P = PROJECT.id
const R = ZIP_REPOSITORY.id
const METRICS_PATH = `/projects/${P}/repositories/${R}/analysis/code-metrics`
const GET_METRICS = `GET ${METRICS_PATH}`
const POST_METRICS = `POST ${METRICS_PATH}`
const JOBS_KEY = `GET /projects/${P}/repositories/${R}/jobs`

type Route = MockResponse | ((config: InternalAxiosRequestConfig) => MockResponse)

function job(
  status: AnalysisJob['status'],
  extra: Partial<AnalysisJob> = {},
): AnalysisJob {
  return {
    id: 'job-1',
    project_id: P,
    repository_id: R,
    job_type: 'code_metrics',
    status,
    progress: status === 'completed' ? 100 : 0,
    error_message: null,
    created_at: '2026-10-02T00:00:00Z',
    started_at: null,
    completed_at: null,
    ...extra,
  }
}

const SUMMARY: CodeMetricsSummary = {
  files: 6,
  analyzed: 3,
  binary: 1,
  too_large: 1,
  unavailable: 1,
  total_lines: 120,
  blank_lines: 17,
}

function metric(path: string, lines: number | null, extra: Partial<FileMetric> = {}): FileMetric {
  return {
    path,
    language: 'Python',
    status: lines === null ? 'binary' : 'analyzed',
    line_count: lines,
    blank_line_count: lines === null ? null : Math.floor(lines / 10),
    ...extra,
  }
}

function metrics(data: Partial<CodeMetrics> = {}): MockResponse {
  return { status: 200, data: { job: null, summary: null, files: [], ...data } }
}

function renderPanel(routes: Record<string, Route>, status: RepositoryStatus = 'ready') {
  const requests = mockApi(routes)
  const queryClient = createTestQueryClient()
  const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
  render(
    <QueryClientProvider client={queryClient}>
      <CodeMetricsPanel projectId={P} repositoryId={R} repositoryStatus={status} />
    </QueryClientProvider>,
  )
  return { requests, invalidate }
}

const button = () => screen.getByRole('button', { name: 'Analyze code' }) as HTMLButtonElement
const posts = (requests: RecordedRequest[]) => requests.filter((r) => r.method === 'POST')

describe('CodeMetricsPanel', () => {
  it('shows "No analysis yet" with an enabled button', async () => {
    const { requests } = renderPanel({ [GET_METRICS]: metrics() })
    expect(await screen.findByText('No analysis yet.')).toBeTruthy()
    expect(button().disabled).toBe(false)
    expect(posts(requests)).toHaveLength(0)
  })

  it('starts an analysis with exactly one POST and refreshes metrics and jobs', async () => {
    let gets = 0
    const { requests, invalidate } = renderPanel({
      [GET_METRICS]: () => {
        gets += 1
        return gets === 1 ? metrics() : metrics({ job: job('pending') })
      },
      [POST_METRICS]: {
        status: 202,
        data: { job_id: 'job-1', repository_id: R, status: 'pending' },
      },
    })
    await screen.findByText('No analysis yet.')
    fireEvent.click(button())

    expect(await screen.findByText('Analysis in progress…')).toBeTruthy()
    expect(posts(requests)).toHaveLength(1)
    expect(posts(requests)[0].url).toBe(METRICS_PATH)
    expect(posts(requests)[0].data).toBeUndefined() // no request body
    const refreshed = invalidate.mock.calls.map((call) => JSON.stringify(call[0]?.queryKey))
    expect(refreshed).toContain(JSON.stringify(queryKeys.codeMetrics(P, R)))
    expect(refreshed).toContain(JSON.stringify(queryKeys.jobs(P, R)))
    expect(button().disabled).toBe(true) // the new job is active
  })

  it('disables the button while the start request is in flight', async () => {
    const { requests } = renderPanel({
      [GET_METRICS]: metrics(),
      [POST_METRICS]: { status: 202, data: { job_id: 'j', repository_id: R, status: 'pending' } },
    })
    await screen.findByText('No analysis yet.')
    // Hold the POST at the transport until released.
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

    fireEvent.click(button())
    await waitFor(() => expect(button().disabled).toBe(true))
    fireEvent.click(button()) // ignored while disabled
    release()
    await waitFor(() => expect(posts(requests)).toHaveLength(1))
  })

  it.each(['pending', 'processing'] as const)(
    'disables the button while the latest analysis is %s',
    async (status) => {
      renderPanel({ [GET_METRICS]: metrics({ job: job(status) }) })
      expect(await screen.findByText('Analysis in progress…')).toBeTruthy()
      expect(button().disabled).toBe(true)
      expect(screen.queryByText('No analysis yet.')).toBeNull()
    },
  )

  it.each(['pending', 'processing', 'failed'] as const)(
    'explains and disables the button when the repository is %s',
    async (status) => {
      const { requests } = renderPanel({ [GET_METRICS]: metrics() }, status)
      await screen.findByText('No analysis yet.')
      expect(
        screen.getByText('The repository must be ingested successfully before it can be analyzed.'),
      ).toBeTruthy()
      expect(button().disabled).toBe(true)
      fireEvent.click(button())
      expect(posts(requests)).toHaveLength(0)
    },
  )

  it.each([
    [409, 'Repository must be ingested before analysis'],
    [409, 'Repository ingestion is in progress'],
    [409, 'Code analysis is already in progress'],
    [503, 'Analysis could not be queued; try again later'],
  ])('shows the exact server message for %i: %s', async (status, detail) => {
    renderPanel({
      [GET_METRICS]: metrics(),
      [POST_METRICS]: { status, data: { detail } },
    })
    await screen.findByText('No analysis yet.')
    fireEvent.click(button())
    expect((await screen.findByRole('alert')).textContent).toBe(detail)
  })

  it('shows all seven summary values', async () => {
    renderPanel({
      [GET_METRICS]: metrics({ job: job('completed'), summary: SUMMARY, files: [] }),
    })
    const table = within(await screen.findByRole('table', { name: 'Code metrics summary' }))
    const rows = table.getAllByRole('row').map((row) => [
      within(row).getByRole('rowheader').textContent,
      within(row).getByRole('cell').textContent,
    ])
    expect(rows).toEqual([
      ['Files', '6'],
      ['Analyzed', '3'],
      ['Binary', '1'],
      ['Too large', '1'],
      ['Unavailable', '1'],
      ['Total lines', '120'],
      ['Blank lines', '17'],
    ])
  })

  it('lists the top 10 files by lines, ties by path, skipping files without a count', async () => {
    const files = [
      metric('b.py', 50),
      metric('a.py', 50), // tie with b.py: path ascending
      metric('Z.py', 50), // uppercase sorts before lowercase (code-point order)
      metric('big.bin', null),
      metric('gone.py', null, { status: 'unavailable' }),
      ...Array.from({ length: 9 }, (_, i) => metric(`f${i}.py`, i + 1)),
    ]
    renderPanel({ [GET_METRICS]: metrics({ job: job('completed'), summary: SUMMARY, files }) })
    const table = within(await screen.findByRole('table', { name: 'Largest files' }))
    const paths = table
      .getAllByRole('row')
      .slice(1)
      .map((row) => (row as HTMLTableRowElement).cells[0].textContent)
    expect(paths).toEqual(['Z.py', 'a.py', 'b.py', 'f8.py', 'f7.py', 'f6.py', 'f5.py', 'f4.py', 'f3.py', 'f2.py'])
    expect(paths).not.toContain('big.bin')
    expect(largestFiles(files).map((f) => f.path)).toEqual(paths)
  })

  it('shows the fixed message of a failed analysis', async () => {
    const message = 'Repository changed during analysis; run it again'
    renderPanel({
      [GET_METRICS]: metrics({ job: job('failed', { error_message: message }) }),
    })
    expect((await screen.findByRole('alert')).textContent).toBe(message)
    expect(screen.getByText('Run the analysis for the current files.')).toBeTruthy()
    expect(button().disabled).toBe(false)
  })

  it('asks for a new analysis when the metrics were cleared by re-ingestion', async () => {
    renderPanel({ [GET_METRICS]: metrics({ job: job('completed'), summary: null, files: [] }) })
    expect(await screen.findByText('Run the analysis for the current files.')).toBeTruthy()
    expect(screen.queryByRole('table')).toBeNull()
  })

  it('renders file paths as text only', async () => {
    const path = '<img src=x onerror=alert(1)>.py'
    renderPanel({
      [GET_METRICS]: metrics({ job: job('completed'), summary: SUMMARY, files: [metric(path, 3)] }),
    })
    expect(await screen.findByText(path)).toBeTruthy()
    expect(document.querySelector('img, script')).toBeNull()
  })
})

describe('JobProgress job types', () => {
  it('labels code analysis and ingestion jobs', async () => {
    mockApi({
      [JOBS_KEY]: {
        status: 200,
        data: [
          job('completed', { id: 'j2' }),
          job('completed', { id: 'j1', job_type: 'ingestion' }),
        ],
      },
    })
    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <JobProgress projectId={P} repositoryId={R} />
      </QueryClientProvider>,
    )
    expect((await screen.findByTestId('job-type')).textContent).toBe('Code analysis')
    const recent = within(screen.getByLabelText('Recent jobs'))
    expect(recent.getByText('Ingestion')).toBeTruthy()
  })
})

describe('refresh after an analysis finishes', () => {
  it('updates the panel through the existing job polling', async () => {
    let jobCalls = 0
    let finished = false
    mockApi({
      [JOBS_KEY]: () => {
        jobCalls += 1
        finished = jobCalls >= 3
        return { status: 200, data: [job(finished ? 'completed' : 'processing')] }
      },
      [GET_METRICS]: () =>
        finished
          ? metrics({ job: job('completed'), summary: SUMMARY, files: [metric('a.py', 9)] })
          : metrics({ job: job('processing') }),
    })
    const queryClient = createTestQueryClient()
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    render(
      <QueryClientProvider client={queryClient}>
        <JobProgress projectId={P} repositoryId={R} pollIntervalMs={20} />
        <CodeMetricsPanel projectId={P} repositoryId={R} repositoryStatus="ready" />
      </QueryClientProvider>,
    )
    expect(await screen.findByText('Analysis in progress…')).toBeTruthy()
    expect(await screen.findByRole('table', { name: 'Code metrics summary' })).toBeTruthy()
    expect(screen.queryByText('Analysis in progress…')).toBeNull()
    expect(button().disabled).toBe(false)
    // useJobs refreshes the code-metrics query explicitly when the job finishes.
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: queryKeys.codeMetrics(P, R),
      exact: true,
    })
  })
})

describe('RepositoryPage integration', () => {
  it('shows the code metrics panel next to the existing panels', async () => {
    mockApi({
      [`GET /projects/${P}/repositories/${R}`]: {
        status: 200,
        data: { ...ZIP_REPOSITORY, status: 'ready' },
      },
      [`GET /projects/${P}/repositories/${R}/tree`]: {
        status: 200,
        data: {
          repository_id: R,
          status: 'ready',
          file_count: 0,
          total_bytes: 0,
          tree: { type: 'directory', name: '', path: '', children: [] },
        },
      },
      [`GET /projects/${P}/repositories/${R}/languages`]: {
        status: 200,
        data: { repository_id: R, status: 'ready', total_files: 0, total_bytes: 0, languages: [] },
      },
      [JOBS_KEY]: { status: 200, data: [] },
      [GET_METRICS]: metrics({ job: job('completed'), summary: SUMMARY, files: [metric('a.py', 9)] }),
    })
    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <MemoryRouter initialEntries={[`/projects/${P}/repositories/${R}`]}>
          <Routes>
            <Route
              path="/projects/:projectId/repositories/:repositoryId"
              element={<RepositoryPage />}
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
    const panel = within(await screen.findByRole('region', { name: 'Code metrics' }))
    expect(await panel.findByRole('table', { name: 'Code metrics summary' })).toBeTruthy()
    expect((panel.getByRole('button', { name: 'Analyze code' }) as HTMLButtonElement).disabled).toBe(
      false,
    )
    for (const name of ['Files', 'Languages', 'File']) {
      expect(screen.getByRole('region', { name })).toBeTruthy()
    }
    expect(screen.getByText('Select a file to view its content.')).toBeTruthy()
  })
})
