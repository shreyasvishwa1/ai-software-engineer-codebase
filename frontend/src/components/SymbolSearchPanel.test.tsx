import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { AxiosError, type InternalAxiosRequestConfig } from 'axios'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { RepositoryPage } from '../pages/RepositoryPage'
import { apiClient } from '../services/client'
import { mockApi, type MockResponse, PROJECT, ZIP_REPOSITORY } from '../test/mockApi'
import { createTestQueryClient } from '../test/render'
import type { SymbolMatch, SymbolSearchResult, TreeDirectory } from '../types/api'
import { HINT, NO_SYMBOLS, summaryText, SymbolSearchPanel, TRUNCATED_NOTICE } from './SymbolSearchPanel'

const P = PROJECT.id
const R = ZIP_REPOSITORY.id
const BASE = `/projects/${P}/repositories/${R}`
const SYMBOLS_PATH = `${BASE}/symbols/search`
const POST_SYMBOLS = `POST ${SYMBOLS_PATH}`
const HTML = '<img src=x onerror=alert(1)>'
const CHANGED = 'Repository changed during analysis; run it again'

function match(extra: Partial<SymbolMatch>): SymbolMatch {
  return {
    path: 'app/services/auth.py',
    kind: 'function',
    async: false,
    name: 'authenticate',
    line: 1,
    end_line: 2,
    depth: 0,
    ...extra,
  }
}

// Server order, deliberately not what sorting by path, name or line would produce.
const MATCHES: SymbolMatch[] = [
  match({ path: 'z/last.py', kind: 'class', line: 30, end_line: 40 }),
  match({ path: 'a/first.py', kind: 'method', async: true, line: 12, end_line: 15, depth: 1 }),
  match({ path: 'a/first.py', kind: 'function', line: 3, end_line: 4, depth: 2 }),
]
const SUMMARY = { files_scanned: 9, too_large: 1, binary: 2, syntax_error: 3 }

function symbols(extra: Partial<SymbolSearchResult> = {}): SymbolSearchResult {
  return { results: MATCHES, summary: SUMMARY, truncated: false, ...extra }
}

function ok(data: unknown): MockResponse {
  return { status: 200, data }
}

type Handler = MockResponse | ((config: InternalAxiosRequestConfig) => MockResponse)

function renderPanel(route: Handler, onSelectLine?: (path: string, line: number) => void) {
  const requests = mockApi({ [POST_SYMBOLS]: route })
  const view = render(
    <QueryClientProvider client={createTestQueryClient()}>
      <SymbolSearchPanel projectId={P} repositoryId={R} onSelectLine={onSelectLine} />
    </QueryClientProvider>,
  )
  return { requests, ...view }
}

const input = () => screen.getByLabelText('Symbol name') as HTMLInputElement
const searchButton = () => screen.getByRole('button', { name: /^Search/ }) as HTMLButtonElement
const type = (value: string) => fireEvent.change(input(), { target: { value } })
const resultList = () => screen.findByRole('list', { name: 'Symbol results' })
const rows = async () => {
  await resultList()
  return screen.getAllByTestId('symbol-result')
}
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

function submit(value: string) {
  type(value)
  fireEvent.click(searchButton())
}

// --- Initial state and requests -------------------------------------------------------------

describe('SymbolSearchPanel initial state', () => {
  it('renders the form, label, hint and disabled button without any request', async () => {
    const { requests } = renderPanel(ok(symbols()))
    expect(screen.getByRole('form', { name: 'Search symbols' })).toBeTruthy()
    expect(input().value).toBe('')
    expect(searchButton().textContent).toBe('Search')
    expect(searchButton().disabled).toBe(true)
    expect(screen.getByText(HINT)).toBeTruthy()
    await sleep(50)
    expect(requests).toHaveLength(0)
  })

  it('sends nothing while typing and nothing for an empty input', async () => {
    const { requests } = renderPanel(ok(symbols()))
    for (const value of ['a', 'au', 'auth']) {
      type(value)
    }
    expect(searchButton().disabled).toBe(false)
    type('')
    expect(searchButton().disabled).toBe(true)
    fireEvent.click(searchButton())
    fireEvent.submit(screen.getByRole('form', { name: 'Search symbols' }))
    await sleep(50)
    expect(requests).toHaveLength(0)
  })
})

