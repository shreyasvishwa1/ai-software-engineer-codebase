import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import { queryKeys } from '../hooks/queryKeys'
import { RepositoryPage } from '../pages/RepositoryPage'
import { mockApi, type MockResponse, PROJECT, ZIP_REPOSITORY } from '../test/mockApi'
import { createTestQueryClient } from '../test/render'
import type { AnalysisJob, DependencyInventory, DependencyManifest } from '../types/api'
import { DependenciesPanel, TRUNCATED_NOTICE } from './DependenciesPanel'
import { JobProgress } from './JobProgress'

const P = PROJECT.id
const R = ZIP_REPOSITORY.id
const DEPS_PATH = `/projects/${P}/repositories/${R}/analysis/dependencies`
const GET_DEPS = `GET ${DEPS_PATH}`
const JOBS_KEY = `GET /projects/${P}/repositories/${R}/jobs`

const PYPROJECT: DependencyManifest = {
  path: 'backend/pyproject.toml',
  ecosystem: 'python',
  status: 'parsed',
  dependencies: [
    { name: 'pytest', specifier: '>=8', scope: 'optional:dev' },
    { name: 'fastapi', specifier: '==0.110.0', scope: 'runtime' },
  ],
}
const PACKAGE: DependencyManifest = {
  path: 'frontend/package.json',
  ecosystem: 'npm',
  status: 'parsed',
  dependencies: [{ name: 'react', specifier: '^18.2.0', scope: 'runtime' }],
}

function inventory(extra: Partial<DependencyInventory> = {}): DependencyInventory {
  return {
    manifests: [PYPROJECT, PACKAGE],
    summary: { manifests: 2, parsed: 2, invalid: 0, too_large: 0, unavailable: 0, dependencies: 3 },
    truncated: false,
    ...extra,
  }
}

function ok(data: DependencyInventory): MockResponse {
  return { status: 200, data }
}

function renderPanel(route: MockResponse | (() => MockResponse)) {
  const requests = mockApi({ [GET_DEPS]: route })
  const view = render(
    <QueryClientProvider client={createTestQueryClient()}>
      <DependenciesPanel projectId={P} repositoryId={R} />
    </QueryClientProvider>,
  )
  return { requests, ...view }
}

const manifestRows = () => within(screen.getByRole('list', { name: 'Manifests' })).getAllByRole('listitem')
const toggle = (path: string) => screen.getByRole('button', { name: new RegExp(path.replace('.', '\\.')) })

describe('DependenciesPanel states', () => {
  it('shows a loading state, then the summary', async () => {
    renderPanel(ok(inventory()))
    expect(screen.getByText('Loading dependencies…')).toBeTruthy()
    expect(await screen.findByRole('table', { name: 'Dependency summary' })).toBeTruthy()
  })

  it('shows the server error detail', async () => {
    renderPanel({ status: 404, data: { detail: 'Repository not found' } })
    expect((await screen.findByRole('alert')).textContent).toBe('Repository not found')
  })

  it('shows when there are no manifests', async () => {
    renderPanel(
      ok(
        inventory({
          manifests: [],
          summary: { manifests: 0, parsed: 0, invalid: 0, too_large: 0, unavailable: 0, dependencies: 0 },
        }),
      ),
    )
    expect(await screen.findByText('No dependency manifests found.')).toBeTruthy()
    expect(screen.queryByRole('table')).toBeNull()
  })

  it('shows all six summary values', async () => {
    renderPanel(
      ok(
        inventory({
          summary: { manifests: 7, parsed: 3, invalid: 2, too_large: 1, unavailable: 1, dependencies: 41 },
        }),
      ),
    )
    const table = within(await screen.findByRole('table', { name: 'Dependency summary' }))
    const rows = table.getAllByRole('row').map((row) => [
      within(row).getByRole('rowheader').textContent,
      within(row).getByRole('cell').textContent,
    ])
    expect(rows).toEqual([
      ['Manifests', '7'],
      ['Parsed', '3'],
      ['Invalid', '2'],
      ['Too large', '1'],
      ['Unavailable', '1'],
      ['Dependencies', '41'],
    ])
  })

  it.each([true, false])('shows the truncation notice only when truncated=%s', async (truncated) => {
    renderPanel(ok(inventory({ truncated })))
    await screen.findByRole('table', { name: 'Dependency summary' })
    expect(screen.queryByText(TRUNCATED_NOTICE) !== null).toBe(truncated)
    expect(TRUNCATED_NOTICE).toBe(
      'Showing a limited set (at most 50 manifests and 5,000 dependencies).',
    )
  })
})

