import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { InternalAxiosRequestConfig } from 'axios'
import type { ReactElement } from 'react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { RepositoryPage } from '../pages/RepositoryPage'
import { mockApi, type MockResponse, PROJECT, ZIP_REPOSITORY } from '../test/mockApi'
import { createTestQueryClient } from '../test/render'
import type {
  AnalysisJob,
  CodeMetrics,
  DependencyInventory,
  DependencyManifest,
  TreeDirectory,
} from '../types/api'
import { CodeMetricsPanel } from './CodeMetricsPanel'
import { DependenciesPanel } from './DependenciesPanel'

const P = PROJECT.id
const R = ZIP_REPOSITORY.id
const BASE = `/projects/${P}/repositories/${R}`
const HTML_PATH = '<img src=x onerror=alert(1)>.py'

const COMPLETED: AnalysisJob = {
  id: 'job-1',
  project_id: P,
  repository_id: R,
  job_type: 'code_metrics',
  status: 'completed',
  progress: 100,
  error_message: null,
  created_at: '2026-10-02T00:00:00Z',
  started_at: null,
  completed_at: null,
}

const METRICS: CodeMetrics = {
  job: COMPLETED,
  summary: {
    files: 3,
    analyzed: 3,
    binary: 0,
    too_large: 0,
    unavailable: 0,
    total_lines: 60,
    blank_lines: 6,
  },
  files: [
    { path: 'src/app.py', language: 'Python', status: 'analyzed', line_count: 40, blank_line_count: 4 },
    { path: HTML_PATH, language: 'Python', status: 'analyzed', line_count: 15, blank_line_count: 1 },
    { path: 'README.md', language: 'Markdown', status: 'analyzed', line_count: 5, blank_line_count: 1 },
  ],
}

const MANIFESTS: DependencyManifest[] = [
  {
    path: 'requirements.txt',
    ecosystem: 'python',
    status: 'parsed',
    dependencies: [{ name: 'flask', specifier: '==3.0', scope: 'runtime' }],
  },
  { path: 'broken/package.json', ecosystem: 'npm', status: 'invalid', dependencies: [] },
  { path: 'big/requirements.txt', ecosystem: 'python', status: 'too_large', dependencies: [] },
  { path: 'gone/pyproject.toml', ecosystem: 'python', status: 'unavailable', dependencies: [] },
]

const INVENTORY: DependencyInventory = {
  manifests: MANIFESTS,
  summary: { manifests: 4, parsed: 1, invalid: 1, too_large: 1, unavailable: 1, dependencies: 1 },
  truncated: false,
}

const TREE: TreeDirectory = {
  type: 'directory',
  name: '',
  path: '',
  children: [
    {
      type: 'directory',
      name: 'src',
      path: 'src',
      children: [
        { type: 'file', name: 'app.py', path: 'src/app.py', extension: 'py', language: 'Python', size_bytes: 40 },
      ],
    },
    { type: 'file', name: 'README.md', path: 'README.md', extension: 'md', language: 'Markdown', size_bytes: 5 },
    { type: 'file', name: 'requirements.txt', path: 'requirements.txt', extension: 'txt', language: 'Text', size_bytes: 9 },
  ],
}

function withClient(ui: ReactElement) {
  return render(<QueryClientProvider client={createTestQueryClient()}>{ui}</QueryClientProvider>)
}

const viewLabel = (path: string) => `Open ${path} in the viewer`

// --- Code metrics panel ------------------------------------------------------------------

describe('CodeMetricsPanel file links', () => {
  it('turns largest-file paths into buttons that pass the exact path', async () => {
    mockApi({ [`GET ${BASE}/analysis/code-metrics`]: { status: 200, data: METRICS } })
    const onSelectFile = vi.fn()
    const { container } = withClient(
      <CodeMetricsPanel projectId={P} repositoryId={R} repositoryStatus="ready" onSelectFile={onSelectFile} />,
    )
    const table = within(await screen.findByRole('table', { name: 'Largest files' }))
    for (const file of METRICS.files) {
      expect(table.getByRole('button', { name: viewLabel(file.path) }).textContent).toBe(file.path)
    }
    fireEvent.click(table.getByRole('button', { name: viewLabel(HTML_PATH) }))
    expect(onSelectFile).toHaveBeenCalledWith(HTML_PATH) // exact, untransformed
    expect(container.querySelector('img, script, a')).toBeNull() // HTML-looking path is text
  })

  it('keeps paths as plain text without onSelectFile', async () => {
    mockApi({ [`GET ${BASE}/analysis/code-metrics`]: { status: 200, data: METRICS } })
    withClient(<CodeMetricsPanel projectId={P} repositoryId={R} repositoryStatus="ready" />)
    const table = within(await screen.findByRole('table', { name: 'Largest files' }))
    expect(table.queryAllByRole('button')).toHaveLength(0)
    expect(table.getByText('src/app.py')).toBeTruthy()
  })
})