describe('SymbolSearchPanel requests', () => {
  it('sends exactly one POST with exactly the typed query', async () => {
    const seen: InternalAxiosRequestConfig[] = []
    const { requests } = renderPanel((config) => {
      seen.push(config)
      return ok(symbols())
    })
    submit('authenticate')
    await resultList()
    expect(requests).toHaveLength(1)
    expect(requests[0].method).toBe('POST')
    expect(requests[0].url).toBe(`/projects/${P}/repositories/${R}/symbols/search`)
    expect(requests[0].url).not.toContain('?')
    expect(requests[0].url).not.toContain('authenticate')
    expect(JSON.parse(requests[0].data as string)).toEqual({ query: 'authenticate' })
    expect(Object.keys(JSON.parse(requests[0].data as string))).toEqual(['query'])
    expect(seen[0].params).toBeUndefined()
  })

  it('uses the exact project and repository ids it is given', async () => {
    const requests = mockApi({ 'POST /projects/proj-x/repositories/repo-y/symbols/search': ok(symbols({ results: [] })) })
    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <SymbolSearchPanel projectId="proj-x" repositoryId="repo-y" />
      </QueryClientProvider>,
    )
    submit('x')
    expect(await screen.findByText(NO_SYMBOLS)).toBeTruthy()
    expect(requests.map((r) => `${r.method} ${r.url}`)).toEqual([
      'POST /projects/proj-x/repositories/repo-y/symbols/search',
    ])
  })

  it.each([
    ['  authenticate  '],
    ['MiXeD_Case'],
    ['ａｕｔｈ'],
    ['app.auth'],
    ['1abc'],
    ['   '],
    [HTML],
  ])('sends %j unchanged (the server validates it)', async (query) => {
    const { requests } = renderPanel(ok(symbols({ results: [] })))
    submit(query)
    await screen.findByText(NO_SYMBOLS)
    expect(JSON.parse(requests[0].data as string)).toEqual({ query })
  })

  it('submits with Enter', async () => {
    const { requests } = renderPanel(ok(symbols({ results: [] })))
    type('auth')
    fireEvent.submit(input().form as HTMLFormElement)
    await screen.findByText(NO_SYMBOLS)
    expect(requests).toHaveLength(1)
  })

  it('sends a new request every time, even for the same query', async () => {
    let calls = 0
    renderPanel(() => {
      calls += 1
      return ok(symbols({ results: calls === 1 ? MATCHES : [] }))
    })
    submit('authenticate')
    await resultList()
    fireEvent.click(searchButton())
    expect(await screen.findByText(NO_SYMBOLS)).toBeTruthy()
    expect(calls).toBe(2)
  })

  it('shows progress, disables the button and ignores a second submission', async () => {
    let resolve: (value: { data: SymbolSearchResult }) => void = () => {}
    const post = vi
      .spyOn(apiClient, 'post')
      .mockImplementation(() => new Promise((r) => (resolve = r as typeof resolve)))
    try {
      render(
        <QueryClientProvider client={createTestQueryClient()}>
          <SymbolSearchPanel projectId={P} repositoryId={R} />
        </QueryClientProvider>,
      )
      submit('authenticate')
      expect(await screen.findByText('Searching symbols…')).toBeTruthy()
      expect(searchButton().textContent).toBe('Searching…')
      expect(searchButton().disabled).toBe(true)
      expect(screen.queryByText(HINT)).toBeNull()
      fireEvent.submit(input().form as HTMLFormElement)
      await sleep(50)
      expect(post).toHaveBeenCalledTimes(1)
      expect(post).toHaveBeenCalledWith(SYMBOLS_PATH, { query: 'authenticate' })
      resolve({ data: symbols() })
      await resultList()
      expect(searchButton().textContent).toBe('Search')
    } finally {
      post.mockRestore()
    }
  })
})