describe('Manifest list', () => {
  it('lists manifests in server order with path, ecosystem, status and count', async () => {
    // Deliberately not alphabetical: the client must not re-sort.
    renderPanel(ok(inventory({ manifests: [PACKAGE, PYPROJECT] })))
    await screen.findByRole('list', { name: 'Manifests' })
    const rows = manifestRows()
    expect(rows.map((row) => row.textContent)).toEqual([
      expect.stringContaining('frontend/package.json'),
      expect.stringContaining('backend/pyproject.toml'),
    ])
    expect(rows[0].textContent).toContain('npm')
    expect(rows[0].textContent).toContain('parsed')
    expect(rows[0].textContent).toContain('1 dependency')
    expect(rows[1].textContent).toContain('Python')
    expect(rows[1].textContent).toContain('2 dependencies')
  })

  it('keeps parsed manifests collapsed with no dependency table until expanded', async () => {
    renderPanel(ok(inventory()))
    await screen.findByRole('list', { name: 'Manifests' })
    expect(toggle('backend/pyproject.toml').getAttribute('aria-expanded')).toBe('false')
    expect(toggle('frontend/package.json').getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByRole('table', { name: /^Dependencies of/ })).toBeNull()
  })

  it('expands one manifest exactly as received, and collapses it again', async () => {
    renderPanel(ok(inventory()))
    await screen.findByRole('list', { name: 'Manifests' })
    fireEvent.click(toggle('backend/pyproject.toml'))

    expect(toggle('backend/pyproject.toml').getAttribute('aria-expanded')).toBe('true')
    const table = within(screen.getByRole('table', { name: 'Dependencies of backend/pyproject.toml' }))
    const rows = table
      .getAllByRole('row')
      .slice(1)
      .map((row) => Array.from((row as HTMLTableRowElement).cells).map((cell) => cell.textContent))
    expect(rows).toEqual([
      ['pytest', '>=8', 'optional:dev'],
      ['fastapi', '==0.110.0', 'runtime'],
    ]) // server order, values unchanged
    // The other manifest stays collapsed.
    expect(toggle('frontend/package.json').getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByRole('table', { name: 'Dependencies of frontend/package.json' })).toBeNull()

    fireEvent.click(toggle('backend/pyproject.toml'))
    expect(screen.queryByRole('table', { name: /^Dependencies of/ })).toBeNull()
  })

  it.each([
    ['invalid', 'Could not be parsed.'],
    ['too_large', 'Too large to analyze (limit 1 MiB).'],
    ['unavailable', 'Not available right now; try again later.'],
  ] as const)('shows the fixed message for %s manifests, with no toggle', async (status, message) => {
    const manifest: DependencyManifest = {
      path: 'broken/package.json',
      ecosystem: 'npm',
      status,
      dependencies: [],
    }
    renderPanel(ok(inventory({ manifests: [manifest] })))
    expect(await screen.findByText(message)).toBeTruthy()
    expect(screen.queryByRole('button')).toBeNull()
    expect(screen.queryByRole('table', { name: /^Dependencies of/ })).toBeNull()
  })

  it('renders untrusted values as plain text, never HTML or links', async () => {
    const manifest: DependencyManifest = {
      path: '<img src=x onerror=alert(1)>/requirements.txt',
      ecosystem: 'python',
      status: 'parsed',
      dependencies: [
        { name: '<script>alert(1)</script>', specifier: '@ https://example.com/package', scope: '<b>x</b>' },
      ],
    }
    const { container } = renderPanel(ok(inventory({ manifests: [manifest] })))
    fireEvent.click(await screen.findByRole('button', { name: /requirements\.txt/ }))
    expect(screen.getByText('<script>alert(1)</script>')).toBeTruthy()
    expect(screen.getByText('@ https://example.com/package')).toBeTruthy()
    expect(screen.getByText('<b>x</b>')).toBeTruthy()
    expect(container.querySelector('img, script, a, b')).toBeNull()
  })
})

