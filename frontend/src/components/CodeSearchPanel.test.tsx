import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { AxiosError, type InternalAxiosRequestConfig } from 'axios'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { RepositoryPage } from '../pages/RepositoryPage'
import { apiClient } from '../services/client'
import { mockApi, type MockResponse, PROJECT, ZIP_REPOSITORY } from '../test/mockApi'
import { createTestQueryClient } from '../test/render'
import type { CodeSearchMatch, CodeSearchResult, TreeDirectory } from '../types/api'
import { CodeSearchPanel, HINT, NO_MATCHES, TRUNCATED_NOTICE } from './CodeSearchPanel'

const P = PROJECT.id
const R = ZIP_REPOSITORY.id
const BASE = `/projects/${P}/repositories/${R}`
const SEARCH_PATH = `${BASE}/search`
const POST_SEARCH = `POST ${SEARCH_PATH}`
const HTML = '<img src=x onerror=alert(1)>'

// Server order (path, line); deliberately not what a naive client sort would produce.
const MATCHES: CodeSearchMatch[] = [
  { path: 'B.md', line: 9, preview: 'Load the config' },
  { path: 'a/app.py', line: 12, preview: 'def load_config(path):' },
  { path: 'a/app.py', line: 3, preview: '    return LOAD_CONFIG' },
]

function result(extra: Partial<CodeSearchResult> = {}): CodeSearchResult {
  return {
    matches: MATCHES,
    summary: { files_scanned: 7, binary: 2, too_large: 1, unavailable: 3, matches: 3 },
    truncated: false,
    ...extra,
  }
}

const EMPTY: CodeSearchResult = {
  matches: [],
  summary: { files_scanned: 4, binary: 0, too_large: 0, unavailable: 0, matches: 0 },
  truncated: false,
}

function ok(data: unknown): MockResponse {
  return { status: 200, data }
}

type Handler = MockResponse | ((config: InternalAxiosRequestConfig) => MockResponse)

function renderPanel(route: Handler, onSelectFile?: (path: string) => void) {
  const requests = mockApi({ [POST_SEARCH]: route })
  const view = render(
    <QueryClientProvider client={createTestQueryClient()}>
      <CodeSearchPanel projectId={P} repositoryId={R} onSelectFile={onSelectFile} />
    </QueryClientProvider>,
  )
  return { requests, ...view }
}

const input = () => screen.getByLabelText('Search text') as HTMLInputElement
const searchButton = () => screen.getByRole('button', { name: /^Search/ }) as HTMLButtonElement
const type = (value: string) => fireEvent.change(input(), { target: { value } })

function submit(value: string) {
  type(value)
  fireEvent.click(searchButton())
}

const resultsTable = () => screen.findByRole('table', { name: 'Search results' })

