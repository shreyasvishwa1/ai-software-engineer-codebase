import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { InternalAxiosRequestConfig } from 'axios'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { RepositoryPage } from '../pages/RepositoryPage'
import { mockApi, type MockResponse, PROJECT, ZIP_REPOSITORY } from '../test/mockApi'
import { createTestQueryClient } from '../test/render'
import type {
  AnalysisJob,
  CodeMetrics,
  CodeSearchResult,
  SecretScan,
  TreeDirectory,
} from '../types/api'
import { CodeSearchPanel } from './CodeSearchPanel'
import { FileViewer, type LineSelection } from './FileViewer'
import { SecretsPanel } from './SecretsPanel'

const P = PROJECT.id
const R = ZIP_REPOSITORY.id
const BASE = `/projects/${P}/repositories/${R}`
const CONTENT_KEY = `GET ${BASE}/files/content`
const HTML = '<img src=x onerror=alert(1)>'

const FIVE_LINES = 'first line\nsecond line\nthird line\nfourth line\nfifth line'

function contentRoute(contents: Record<string, string>, seen: string[] = []) {
  return (config: InternalAxiosRequestConfig): MockResponse => {
    const path = (config.params as { path: string }).path
    seen.push(path)
    const content = contents[path] ?? `content of ${path}`
    return {
      status: 200,
      data: { path, language: null, size_bytes: content.length, content, reason: null },
    }
  }
}

function renderViewer(content: string, selectedLine: LineSelection | null, path = 'src/app.py') {
  const seen: string[] = []
  mockApi({ [CONTENT_KEY]: contentRoute({ [path]: content }, seen) })
  const view = render(
    <QueryClientProvider client={createTestQueryClient()}>
      <FileViewer projectId={P} repositoryId={R} selectedPath={path} selectedLine={selectedLine} />
    </QueryClientProvider>,
  )
  return { seen, ...view }
}

async function codeElement(container: HTMLElement): Promise<HTMLElement> {
  return waitFor(() => {
    const element = container.querySelector('pre code')
    expect(element).toBeTruthy()
    return element as HTMLElement
  })
}

const highlighted = () => screen.queryByTestId('selected-line')

// --- Layout stubs (jsdom has no layout): the selected line sits 400px into the code box. ---

const scrollTopSpy = vi.fn()

function stubLayout() {
  Object.defineProperty(HTMLSpanElement.prototype, 'offsetTop', {
    configurable: true,
    get(this: HTMLElement) {
      return this.dataset.testid === 'selected-line' ? 400 : 0
    },
  })
  Object.defineProperty(HTMLPreElement.prototype, 'clientHeight', { configurable: true, get: () => 300 })
  Object.defineProperty(HTMLPreElement.prototype, 'scrollTop', {
    configurable: true,
    get: () => 0,
    set: scrollTopSpy,
  })
}

const scrollIntoViewSpy = vi.fn()

beforeEach(() => {
  stubLayout()
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
    configurable: true,
    writable: true,
    value: scrollIntoViewSpy,
  })
})

afterEach(() => {
  scrollTopSpy.mockReset()
  scrollIntoViewSpy.mockReset()
  delete (HTMLSpanElement.prototype as { offsetTop?: unknown }).offsetTop
  delete (HTMLPreElement.prototype as { clientHeight?: unknown }).clientHeight
  delete (HTMLPreElement.prototype as { scrollTop?: unknown }).scrollTop
  delete (HTMLElement.prototype as { scrollIntoView?: unknown }).scrollIntoView
})

// --- FileViewer -----------------------------------------------------------------------------