// --- Results -----------------------------------------------------------------------------

describe('SymbolSearchPanel results', () => {
  it('lists results in exact server order with kind, async, name, nesting, path and lines', async () => {
    renderPanel(ok(symbols()), vi.fn())
    submit('authenticate')
    const items = await rows()
    expect(items.map((item) => item.textContent)).toEqual([
      'classauthenticatez/last.pyline 30–40',
      'methodasyncauthenticatenesteda/first.pyline 12–15',
      'functionauthenticatenesteda/first.pyline 3–4',
    ])
  })

  it('never shows the numeric depth, only "nested" when depth > 0', async () => {
    const depths = [0, 1, 42].map((depth, i) => match({ path: `d${i}.py`, depth, line: 5 + i, end_line: 9 }))
    renderPanel(ok(symbols({ results: depths })))
    submit('authenticate')
    const items = await rows()
    expect(items.map((item) => within(item).queryByText('nested') !== null)).toEqual([false, true, true])
    expect(items[2].textContent).not.toContain('42')
  })

  it('keeps every result, including same-name definitions of different kinds', async () => {
    const same = [
      match({ path: 'b.py', kind: 'function', line: 1, end_line: 1 }),
      match({ path: 'b.py', kind: 'class', line: 3, end_line: 4 }),
      match({ path: 'b.py', kind: 'function', line: 1, end_line: 1 }),
    ]
    renderPanel(ok(symbols({ results: same })))
    submit('authenticate')
    expect((await rows()).map((item) => item.textContent)).toEqual([
      'functionauthenticateb.pyline 1–1',
      'classauthenticateb.pyline 3–4',
      'functionauthenticateb.pyline 1–1',
    ])
  })

  it('shows results the server returned even if their names differ from the input', async () => {
    renderPanel(ok(symbols({ results: [match({ name: 'auth' })] })))
    submit('ａｕｔｈ')
    expect((await rows()).map((item) => item.textContent)).toEqual(['functionauthapp/services/auth.pyline 1–2'])
  })

  it('shows the searched query, the summary and the empty state', async () => {
    renderPanel(ok(symbols({ results: [] })))
    submit('  spaced  ')
    expect(await screen.findByText(NO_SYMBOLS)).toBeTruthy()
    expect(NO_SYMBOLS).toBe('No symbols found.')
    const label = screen.getByText(/^Results for/)
    expect(label.querySelector('span')?.textContent).toBe('  spaced  ')
    type('other')
    expect(screen.getByText(/^Results for/).querySelector('span')?.textContent).toBe('  spaced  ')
    expect(screen.getByTestId('symbols-summary').textContent).toBe(
      'Scanned 9 files · 1 too large · 2 binary · 3 with syntax errors.',
    )
    expect(summaryText({ files_scanned: 0, too_large: 99, binary: 7, syntax_error: 1 })).toBe(
      'Scanned 0 files · 99 too large · 7 binary · 1 with syntax errors.',
    )
    expect(screen.queryByText(TRUNCATED_NOTICE)).toBeNull()
  })

  it('shows the truncated notice only when truncated is true', async () => {
    const { unmount } = renderPanel(ok(symbols({ truncated: true })))
    submit('authenticate')
    expect(await screen.findByText(TRUNCATED_NOTICE)).toBeTruthy()
    expect(TRUNCATED_NOTICE).toBe(
      'Results may be incomplete: a scan or result limit was reached (at most 200 results).',
    )
    unmount()
    renderPanel(ok(symbols({ truncated: true, results: [] })))
    submit('authenticate')
    expect(await screen.findByText(TRUNCATED_NOTICE)).toBeTruthy()
    expect(screen.getByText(NO_SYMBOLS)).toBeTruthy()
  })
})

// --- Errors ------------------------------------------------------------------------------