async function resultRows(): Promise<string[][]> {
  const table = await resultsTable()
  return within(table)
    .getAllByRole('row')
    .slice(1) // header
    .map((row) => within(row).getAllByRole('cell').map((cell) => cell.textContent ?? ''))
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

describe('CodeSearchPanel initial state', () => {
  it('renders the form, input, disabled button and hint without any request', async () => {
    const { requests } = renderPanel(ok(result()))
    expect(screen.getByRole('form', { name: 'Search code' })).toBeTruthy()
    expect(input().type).toBe('text')
    expect(input().value).toBe('')
    expect(searchButton().textContent).toBe('Search')
    expect(searchButton().disabled).toBe(true)
    expect(screen.getByText(HINT)).toBeTruthy()
    expect(HINT).toBe('Literal, case-insensitive text search.')
    await sleep(50)
    expect(requests).toHaveLength(0)
  })

  it('sends nothing while the user types', async () => {
    const { requests } = renderPanel(ok(result()))
    for (const value of ['l', 'lo', 'load', 'load_config']) {
      type(value)
    }
    expect(searchButton().disabled).toBe(false)
    await sleep(100)
    expect(requests).toHaveLength(0)
  })

  it('keeps the button disabled and sends nothing for an empty input', async () => {
    const { requests } = renderPanel(ok(result()))
    type('x')
    type('')
    expect(searchButton().disabled).toBe(true)
    fireEvent.click(searchButton())
    fireEvent.submit(screen.getByRole('form', { name: 'Search code' }))
    await sleep(50)
    expect(requests).toHaveLength(0)
  })
})

describe('CodeSearchPanel submission', () => {
  it('sends exactly one POST with the exact query in the JSON body', async () => {
    const seen: InternalAxiosRequestConfig[] = []
    const { requests } = renderPanel((config) => {
      seen.push(config)
      return ok(result())
    })
    submit('load_config')
    await resultsTable()
    expect(requests).toHaveLength(1)
    expect(requests[0].method).toBe('POST')
    expect(requests[0].url).toBe(`/projects/${P}/repositories/${R}/search`)
    expect(requests[0].url).not.toContain('?')
    expect(requests[0].url).not.toContain('load_config')
    expect(JSON.parse(requests[0].data as string)).toEqual({ query: 'load_config' })
    expect(Object.keys(JSON.parse(requests[0].data as string))).toEqual(['query'])
    expect(seen[0].params).toBeUndefined()
  })

  it('uses the exact project and repository ids it is given', async () => {
    const requests = mockApi({ 'POST /projects/proj-x/repositories/repo-y/search': ok(EMPTY) })
    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <CodeSearchPanel projectId="proj-x" repositoryId="repo-y" />
      </QueryClientProvider>,
    )
    submit('needle')
    expect(await screen.findByText(NO_MATCHES)).toBeTruthy()
    expect(requests.map((r) => `${r.method} ${r.url}`)).toEqual([
      'POST /projects/proj-x/repositories/repo-y/search',
    ])
  })

  it.each([
    ['  spaced query  '],
    ['.*('],
    ['[a-z]+'],
    ['\\d{3}'],
    ['a|b^$'],
    ['*.py?'],
    ['MiXeD Case'],
    ['a'],
    ['   '],
    [HTML],
  ])('sends %j exactly as typed', async (query) => {
    const { requests } = renderPanel(ok(EMPTY))
    submit(query)
    await screen.findByText(NO_MATCHES)
    expect(JSON.parse(requests[0].data as string)).toEqual({ query })
  })

  it('submits with Enter', async () => {
    const { requests } = renderPanel(ok(EMPTY))
    type('needle')
    fireEvent.submit(input().form as HTMLFormElement)
    await screen.findByText(NO_MATCHES)
    expect(requests).toHaveLength(1)
  })
})

describe('CodeSearchPanel pending state', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('shows progress, disables the button and ignores a second submission', async () => {
    let resolve: (value: { data: CodeSearchResult }) => void = () => {}
    const post = vi.spyOn(apiClient, 'post').mockImplementation(
      () => new Promise((r) => (resolve = r as typeof resolve)),
    )
    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <CodeSearchPanel projectId={P} repositoryId={R} />
      </QueryClientProvider>,
    )
    submit('needle')
    expect(await screen.findByText('Searching the repository…')).toBeTruthy()
    expect(searchButton().textContent).toBe('Searching…')
    expect(searchButton().disabled).toBe(true)
    expect(screen.queryByText(HINT)).toBeNull()
    fireEvent.click(searchButton())
    fireEvent.submit(input().form as HTMLFormElement) // Enter while pending
    await sleep(50)
    expect(post).toHaveBeenCalledTimes(1)
    expect(post).toHaveBeenCalledWith(SEARCH_PATH, { query: 'needle' })

    resolve({ data: EMPTY })
    expect(await screen.findByText(NO_MATCHES)).toBeTruthy()
    expect(screen.queryByText('Searching the repository…')).toBeNull()
    expect(searchButton().textContent).toBe('Search')
    expect(searchButton().disabled).toBe(false)
  })
})