describe('FileViewer line highlighting', () => {
  it('highlights exactly the selected line and keeps the text identical', async () => {
    const { container, seen } = renderViewer(FIVE_LINES, { line: 3 })
    const code = await codeElement(container)
    expect(code.textContent).toBe(FIVE_LINES)
    expect(highlighted()?.textContent).toBe('third line')
    expect(screen.getAllByTestId('selected-line')).toHaveLength(1)
    expect(highlighted()?.className).toBe('bg-amber-400/20')
    expect(code.childNodes).toHaveLength(3) // before, line, after: never one node per line
    expect(screen.getByText('Showing line 3')).toBeTruthy()
    expect(screen.queryByText(/is not in this file/)).toBeNull()
    expect(seen).toEqual(['src/app.py']) // one content request, as before
  })

  it.each([
    [1, 'first line'],
    [5, 'fifth line'],
  ])('highlights line %i', async (line, text) => {
    const { container } = renderViewer(FIVE_LINES, { line })
    const code = await codeElement(container)
    expect(highlighted()?.textContent).toBe(text)
    expect(code.textContent).toBe(FIVE_LINES)
  })

  it('counts a trailing empty line like the server does', async () => {
    const content = 'alpha\nbeta\n'
    const { container } = renderViewer(content, { line: 3 })
    const code = await codeElement(container)
    expect(highlighted()?.textContent).toBe('')
    expect(code.textContent).toBe(content)
  })

  it('keeps CRLF content byte-for-byte with the same numbering', async () => {
    const content = 'one\r\ntwo\r\nthree\r\n'
    const { container } = renderViewer(content, { line: 2 })
    const code = await codeElement(container)
    expect(highlighted()?.textContent).toBe('two\r')
    expect(code.textContent).toBe(content)
  })

  it('reports a line beyond the end without highlighting anything', async () => {
    const { container } = renderViewer(FIVE_LINES, { line: 6 })
    const code = await codeElement(container)
    expect(screen.getByText('Line 6 is not in this file.')).toBeTruthy()
    expect(highlighted()).toBeNull()
    expect(screen.queryByText(/^Showing line/)).toBeNull()
    expect(code.childNodes).toHaveLength(1)
    expect(code.textContent).toBe(FIVE_LINES)
    expect(scrollTopSpy).not.toHaveBeenCalled()
  })

  it.each([0, -1, 2.5, Number.NaN, Number.POSITIVE_INFINITY, '3' as unknown as number])(
    'ignores the invalid line %s',
    async (line) => {
      const { container } = renderViewer(FIVE_LINES, { line })
      const code = await codeElement(container)
      expect(highlighted()).toBeNull()
      expect(screen.queryByText(/not in this file|Showing line/)).toBeNull()
      expect(code.childNodes).toHaveLength(1)
      expect(code.textContent).toBe(FIVE_LINES)
    },
  )

  it('renders a single text node when no line is selected', async () => {
    const { container } = renderViewer(FIVE_LINES, null)
    const code = await codeElement(container)
    expect(code.childNodes).toHaveLength(1)
    expect(code.firstChild?.nodeType).toBe(Node.TEXT_NODE)
    expect(code.textContent).toBe(FIVE_LINES)
    expect(highlighted()).toBeNull()
    expect(scrollTopSpy).not.toHaveBeenCalled()
  })

  it('highlights a malicious line as literal text', async () => {
    const content = `safe\n${HTML}<script>alert(1)</script><a href="https://example.invalid">x</a>\nend`
    const { container } = renderViewer(content, { line: 2 })
    const code = await codeElement(container)
    expect(highlighted()?.textContent).toBe(`${HTML}<script>alert(1)</script><a href="https://example.invalid">x</a>`)
    expect(code.textContent).toBe(content)
    expect(document.querySelector('img, script, a, iframe')).toBeNull()
    expect(document.querySelector('[href], [src], [onerror]')).toBeNull()
  })

  it('scrolls only the code box to the line, never the page', async () => {
    const { container } = renderViewer(FIVE_LINES, { line: 3 })
    await codeElement(container)
    await waitFor(() => expect(scrollTopSpy).toHaveBeenCalled())
    expect(scrollTopSpy).toHaveBeenLastCalledWith(300) // 400 - 0 - 300 / 3
    expect(scrollIntoViewSpy).not.toHaveBeenCalled()
  })

  it('scrolls again for a fresh selection of the same line', async () => {
    mockApi({ [CONTENT_KEY]: contentRoute({ 'src/app.py': FIVE_LINES }) })
    const client = createTestQueryClient()
    const ui = (selectedLine: LineSelection) => (
      <QueryClientProvider client={client}>
        <FileViewer projectId={P} repositoryId={R} selectedPath="src/app.py" selectedLine={selectedLine} />
      </QueryClientProvider>
    )
    const { rerender } = render(ui({ line: 3 }))
    await waitFor(() => expect(scrollTopSpy).toHaveBeenCalledTimes(1))
    rerender(ui({ line: 3 }))
    await waitFor(() => expect(scrollTopSpy).toHaveBeenCalledTimes(2))
  })
})

// --- Panels --------------------------------------------------------------------------------

const SCAN: SecretScan = {
  findings: [{ path: 'a/app.py', line: 4, rule: 'private-key', description: 'Private key' }],
  summary: { files_scanned: 1, binary: 0, too_large: 0, unavailable: 0, findings: 1 },
  truncated: false,
}

const SEARCH: CodeSearchResult = {
  matches: [{ path: 'a/app.py', line: 3, preview: 'third line' }],
  summary: { files_scanned: 2, binary: 0, too_large: 0, unavailable: 0, matches: 1 },
  truncated: false,
}