describe('Requests', () => {
  it('makes exactly one request to the endpoint and does not poll', async () => {
    let calls = 0
    const { requests } = renderPanel(() => {
      calls += 1
      return ok(inventory())
    })
    await screen.findByRole('table', { name: 'Dependency summary' })
    await new Promise((resolve) => setTimeout(resolve, 150))
    expect(calls).toBe(1)
    expect(requests).toHaveLength(1)
    expect(requests[0].method).toBe('GET')
    expect(requests[0].url).toBe(DEPS_PATH)
  })

  it('refreshes the inventory when an ingestion job finishes', async () => {
    let jobCalls = 0
    let depsCalls = 0
    const job = (status: AnalysisJob['status']): AnalysisJob => ({
      id: 'job-1',
      project_id: P,
      repository_id: R,
      job_type: 'ingestion',
      status,
      progress: status === 'completed' ? 100 : 0,
      error_message: null,
      created_at: '2026-10-02T00:00:00Z',
      started_at: null,
      completed_at: null,
    })
    mockApi({
      [JOBS_KEY]: () => {
        jobCalls += 1
        return { status: 200, data: [job(jobCalls >= 3 ? 'completed' : 'processing')] }
      },
      [GET_DEPS]: () => {
        depsCalls += 1
        return ok(depsCalls === 1 ? inventory() : inventory({ manifests: [PACKAGE] }))
      },
    })
    const queryClient = createTestQueryClient()
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    render(
      <QueryClientProvider client={queryClient}>
        <JobProgress projectId={P} repositoryId={R} pollIntervalMs={20} />
        <DependenciesPanel projectId={P} repositoryId={R} />
      </QueryClientProvider>,
    )
    await screen.findByRole('button', { name: /backend\/pyproject\.toml/ })
    await waitFor(() => expect(screen.getByTestId('status-badge').textContent).toBe('completed'))
    await waitFor(() => expect(screen.queryByRole('button', { name: /backend\/pyproject\.toml/ })).toBeNull())
    expect(depsCalls).toBeGreaterThanOrEqual(2) // refreshed (two refresh paths may overlap)
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: queryKeys.dependencies(P, R),
      exact: true,
    })
  })
})

describe('RepositoryPage integration', () => {
  it('places Dependencies between Code metrics and File, keeping the other panels', async () => {
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
      [`GET /projects/${P}/repositories/${R}/analysis/code-metrics`]: {
        status: 200,
        data: { job: null, summary: null, files: [] },
      },
      [GET_DEPS]: ok(inventory()),
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
    const deps = await screen.findByRole('region', { name: 'Dependencies' })
    expect(await within(deps).findByRole('table', { name: 'Dependency summary' })).toBeTruthy()
    const metrics = screen.getByRole('region', { name: 'Code metrics' })
    const file = screen.getByRole('region', { name: 'File' })
    const FOLLOWING = Node.DOCUMENT_POSITION_FOLLOWING
    expect(metrics.compareDocumentPosition(deps) & FOLLOWING).toBeTruthy()
    expect(deps.compareDocumentPosition(file) & FOLLOWING).toBeTruthy()
    expect(within(metrics).getByText('No analysis yet.')).toBeTruthy()
    expect(within(file).getByText('Select a file to view its content.')).toBeTruthy()
    for (const name of ['Files', 'Languages']) {
      expect(screen.getByRole('region', { name })).toBeTruthy()
    }
  })
})