describe('CodeSearchPanel results', () => {
  it('shows the no-matches state with the summary', async () => {
    renderPanel(ok(EMPTY))
    submit('absent')
    expect(await screen.findByText(NO_MATCHES)).toBeTruthy()
    expect(NO_MATCHES).toBe('No matches found.')
    expect(screen.getByRole('table', { name: 'Search summary' })).toBeTruthy()
    expect(screen.queryByRole('table', { name: 'Search results' })).toBeNull()
    expect(screen.queryByText(HINT)).toBeNull()
  })

  it('lists matches with file, line and match columns', async () => {
    renderPanel(ok(result()))
    submit('load')
    const table = await resultsTable()
    const headers = within(table)
      .getAllByRole('columnheader')
      .map((header) => header.textContent)
    expect(headers).toEqual(['File', 'Line', 'Match'])
    expect(await resultRows()).toEqual([
      ['B.md', '9', 'Load the config'],
      ['a/app.py', '12', 'def load_config(path):'],
      ['a/app.py', '3', '    return LOAD_CONFIG'],
    ])
    expect(screen.queryByText(NO_MATCHES)).toBeNull()
  })

  it('keeps the exact server order, without sorting or deduplicating', async () => {
    const order: CodeSearchMatch[] = [
      { path: 'z.txt', line: 2, preview: 'z' },
      { path: 'a.txt', line: 1, preview: 'a' },
      { path: 'a.txt', line: 1, preview: 'a' },
      { path: 'm.txt', line: 10, preview: 'm10' },
      { path: 'm.txt', line: 2, preview: 'm2' },
    ]
    renderPanel(ok(result({ matches: order })))
    submit('x1')
    const rows = await resultRows()
    expect(rows.map(([path, line]) => `${path}:${line}`)).toEqual([
      'z.txt:2',
      'a.txt:1',
      'a.txt:1',
      'm.txt:10',
      'm.txt:2',
    ])
  })

  it('shows all five summary values', async () => {
    renderPanel(ok(result()))
    submit('load')
    const summary = await screen.findByRole('table', { name: 'Search summary' })
    const rows = within(summary)
      .getAllByRole('row')
      .map((row) => [
        within(row).getByRole('rowheader').textContent,
        within(row).getByRole('cell').textContent,
      ])
    expect(rows).toEqual([
      ['Files scanned', '7'],
      ['Binary or not UTF-8', '2'],
      ['Too large', '1'],
      ['Unavailable', '3'],
      ['Matches', '3'],
    ])
  })

  it('shows the truncated notice only when truncated is true', async () => {
    const { unmount } = renderPanel(ok(result({ truncated: true })))
    submit('load')
    expect(await screen.findByText(TRUNCATED_NOTICE)).toBeTruthy()
    expect(TRUNCATED_NOTICE).toBe(
      'Results are incomplete: the search stopped at a limit (at most 5,000 files, 64 MiB or 200 matches).',
    )
    unmount()

    renderPanel(ok(result({ truncated: false })))
    submit('load')
    await resultsTable()
    expect(screen.queryByText(TRUNCATED_NOTICE)).toBeNull()
  })

  it('keeps showing the searched query after the input is edited', async () => {
    renderPanel(ok(result()))
    submit('  load_config ')
    await resultsTable()
    const label = screen.getByText(/^Results for/)
    expect(label.querySelector('span')?.textContent).toBe('  load_config ')
    type('something else')
    expect(screen.getByText(/^Results for/).querySelector('span')?.textContent).toBe('  load_config ')
    expect(input().value).toBe('something else')
  })

  it('keeps preview whitespace exactly as received', async () => {
    const preview = '\t  indented\t value   '
    renderPanel(ok(result({ matches: [{ path: 'a.py', line: 1, preview }] })))
    submit('value')
    await resultsTable()
    expect(screen.getByTestId('search-preview').textContent).toBe(preview)
  })
})