describe('Panels with onSelectLine', () => {
  it('Secrets passes the exact path and line to onSelectLine instead of onSelectFile', async () => {
    mockApi({ [`GET ${BASE}/analysis/secrets`]: { status: 200, data: SCAN } })
    const onSelectFile = vi.fn()
    const onSelectLine = vi.fn()
    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <SecretsPanel projectId={P} repositoryId={R} onSelectFile={onSelectFile} onSelectLine={onSelectLine} />
      </QueryClientProvider>,
    )
    fireEvent.click(
      await screen.findByRole('button', { name: 'Open a/app.py in the viewer (line 4, Private key)' }),
    )
    expect(onSelectLine.mock.calls).toEqual([['a/app.py', 4]])
    expect(onSelectFile).not.toHaveBeenCalled()
  })

  it('Search passes the exact path and line to onSelectLine instead of onSelectFile', async () => {
    mockApi({ [`POST ${BASE}/search`]: { status: 200, data: SEARCH } })
    const onSelectFile = vi.fn()
    const onSelectLine = vi.fn()
    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <CodeSearchPanel projectId={P} repositoryId={R} onSelectFile={onSelectFile} onSelectLine={onSelectLine} />
      </QueryClientProvider>,
    )
    fireEvent.change(screen.getByLabelText('Search text'), { target: { value: 'third' } })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Open a/app.py in the viewer (line 3)' }))
    expect(onSelectLine.mock.calls).toEqual([['a/app.py', 3]])
    expect(onSelectFile).not.toHaveBeenCalled()
  })

  it('both panels keep calling onSelectFile without onSelectLine', async () => {
    mockApi({
      [`GET ${BASE}/analysis/secrets`]: { status: 200, data: SCAN },
      [`POST ${BASE}/search`]: { status: 200, data: SEARCH },
    })
    const onSelectFile = vi.fn()
    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <SecretsPanel projectId={P} repositoryId={R} onSelectFile={onSelectFile} />
        <CodeSearchPanel projectId={P} repositoryId={R} onSelectFile={onSelectFile} />
      </QueryClientProvider>,
    )
    fireEvent.click(await screen.findByRole('button', { name: /line 4, Private key/ }))
    fireEvent.change(screen.getByLabelText('Search text'), { target: { value: 'third' } })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Open a/app.py in the viewer (line 3)' }))
    expect(onSelectFile.mock.calls).toEqual([['a/app.py'], ['a/app.py']])
  })
})

// --- Repository page ---------------------------------------------------------------------

const TREE: TreeDirectory = {
  type: 'directory',
  name: '',
  path: '',
  children: [
    {
      type: 'directory',
      name: 'a',
      path: 'a',
      children: [
        { type: 'file', name: 'app.py', path: 'a/app.py', extension: 'py', language: 'Python', size_bytes: 60 },
      ],
    },
    { type: 'file', name: 'B.md', path: 'B.md', extension: 'md', language: 'Markdown', size_bytes: 9 },
  ],
}

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
  summary: { files: 1, analyzed: 1, binary: 0, too_large: 0, unavailable: 0, total_lines: 5, blank_lines: 0 },
  files: [{ path: 'a/app.py', language: 'Python', status: 'analyzed', line_count: 5, blank_line_count: 0 }],
}