describe('SymbolSearchPanel errors', () => {
  it.each([
    [401, 'Could not validate credentials'],
    [404, 'Repository not found'],
    [409, CHANGED],
    [429, 'Too many attempts; please try again later'],
  ])('shows the exact server detail for %i and no results', async (status, detail) => {
    renderPanel({ status, data: { detail, results: MATCHES } })
    submit('authenticate')
    expect((await screen.findByRole('alert')).textContent).toBe(detail)
    expect(screen.queryByRole('list', { name: 'Symbol results' })).toBeNull()
  })

  it('shows the server validation messages for 422', async () => {
    renderPanel({
      status: 422,
      data: { detail: [{ loc: ['body', 'query'], msg: 'Value error, query must be a Python identifier' }] },
    })
    submit('app.auth')
    expect((await screen.findByRole('alert')).textContent).toBe(
      'Value error, query must be a Python identifier',
    )
  })

  it('uses the existing network and fallback messages, without retrying', async () => {
    const { unmount } = renderPanel(() => {
      throw new AxiosError('Network Error', AxiosError.ERR_NETWORK)
    })
    submit('authenticate')
    expect((await screen.findByRole('alert')).textContent).toBe('Could not reach the server')
    unmount()
    const { requests } = renderPanel({ status: 500, data: {} })
    submit('authenticate')
    expect((await screen.findByRole('alert')).textContent).toBe('Could not search symbols')
    await sleep(100)
    expect(requests).toHaveLength(1) // no automatic retry
  })

  it('clears an earlier error after a successful search', async () => {
    let calls = 0
    renderPanel(() => {
      calls += 1
      return calls === 1 ? { status: 409, data: { detail: CHANGED } } : ok(symbols())
    })
    submit('authenticate')
    await screen.findByRole('alert')
    fireEvent.click(searchButton())
    await resultList()
    expect(screen.queryByRole('alert')).toBeNull()
  })
})

// --- Navigation and safety ---------------------------------------------------------------