describe('CodeSearchPanel errors', () => {
  it.each([
    [401, 'Could not validate credentials'],
    [404, 'Repository not found'],
    [429, 'Too many attempts; please try again later'],
  ])('shows the exact server detail for %i', async (status, detail) => {
    renderPanel({ status, data: { detail } })
    submit('needle')
    expect((await screen.findByRole('alert')).textContent).toBe(detail)
    expect(screen.queryByRole('table')).toBeNull()
  })

  it('shows the server validation messages for 422', async () => {
    renderPanel({
      status: 422,
      data: {
        detail: [
          { loc: ['body', 'query'], msg: 'String should have at least 2 characters', type: 'string_too_short' },
        ],
      },
    })
    submit('a')
    expect((await screen.findByRole('alert')).textContent).toBe(
      'String should have at least 2 characters',
    )
  })

  it('uses the existing network error message', async () => {
    renderPanel(() => {
      throw new AxiosError('Network Error', AxiosError.ERR_NETWORK)
    })
    submit('needle')
    expect((await screen.findByRole('alert')).textContent).toBe('Could not reach the server')
  })

  it('falls back to the panel message without a server detail', async () => {
    renderPanel({ status: 500, data: {} })
    submit('needle')
    expect((await screen.findByRole('alert')).textContent).toBe('Could not search the repository')
  })

  it('clears an earlier error after a successful search', async () => {
    let calls = 0
    renderPanel(() => {
      calls += 1
      return calls === 1 ? { status: 429, data: { detail: 'Too many attempts; please try again later' } } : ok(result())
    })
    submit('needle')
    await screen.findByRole('alert')
    submit('needle again')
    await resultsTable()
    expect(screen.queryByRole('alert')).toBeNull()
  })
})

describe('CodeSearchPanel safety', () => {
  it('renders HTML- and script-looking path, preview and query as plain text', async () => {
    const script = '<script>alert("x")</script>'
    const link = '<a href="https://example.invalid">click</a>'
    renderPanel(ok(result({ matches: [{ path: HTML, line: 1, preview: `${script}${link}` }] })), vi.fn())
    submit(HTML)
    const rows = await resultRows()
    expect(rows[0].slice(0, 3)).toEqual([HTML, '1', `${script}${link}`])
    expect(screen.getByText(/^Results for/).querySelector('span')?.textContent).toBe(HTML)
    expect(document.querySelector('img, script, a, iframe')).toBeNull()
    expect(document.querySelector('[href], [src], [onerror]')).toBeNull()
  })

  it('never renders unexpected fields', async () => {
    const extra = {
      value: 'FAKE-VALUE-MARKER',
      content: 'FAKE-CONTENT-MARKER',
      column: 4242,
      sha256: 'FAKE-SHA-MARKER',
      id: 'FAKE-ID-MARKER',
      storage_path: 'FAKE-STORAGE-MARKER',
    }
    const matches = MATCHES.map((match) => ({ ...match, ...extra }))
    renderPanel(ok({ ...result(), matches, ...extra, summary: { ...result().summary, ...extra } }), vi.fn())
    submit('load')
    await resultsTable()
    const text = document.body.textContent ?? ''
    for (const marker of Object.values(extra)) {
      expect(text).not.toContain(String(marker))
    }
    for (const button of screen.getAllByRole('button', { name: /in the viewer/ })) {
      expect(button.getAttribute('aria-label')).not.toMatch(/FAKE|4242/)
    }
    const summary = screen.getByRole('table', { name: 'Search summary' })
    expect(within(summary).getAllByRole('row')).toHaveLength(5)
  })

  it('uses no browser storage or cookies of its own and makes no other requests', async () => {
    const getItem = vi.spyOn(Storage.prototype, 'getItem')
    const writes = [
      vi.spyOn(Storage.prototype, 'setItem'),
      vi.spyOn(Storage.prototype, 'removeItem'),
      vi.spyOn(Storage.prototype, 'clear'),
    ]
    const cookieGet = vi.fn(() => '')
    const cookieSet = vi.fn()
    Object.defineProperty(document, 'cookie', { configurable: true, get: cookieGet, set: cookieSet })
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    try {
      const { requests } = renderPanel(ok(result()), vi.fn())
      submit('load')
      await resultsTable()
      fireEvent.click(screen.getAllByRole('button', { name: /in the viewer/ })[0])
      await sleep(50)
      // The only read is the existing apiClient's access token lookup (services/session.ts).
      expect(getItem.mock.calls).toEqual([['ai_se_access_token']])
      for (const spy of writes) {
        expect(spy).not.toHaveBeenCalled()
      }
      expect(cookieGet).not.toHaveBeenCalled()
      expect(cookieSet).not.toHaveBeenCalled()
      expect(fetchSpy).not.toHaveBeenCalled()
      expect(requests.map((r) => `${r.method} ${r.url}`)).toEqual([POST_SEARCH])
      expect(requests[0].baseURL).toBe(apiClient.defaults.baseURL)
    } finally {
      for (const spy of [getItem, ...writes, fetchSpy]) {
        spy.mockRestore()
      }
      delete (document as { cookie?: unknown }).cookie // back to the prototype accessor
    }
  })
})