function renderPage(search: CodeSearchResult = SEARCH) {
  const contentPaths: string[] = []
  const requests = mockApi({
    [`GET ${BASE}`]: { status: 200, data: { ...ZIP_REPOSITORY, status: 'ready' } },
    [`GET ${BASE}/tree`]: {
      status: 200,
      data: { repository_id: R, status: 'ready', file_count: 2, total_bytes: 69, tree: TREE },
    },
    [`GET ${BASE}/languages`]: {
      status: 200,
      data: { repository_id: R, status: 'ready', total_files: 2, total_bytes: 69, languages: [] },
    },
    [`GET ${BASE}/jobs`]: { status: 200, data: [] },
    [`GET ${BASE}/analysis/code-metrics`]: { status: 200, data: METRICS },
    [`GET ${BASE}/analysis/dependencies`]: {
      status: 200,
      data: {
        manifests: [{ path: 'B.md', ecosystem: 'npm', status: 'invalid', dependencies: [] }],
        summary: { manifests: 1, parsed: 0, invalid: 1, too_large: 0, unavailable: 0, dependencies: 0 },
        truncated: false,
      },
    },
    [`GET ${BASE}/analysis/secrets`]: { status: 200, data: SCAN },
    [`POST ${BASE}/search`]: { status: 200, data: search },
    [CONTENT_KEY]: contentRoute({ 'a/app.py': FIVE_LINES, 'B.md': 'bee\nmd\n' }, contentPaths),
  })
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
const tree = () => within(screen.getByRole('navigation', { name: 'File tree' }))

async function searchAndOpen(line: number) {
  const search = within(await screen.findByRole('region', { name: 'Search' }))
  fireEvent.change(search.getByLabelText('Search text'), { target: { value: 'line' } })
  fireEvent.click(search.getByRole('button', { name: 'Search' }))
  fireEvent.click(await search.findByRole('button', { name: `Open a/app.py in the viewer (line ${line})` }))
}

describe('RepositoryPage line navigation', () => {
  it('opens a search result at its line and scrolls the File card once', async () => {
    const { contentPaths } = renderPage()
    await searchAndOpen(3)
    await waitFor(() => expect(highlighted()?.textContent).toBe('third line'))
    expect(viewerCode()).toBe(FIVE_LINES)
    expect(within(fileCard()).getByText('Showing line 3')).toBeTruthy()
    expect(scrollIntoViewSpy).toHaveBeenCalledTimes(1)
    expect(scrollIntoViewSpy.mock.contexts[0]).toBe(fileCard())
    await waitFor(() => expect(scrollTopSpy).toHaveBeenCalled())
    expect(contentPaths).toEqual(['a/app.py'])
    expect(tree().getByRole('button', { name: /app\.py/ }).getAttribute('aria-current')).toBe('true')
  })

  it('opens a secret finding at its line', async () => {
    renderPage()
    const secrets = within(await screen.findByRole('region', { name: 'Secrets' }))
    fireEvent.click(
      await secrets.findByRole('button', { name: 'Open a/app.py in the viewer (line 4, Private key)' }),
    )
    await waitFor(() => expect(highlighted()?.textContent).toBe('fourth line'))
    expect(viewerCode()).toBe(FIVE_LINES)
    expect(scrollIntoViewSpy).toHaveBeenCalledTimes(1)
  })

  it('scrolls the code box again when the same result is chosen twice', async () => {
    renderPage()
    await searchAndOpen(3)
    await waitFor(() => expect(scrollTopSpy).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByRole('button', { name: 'Open a/app.py in the viewer (line 3)' }))
    await waitFor(() => expect(scrollTopSpy).toHaveBeenCalledTimes(2))
    expect(highlighted()?.textContent).toBe('third line')
  })

  it('clears the line on tree selection, without scrolling the File card', async () => {
    renderPage()
    await searchAndOpen(3)
    await waitFor(() => expect(highlighted()).toBeTruthy())
    scrollIntoViewSpy.mockClear()

    fireEvent.click(tree().getByRole('button', { name: /B\.md/ }))
    await waitFor(() => expect(viewerCode()).toBe('bee\nmd\n'))
    expect(highlighted()).toBeNull()
    expect(scrollIntoViewSpy).not.toHaveBeenCalled()

    fireEvent.click(tree().getByRole('button', { name: /app\.py/ }))
    await waitFor(() => expect(viewerCode()).toBe(FIVE_LINES))
    expect(highlighted()).toBeNull() // the earlier line is not restored
    expect(scrollIntoViewSpy).not.toHaveBeenCalled()
  })

  it('clears the line on a Code metrics selection', async () => {
    renderPage()
    await searchAndOpen(3)
    await waitFor(() => expect(highlighted()).toBeTruthy())
    const metrics = within(screen.getByRole('region', { name: 'Code metrics' }))
    fireEvent.click(await metrics.findByRole('button', { name: 'Open a/app.py in the viewer' }))
    await waitFor(() => expect(highlighted()).toBeNull())
    expect(viewerCode()).toBe(FIVE_LINES)
    expect(screen.queryByText(/^Showing line/)).toBeNull()
  })

  it('clears the line on a Dependencies selection', async () => {
    renderPage()
    await searchAndOpen(3)
    await waitFor(() => expect(highlighted()).toBeTruthy())
    const deps = within(screen.getByRole('region', { name: 'Dependencies' }))
    fireEvent.click(await deps.findByRole('button', { name: 'Open B.md in the viewer' }))
    await waitFor(() => expect(viewerCode()).toBe('bee\nmd\n'))
    expect(highlighted()).toBeNull()
  })

  it('shows the note for a stale line beyond the file', async () => {
    renderPage({ ...SEARCH, matches: [{ path: 'a/app.py', line: 99, preview: 'gone' }] })
    await searchAndOpen(99)
    expect(await within(fileCard()).findByText('Line 99 is not in this file.')).toBeTruthy()
    expect(highlighted()).toBeNull()
    expect(viewerCode()).toBe(FIVE_LINES)
  })
})