// --- Dependencies panel ------------------------------------------------------------------

describe('DependenciesPanel file links', () => {
  it('gives every manifest, of any status, a View file button with the exact path', async () => {
    mockApi({ [`GET ${BASE}/analysis/dependencies`]: { status: 200, data: INVENTORY } })
    const onSelectFile = vi.fn()
    withClient(<DependenciesPanel projectId={P} repositoryId={R} onSelectFile={onSelectFile} />)
    await screen.findByRole('list', { name: 'Manifests' })
    for (const manifest of MANIFESTS) {
      const button = screen.getByRole('button', { name: viewLabel(manifest.path) })
      expect(button.textContent).toBe('View file')
      expect(button.closest('[aria-expanded]')).toBeNull() // never nested in the toggle
      fireEvent.click(button)
      expect(onSelectFile).toHaveBeenLastCalledWith(manifest.path)
    }
    expect(onSelectFile).toHaveBeenCalledTimes(MANIFESTS.length)
  })

  it('keeps View file and expansion independent', async () => {
    mockApi({ [`GET ${BASE}/analysis/dependencies`]: { status: 200, data: INVENTORY } })
    const onSelectFile = vi.fn()
    withClient(<DependenciesPanel projectId={P} repositoryId={R} onSelectFile={onSelectFile} />)
    await screen.findByRole('list', { name: 'Manifests' })
    // Only the parsed manifest has an expand toggle (the button with aria-expanded).
    const toggle = () =>
      screen.getAllByRole('button').find((b) => b.hasAttribute('aria-expanded'))
    const table = () => screen.queryByRole('table', { name: 'Dependencies of requirements.txt' })

    fireEvent.click(screen.getByRole('button', { name: viewLabel('requirements.txt') }))
    expect(toggle()?.getAttribute('aria-expanded')).toBe('false') // not toggled
    expect(table()).toBeNull()

    fireEvent.click(toggle() as HTMLElement)
    expect(toggle()?.getAttribute('aria-expanded')).toBe('true')
    expect(table()).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: viewLabel('requirements.txt') }))
    expect(toggle()?.getAttribute('aria-expanded')).toBe('true') // still expanded
    expect(onSelectFile).toHaveBeenCalledTimes(2)
  })

  it('has no View file buttons without onSelectFile', async () => {
    mockApi({ [`GET ${BASE}/analysis/dependencies`]: { status: 200, data: INVENTORY } })
    withClient(<DependenciesPanel projectId={P} repositoryId={R} />)
    await screen.findByRole('list', { name: 'Manifests' })
    expect(screen.queryAllByRole('button', { name: /in the viewer$/ })).toHaveLength(0)
    expect(screen.queryByText('View file')).toBeNull()
  })
})

// --- Repository page ---------------------------------------------------------------------

const scrollSpy = vi.fn()

function stubScroll() {
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
    configurable: true,
    writable: true,
    value: scrollSpy,
  })
}

afterEach(() => {
  scrollSpy.mockReset()
  // jsdom has no scrollIntoView: remove the stub so other tests see the real environment.
  delete (HTMLElement.prototype as { scrollIntoView?: unknown }).scrollIntoView
})