describe('CodeSearchPanel navigation', () => {
  it('has a separate View file button per result that passes the exact path', async () => {
    const onSelectFile = vi.fn()
    renderPanel(ok(result()), onSelectFile)
    submit('load')
    const table = await resultsTable()
    const buttons = within(table).getAllByRole('button')
    expect(buttons).toHaveLength(MATCHES.length)
    for (const button of buttons) {
      expect(button.textContent).toBe('View file')
      expect(button.closest('a')).toBeNull()
      expect(button.querySelector('button, a')).toBeNull() // nothing nested
    }
    expect(buttons.map((button) => button.getAttribute('aria-label'))).toEqual([
      'Open B.md in the viewer (line 9)',
      'Open a/app.py in the viewer (line 12)',
      'Open a/app.py in the viewer (line 3)',
    ])
    fireEvent.click(buttons[2])
    fireEvent.click(buttons[0])
    expect(onSelectFile.mock.calls).toEqual([['a/app.py'], ['B.md']])
  })

  it('passes an unusual path through unchanged', async () => {
    const onSelectFile = vi.fn()
    const path = `dir with space/${HTML}/ü%2e%2e.txt`
    renderPanel(ok(result({ matches: [{ path, line: 4, preview: 'x' }] })), onSelectFile)
    submit('x1')
    fireEvent.click(await screen.findByRole('button', { name: `Open ${path} in the viewer (line 4)` }))
    expect(onSelectFile).toHaveBeenCalledWith(path)
  })

  it('has no View file buttons without onSelectFile', async () => {
    renderPanel(ok(result()))
    submit('load')
    const table = await resultsTable()
    expect(within(table).queryAllByRole('button')).toHaveLength(0)
    expect(screen.queryByText('View file')).toBeNull()
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
        { type: 'file', name: 'app.py', path: 'a/app.py', extension: 'py', language: 'Python', size_bytes: 40 },
      ],
    },
    { type: 'file', name: 'B.md', path: 'B.md', extension: 'md', language: 'Markdown', size_bytes: 9 },
  ],
}

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
  delete (HTMLElement.prototype as { scrollIntoView?: unknown }).scrollIntoView
})