describe('SymbolSearchPanel navigation and safety', () => {
  it('opens the exact path at the exact line', async () => {
    const onSelectLine = vi.fn()
    const odd = [
      match({ path: 'App/Mixed Case.py', line: 42 }),
      match({ path: '  padded/path.py  ', line: 7 }),
      match({ path: 'Src\\Win Path\\x.py', line: 1 }),
      match({ path: 'enc/%2E%2e/%20x.py', line: 999 }),
    ]
    renderPanel(ok(symbols({ results: odd })), onSelectLine)
    submit('authenticate')
    const list = await resultList()
    const buttons = within(list).getAllByRole('button')
    expect(buttons.map((b) => b.getAttribute('aria-label'))).toEqual(
      odd.map((m) => `Open ${m.path} at line ${m.line} (function authenticate)`),
    )
    for (const button of [...buttons].reverse()) {
      fireEvent.click(button)
    }
    expect(onSelectLine.mock.calls).toEqual([...odd].reverse().map((m) => [m.path, m.line]))
  })

  it('makes no button for an invalid line and none without onSelectLine', async () => {
    const odd = [
      match({ line: 0 }),
      match({ line: 2.5 }),
      match({ line: '3' as unknown as number }),
      match({ line: 4, path: 'ok.py' }),
    ]
    const onSelectLine = vi.fn()
    const { unmount } = renderPanel(ok(symbols({ results: odd })), onSelectLine)
    submit('authenticate')
    const list = await resultList()
    expect(within(list).getAllByRole('button').map((b) => b.getAttribute('aria-label'))).toEqual([
      'Open ok.py at line 4 (function authenticate)',
    ])
    expect(within(list).getAllByTestId('symbol-result')).toHaveLength(4)
    unmount()
    renderPanel(ok(symbols()))
    submit('authenticate')
    expect(within(await resultList()).queryAllByRole('button')).toHaveLength(0)
  })

  it('renders HTML-like names, kinds and paths as literal text', async () => {
    const evil = [match({ name: HTML, path: '<script>alert(1)</script>.py', kind: '<a href="https://example.invalid">x</a>' as SymbolMatch['kind'] })]
    renderPanel(ok(symbols({ results: evil })), vi.fn())
    submit(HTML)
    const [item] = await rows()
    expect(item.textContent).toBe(`<a href="https://example.invalid">x</a>${HTML}<script>alert(1)</script>.pyline 1–2`)
    expect(screen.getByText(/^Results for/).querySelector('span')?.textContent).toBe(HTML)
    expect(document.querySelector('img, script, a, iframe')).toBeNull()
    expect(document.querySelector('[href], [src], [onerror]')).toBeNull()
  })

  it('never renders unexpected fields', async () => {
    const extra = { snippet: 'FAKE-SNIPPET-MARKER', sha256: 'FAKE-SHA-MARKER', id: 'FAKE-ID-MARKER', column: 4242 }
    const withExtra = MATCHES.map((m) => ({ ...m, ...extra }))
    renderPanel(ok({ ...symbols(), results: withExtra, summary: { ...SUMMARY, ...extra }, ...extra }), vi.fn())
    submit('authenticate')
    await resultList()
    const text = document.body.textContent ?? ''
    for (const marker of Object.values(extra)) {
      expect(text).not.toContain(String(marker))
    }
    for (const button of screen.getAllByRole('button', { name: /^Open / })) {
      expect(button.getAttribute('aria-label')).not.toMatch(/FAKE|4242/)
    }
  })

  it('uses no browser storage or cookies of its own', async () => {
    const getItem = vi.spyOn(Storage.prototype, 'getItem')
    const writes = [
      vi.spyOn(Storage.prototype, 'setItem'),
      vi.spyOn(Storage.prototype, 'removeItem'),
      vi.spyOn(Storage.prototype, 'clear'),
    ]
    const cookieGet = vi.fn(() => '')
    const cookieSet = vi.fn()
    Object.defineProperty(document, 'cookie', { configurable: true, get: cookieGet, set: cookieSet })
    try {
      const { requests } = renderPanel(ok(symbols()), vi.fn())
      submit('authenticate')
      await resultList()
      fireEvent.click(screen.getAllByRole('button', { name: /^Open / })[0])
      // The only read is the existing apiClient's access token lookup (services/session.ts).
      expect(getItem.mock.calls).toEqual([['ai_se_access_token']])
      for (const spy of writes) {
        expect(spy).not.toHaveBeenCalled()
      }
      expect(cookieGet).not.toHaveBeenCalled()
      expect(cookieSet).not.toHaveBeenCalled()
      expect(requests.map((r) => `${r.method} ${r.url}`)).toEqual([POST_SYMBOLS])
    } finally {
      for (const spy of [getItem, ...writes]) {
        spy.mockRestore()
      }
      delete (document as { cookie?: unknown }).cookie // back to the prototype accessor
    }
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
      name: 'app',
      path: 'app',
      children: [
        { type: 'file', name: 'auth.py', path: 'app/auth.py', extension: 'py', language: 'Python', size_bytes: 60 },
      ],
    },
    { type: 'file', name: 'README.md', path: 'README.md', extension: 'md', language: 'Markdown', size_bytes: 9 },
  ],
}

const AUTH = 'import os\n\nclass Service:\n    def authenticate(self):\n        return 1\n\ndef authenticate():\n    pass\n'
const scrollTopSpy = vi.fn()
const scrollIntoViewSpy = vi.fn()

beforeEach(() => {
  Object.defineProperty(HTMLSpanElement.prototype, 'offsetTop', {
    configurable: true,
    get(this: HTMLElement) {
      return this.dataset.testid === 'selected-line' ? 400 : 0
    },
  })
  Object.defineProperty(HTMLPreElement.prototype, 'clientHeight', { configurable: true, get: () => 300 })
  Object.defineProperty(HTMLPreElement.prototype, 'scrollTop', { configurable: true, get: () => 0, set: scrollTopSpy })
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

function renderPage() {
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
    [`GET ${BASE}/analysis/code-metrics`]: { status: 200, data: { job: null, summary: null, files: [] } },
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
    [`GET ${BASE}/files/content`]: (config) => {
      const path = (config.params as { path: string }).path
      contentPaths.push(path)
      const text = path === 'app/auth.py' ? AUTH : '# readme\n'
      return {
        status: 200,
        data: { path, language: path.endsWith('.py') ? 'Python' : 'Markdown', size_bytes: text.length, content: text, reason: null },
      }
    },
    [POST_SYMBOLS]: ok(
      symbols({
        results: [
          match({ path: 'app/auth.py', kind: 'method', line: 4, end_line: 5, depth: 1 }),
          match({ path: 'app/auth.py', kind: 'function', line: 7, end_line: 8 }),
        ],
      }),
    ),
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
const codeText = () => fileCard().querySelector('pre code')?.textContent
const highlighted = () => screen.queryByTestId('selected-line')

async function searchOnPage() {
  const card = within(await screen.findByRole('region', { name: 'Symbols' }))
  fireEvent.change(card.getByLabelText('Symbol name'), { target: { value: 'authenticate' } })
  fireEvent.click(card.getByRole('button', { name: 'Search' }))
  await card.findByRole('list', { name: 'Symbol results' })
  return card
}

describe('RepositoryPage symbol navigation', () => {
  it('places Symbols after Search and before Code metrics, without searching', async () => {
    const { requests } = renderPage()
    const symbolsCard = await screen.findByRole('region', { name: 'Symbols' })
    const FOLLOWING = Node.DOCUMENT_POSITION_FOLLOWING
    const search = screen.getByRole('region', { name: 'Search' })
    const metrics = screen.getByRole('region', { name: 'Code metrics' })
    expect(search.compareDocumentPosition(symbolsCard) & FOLLOWING).toBeTruthy()
    expect(symbolsCard.compareDocumentPosition(metrics) & FOLLOWING).toBeTruthy()
    for (const name of ['Files', 'Languages', 'Dependencies', 'Secrets', 'File']) {
      expect(screen.getByRole('region', { name })).toBeTruthy()
    }
    await screen.findByRole('table', { name: 'Secret scan summary' }) // page settled
    await sleep(50)
    expect(requests.filter((r) => r.url === SYMBOLS_PATH)).toHaveLength(0)
  })

  it('opens a result at its line through the existing line navigation', async () => {
    const { contentPaths } = renderPage()
    const card = await searchOnPage()
    fireEvent.click(card.getByRole('button', { name: 'Open app/auth.py at line 7 (function authenticate)' }))
    await waitFor(() => expect(highlighted()?.textContent).toBe('def authenticate():'))
    expect(codeText()).toBe(AUTH)
    expect(within(fileCard()).getByText('Showing line 7')).toBeTruthy()
    expect(contentPaths).toEqual(['app/auth.py'])
    expect(scrollIntoViewSpy).toHaveBeenCalledTimes(1) // the existing panel selection
    expect(scrollIntoViewSpy.mock.contexts[0]).toBe(fileCard())
    await waitFor(() => expect(scrollTopSpy).toHaveBeenCalledWith(300))
    const tree = within(screen.getByRole('navigation', { name: 'File tree' }))
    expect(tree.getByRole('button', { name: /auth\.py/ }).getAttribute('aria-current')).toBe('true')
  })

  it('scrolls again for the same result and moves to another line', async () => {
    renderPage()
    const card = await searchOnPage()
    const method = card.getByRole('button', { name: 'Open app/auth.py at line 4 (method authenticate)' })
    fireEvent.click(method)
    await waitFor(() => expect(highlighted()?.textContent).toBe('    def authenticate(self):'))
    await waitFor(() => expect(scrollTopSpy).toHaveBeenCalledTimes(1))
    fireEvent.click(method)
    await waitFor(() => expect(scrollTopSpy).toHaveBeenCalledTimes(2))
    fireEvent.click(card.getByRole('button', { name: 'Open app/auth.py at line 7 (function authenticate)' }))
    await waitFor(() => expect(highlighted()?.textContent).toBe('def authenticate():'))
  })
})