function renderPage() {
  const contentPaths: string[] = []
  const routes: Record<string, MockResponse | ((c: InternalAxiosRequestConfig) => MockResponse)> = {
    [`GET ${BASE}`]: { status: 200, data: { ...ZIP_REPOSITORY, status: 'ready' } },
    [`GET ${BASE}/tree`]: {
      status: 200,
      data: { repository_id: R, status: 'ready', file_count: 3, total_bytes: 54, tree: TREE },
    },
    [`GET ${BASE}/languages`]: {
      status: 200,
      data: { repository_id: R, status: 'ready', total_files: 3, total_bytes: 54, languages: [] },
    },
    [`GET ${BASE}/jobs`]: { status: 200, data: [] },
    [`GET ${BASE}/analysis/code-metrics`]: { status: 200, data: METRICS },
    [`GET ${BASE}/analysis/dependencies`]: { status: 200, data: INVENTORY },
    [`GET ${BASE}/files/content`]: (config) => {
      const path = (config.params as { path: string }).path
      contentPaths.push(path)
      return {
        status: 200,
        data: { path, language: null, size_bytes: 3, content: `content of ${path}`, reason: null },
      }
    },
  }
  const requests = mockApi(routes)
  render(
    <QueryClientProvider client={createTestQueryClient()}>
      <MemoryRouter initialEntries={[BASE]}>
        <Routes>
          <Route path="/projects/:projectId/repositories/:repositoryId" element={<RepositoryPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return { contentPaths, requests }
}

const fileCard = () => screen.getByRole('region', { name: 'File' })
const viewerCode = () => fileCard().querySelector('pre code')?.textContent

describe('RepositoryPage navigation', () => {
  it('opens a largest file in the viewer and scrolls to it', async () => {
    stubScroll()
    const { contentPaths, requests } = renderPage()
    fireEvent.click(await screen.findByRole('button', { name: viewLabel('src/app.py') }))

    await waitFor(() => expect(viewerCode()).toBe('content of src/app.py'))
    expect(contentPaths).toEqual(['src/app.py'])
    const contentRequests = requests.filter((r) => r.url.endsWith('/files/content'))
    expect(contentRequests.map((r) => r.url)).toEqual([`${BASE}/files/content`]) // path not in URL
    expect(scrollSpy).toHaveBeenCalledTimes(1)
    expect(scrollSpy.mock.contexts[0]).toBe(fileCard())
    expect(scrollSpy).toHaveBeenCalledWith({ behavior: 'smooth', block: 'start' })
    // The tree marks the same file (its folder is open at the top level).
    const tree = within(screen.getByRole('navigation', { name: 'File tree' }))
    expect(tree.getByRole('button', { name: /app\.py/ }).getAttribute('aria-current')).toBe('true')
  })

  it('opens a manifest from the Dependencies panel, whatever its status', async () => {
    stubScroll()
    const { contentPaths } = renderPage()
    fireEvent.click(await screen.findByRole('button', { name: viewLabel('broken/package.json') }))
    await waitFor(() => expect(viewerCode()).toBe('content of broken/package.json'))
    fireEvent.click(screen.getByRole('button', { name: viewLabel('requirements.txt') }))
    await waitFor(() => expect(viewerCode()).toBe('content of requirements.txt'))
    expect(contentPaths).toEqual(['broken/package.json', 'requirements.txt'])
    expect(scrollSpy).toHaveBeenCalledTimes(2)
  })

  it('passes an HTML-looking path through unchanged and renders it as text', async () => {
    stubScroll()
    const { contentPaths } = renderPage()
    fireEvent.click(await screen.findByRole('button', { name: viewLabel(HTML_PATH) }))
    await waitFor(() => expect(viewerCode()).toBe(`content of ${HTML_PATH}`))
    expect(contentPaths).toEqual([HTML_PATH])
    expect(document.querySelector('img, script')).toBeNull()
  })

  it('keeps tree selection working, without the panel scroll', async () => {
    stubScroll()
    const { contentPaths } = renderPage()
    fireEvent.click(await screen.findByRole('button', { name: viewLabel('src/app.py') }))
    await waitFor(() => expect(viewerCode()).toBe('content of src/app.py'))
    scrollSpy.mockClear()

    const tree = within(screen.getByRole('navigation', { name: 'File tree' }))
    fireEvent.click(tree.getByRole('button', { name: /README\.md/ }))
    await waitFor(() => expect(viewerCode()).toBe('content of README.md'))
    expect(tree.getByRole('button', { name: /README\.md/ }).getAttribute('aria-current')).toBe('true')
    expect(scrollSpy).not.toHaveBeenCalled() // tree selection does not scroll
    expect(contentPaths).toEqual(['src/app.py', 'README.md'])
  })

  it('works where scrollIntoView does not exist', async () => {
    // No stub: jsdom has no scrollIntoView; the guard must make this a no-op.
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: viewLabel('README.md') }))
    await waitFor(() => expect(viewerCode()).toBe('content of README.md'))
  })
})