function renderPage() {
  const contentPaths: string[] = []
  const requests = mockApi({
    [`GET ${BASE}`]: { status: 200, data: { ...ZIP_REPOSITORY, status: 'ready' } },
    [`GET ${BASE}/tree`]: {
      status: 200,
      data: { repository_id: R, status: 'ready', file_count: 2, total_bytes: 49, tree: TREE },
    },
    [`GET ${BASE}/languages`]: {
      status: 200,
      data: { repository_id: R, status: 'ready', total_files: 2, total_bytes: 49, languages: [] },
    },
    [`GET ${BASE}/jobs`]: { status: 200, data: [] },
    [`GET ${BASE}/analysis/code-metrics`]: {
      status: 200,
      data: { job: null, summary: null, files: [] },
    },
    [`GET ${BASE}/analysis/dependencies`]: {
      status: 200,
      data: {
        manifests: [],
        summary: { manifests: 0, parsed: 0, invalid: 0, too_large: 0, unavailable: 0, dependencies: 0 },
        truncated: false,
      },
    },
    [`GET ${BASE}/analysis/secrets`]: {
      status: 200,
      data: {
        findings: [],
        summary: { files_scanned: 2, binary: 0, too_large: 0, unavailable: 0, findings: 0 },
        truncated: false,
      },
    },
    [POST_SEARCH]: ok(result()),
    [`GET ${BASE}/files/content`]: (config) => {
      const path = (config.params as { path: string }).path
      contentPaths.push(path)
      return {
        status: 200,
        data: { path, language: null, size_bytes: 3, content: `content of ${path}`, reason: null },
      }
    },
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

describe('RepositoryPage integration', () => {
  it('places Search after Files and Languages and before Code metrics, without searching', async () => {
    const { requests } = renderPage()
    const search = await screen.findByRole('region', { name: 'Search' })
    expect(within(search).getByRole('form', { name: 'Search code' })).toBeTruthy()
    const FOLLOWING = Node.DOCUMENT_POSITION_FOLLOWING
    const files = screen.getByRole('region', { name: 'Files' })
    const languages = screen.getByRole('region', { name: 'Languages' })
    const metrics = screen.getByRole('region', { name: 'Code metrics' })
    expect(files.compareDocumentPosition(search) & FOLLOWING).toBeTruthy()
    expect(languages.compareDocumentPosition(search) & FOLLOWING).toBeTruthy()
    expect(search.compareDocumentPosition(metrics) & FOLLOWING).toBeTruthy()
    for (const name of ['Dependencies', 'Secrets', 'File']) {
      expect(screen.getByRole('region', { name })).toBeTruthy()
    }
    await screen.findByRole('table', { name: 'Secret scan summary' }) // page settled
    await sleep(50)
    expect(requests.filter((r) => r.method === 'POST')).toHaveLength(0)
  })

  it('opens a result in the existing viewer, marks it in the tree and scrolls to it', async () => {
    stubScroll()
    const { contentPaths, requests } = renderPage()
    const search = within(await screen.findByRole('region', { name: 'Search' }))
    fireEvent.change(search.getByLabelText('Search text'), { target: { value: 'load' } })
    fireEvent.click(search.getByRole('button', { name: 'Search' }))
    fireEvent.click(await search.findByRole('button', { name: 'Open a/app.py in the viewer (line 3)' }))

    await waitFor(() => expect(viewerCode()).toBe('content of a/app.py'))
    expect(contentPaths).toEqual(['a/app.py'])
    const contentRequests = requests.filter((r) => r.url.endsWith('/files/content'))
    expect(contentRequests.map((r) => r.url)).toEqual([`${BASE}/files/content`]) // path not in URL
    expect(scrollSpy).toHaveBeenCalledTimes(1)
    expect(scrollSpy.mock.contexts[0]).toBe(fileCard())
    const tree = within(screen.getByRole('navigation', { name: 'File tree' }))
    expect(tree.getByRole('button', { name: /app\.py/ }).getAttribute('aria-current')).toBe('true')
    expect(requests.filter((r) => r.method === 'POST').map((r) => r.url)).toEqual([SEARCH_PATH])
  })

  it('keeps tree selection working, without the panel scroll', async () => {
    stubScroll()
    const { contentPaths } = renderPage()
    const tree = within(await screen.findByRole('navigation', { name: 'File tree' }))
    fireEvent.click(tree.getByRole('button', { name: /B\.md/ }))
    await waitFor(() => expect(viewerCode()).toBe('content of B.md'))
    expect(tree.getByRole('button', { name: /B\.md/ }).getAttribute('aria-current')).toBe('true')
    expect(scrollSpy).not.toHaveBeenCalled()
    expect(contentPaths).toEqual(['B.md'])
  })
})
