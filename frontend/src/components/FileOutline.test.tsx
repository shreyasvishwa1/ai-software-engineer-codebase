import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { AxiosError, type InternalAxiosRequestConfig } from 'axios'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { RepositoryPage } from '../pages/RepositoryPage'
import { apiClient } from '../services/client'
import { mockApi, type MockResponse, PROJECT, ZIP_REPOSITORY } from '../test/mockApi'
import { createTestQueryClient } from '../test/render'
import type { CodeOutline, FileContent, OutlineSymbol, TreeDirectory } from '../types/api'
import {
  EMPTY_OUTLINE,
  FileOutline,
  STATUS_MESSAGES,
  TRUNCATED_NOTICE,
  UNKNOWN_STATUS,
} from './FileOutline'
import { FileViewer } from './FileViewer'

const P = PROJECT.id
const R = ZIP_REPOSITORY.id
const BASE = `/projects/${P}/repositories/${R}`
const OUTLINE_PATH = `${BASE}/files/outline`
const GET_OUTLINE = `GET ${OUTLINE_PATH}`
const GET_CONTENT = `GET ${BASE}/files/content`
const HTML = '<img src=x onerror=alert(1)>'

const SOURCE = [
  'import os',
  '',
  'class Widget:',
  '    def render(self):',
  '        return 1',
  '',
  '    async def fetch(self):',
  '        return 2',
  '',
  'def main():',
  '    pass',
].join('\n')

const SYMBOLS: OutlineSymbol[] = [
  { kind: 'class', async: false, name: 'Widget', line: 3, end_line: 8, depth: 0 },
  { kind: 'method', async: false, name: 'render', line: 4, end_line: 5, depth: 1 },
  { kind: 'method', async: true, name: 'fetch', line: 7, end_line: 8, depth: 1 },
  { kind: 'function', async: false, name: 'main', line: 10, end_line: 11, depth: 0 },
]

function outline(extra: Partial<CodeOutline> = {}, path = 'app/widget.py'): CodeOutline {
  return { path, status: 'parsed', symbols: SYMBOLS, truncated: false, ...extra }
}

function content(path: string, text: string | null, extra: Partial<FileContent> = {}): MockResponse {
  const language = path.endsWith('.py') || path.endsWith('.pyi') ? 'Python' : path.endsWith('.md') ? 'Markdown' : null
  return {
    status: 200,
    data: { path, language, size_bytes: text?.length ?? 0, content: text, reason: null, ...extra },
  }
}

type Handler = MockResponse | ((config: InternalAxiosRequestConfig) => MockResponse)

function renderOutline(route: Handler, onSelectLine?: (path: string, line: number) => void) {
  const requests = mockApi({ [GET_OUTLINE]: route })
  const view = render(
    <QueryClientProvider client={createTestQueryClient()}>
      <FileOutline
        projectId={P}
        repositoryId={R}
        path="app/widget.py"
        language="Python"
        onSelectLine={onSelectLine}
      />
    </QueryClientProvider>,
  )
  return { requests, ...view }
}

const showButton = () => screen.getByRole('button', { name: /outline/i }) as HTMLButtonElement
const outlineList = () => screen.findByRole('list', { name: 'Outline' })
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

async function show() {
  fireEvent.click(showButton())
}

async function rows(): Promise<HTMLElement[]> {
  await outlineList()
  return screen.getAllByTestId('outline-symbol')
}

// --- Visibility and requests ---------------------------------------------------------------

describe('FileOutline visibility and requests', () => {
  it('shows the button for Python text files and sends nothing until it is clicked', async () => {
    const seen: InternalAxiosRequestConfig[] = []
    const { requests } = renderOutline((config) => {
      seen.push(config)
      return { status: 200, data: outline() }
    })
    expect(showButton().textContent).toBe('Show outline')
    expect(screen.getByRole('region', { name: 'File outline' })).toBeTruthy()
    await sleep(50)
    expect(requests).toHaveLength(0)

    await show()
    await outlineList()
    expect(requests).toHaveLength(1)
    expect(requests[0].method).toBe('GET')
    expect(requests[0].url).toBe(`/projects/${P}/repositories/${R}/files/outline`)
    expect(requests[0].url).not.toContain('widget')
    expect(seen[0].params).toEqual({ path: 'app/widget.py' })
    expect(showButton().textContent).toBe('Refresh outline')
  })

  it.each([['Markdown'], ['JavaScript'], [null], ['python']])(
    'renders nothing for the language %s',
    async (language) => {
      const requests = mockApi({ [GET_OUTLINE]: { status: 200, data: outline() } })
      const { container } = render(
        <QueryClientProvider client={createTestQueryClient()}>
          <FileOutline projectId={P} repositoryId={R} path="README.md" language={language} />
        </QueryClientProvider>,
      )
      expect(container.innerHTML).toBe('')
      expect(screen.queryByRole('button')).toBeNull()
      await sleep(30)
      expect(requests).toHaveLength(0)
    },
  )

  it('shows the loading state and disables the button while waiting', async () => {
    let resolve: (value: { data: CodeOutline }) => void = () => {}
    const get = vi
      .spyOn(apiClient, 'get')
      .mockImplementation(() => new Promise((r) => (resolve = r as typeof resolve)))
    try {
      render(
        <QueryClientProvider client={createTestQueryClient()}>
          <FileOutline projectId={P} repositoryId={R} path="app/widget.py" language="Python" />
        </QueryClientProvider>,
      )
      await show()
      await waitFor(() => expect(showButton().textContent).toBe('Loading outline…'))
      expect(showButton().disabled).toBe(true)
      fireEvent.click(showButton())
      expect(get).toHaveBeenCalledTimes(1)
      expect(get).toHaveBeenCalledWith(OUTLINE_PATH, { params: { path: 'app/widget.py' } })
      resolve({ data: outline() })
      await outlineList()
      expect(showButton().disabled).toBe(false)
    } finally {
      get.mockRestore()
    }
  })
})

// --- Results -----------------------------------------------------------------------------

describe('FileOutline results', () => {
  it('lists classes, functions and methods in server order with lines and nesting', async () => {
    renderOutline({ status: 200, data: outline() })
    await show()
    const items = await rows()
    expect(items.map((item) => item.textContent)).toEqual([
      'classWidgetline 3–8',
      'methodrenderline 4–5',
      'methodasyncfetchline 7–8',
      'functionmainline 10–11',
    ])
    expect(items.map((item) => item.style.paddingLeft)).toEqual(['0rem', '1rem', '1rem', '0rem'])
    expect(screen.queryByText(TRUNCATED_NOTICE)).toBeNull()
    expect(screen.queryByText(EMPTY_OUTLINE)).toBeNull()
  })

  it('keeps the exact server order', async () => {
    const order: OutlineSymbol[] = [
      { kind: 'function', async: false, name: 'zeta', line: 9, end_line: 9, depth: 0 },
      { kind: 'function', async: false, name: 'alpha', line: 2, end_line: 2, depth: 0 },
    ]
    renderOutline({ status: 200, data: outline({ symbols: order }) })
    await show()
    expect((await rows()).map((item) => item.textContent)).toEqual([
      'functionzetaline 9–9',
      'functionalphaline 2–2',
    ])
  })

  it('caps indentation for malformed depths', async () => {
    const odd = [
      { kind: 'function', async: false, name: 'deep', line: 1, end_line: 1, depth: 50 },
      { kind: 'function', async: false, name: 'negative', line: 2, end_line: 2, depth: -3 },
      { kind: 'function', async: false, name: 'text', line: 3, end_line: 3, depth: '4' },
    ] as unknown as OutlineSymbol[]
    renderOutline({ status: 200, data: outline({ symbols: odd }) })
    await show()
    expect((await rows()).map((item) => item.style.paddingLeft)).toEqual(['8rem', '0rem', '0rem'])
  })

  it('shows the empty outline message', async () => {
    renderOutline({ status: 200, data: outline({ symbols: [] }) })
    await show()
    expect(await screen.findByText(EMPTY_OUTLINE)).toBeTruthy()
    expect(EMPTY_OUTLINE).toBe('No classes or functions found.')
    expect(screen.queryByRole('list', { name: 'Outline' })).toBeNull()
  })

  it('shows the truncated notice only when truncated is true', async () => {
    const { unmount } = renderOutline({ status: 200, data: outline({ truncated: true }) })
    await show()
    expect(await screen.findByText(TRUNCATED_NOTICE)).toBeTruthy()
    expect(TRUNCATED_NOTICE).toBe(
      'The outline is incomplete: at most 2,000 symbols and 8 nesting levels are shown.',
    )
    unmount()
    renderOutline({ status: 200, data: outline({ truncated: false }) })
    await show()
    await outlineList()
    expect(screen.queryByText(TRUNCATED_NOTICE)).toBeNull()
  })

  it.each([
    ['syntax_error', 'The outline is unavailable: this file could not be parsed as Python.'],
    ['too_large', 'The outline is unavailable: this file is too large to outline (limit 256 KiB).'],
    ['binary', 'The outline is unavailable: this file is not text.'],
    ['unsupported_language', 'The outline is only available for Python files.'],
  ] as const)('shows the exact message for %s', async (status, message) => {
    expect(STATUS_MESSAGES[status]).toBe(message)
    renderOutline({ status: 200, data: outline({ status, symbols: [] }) })
    await show()
    expect(await screen.findByText(message)).toBeTruthy()
    expect(screen.queryByRole('list', { name: 'Outline' })).toBeNull()
  })

  it('handles an unknown status safely, even with symbols', async () => {
    renderOutline({ status: 200, data: { ...outline(), status: 'exploded' } })
    await show()
    expect(await screen.findByText(UNKNOWN_STATUS)).toBeTruthy()
    expect(UNKNOWN_STATUS).toBe('The outline is unavailable.')
    expect(screen.queryByRole('list', { name: 'Outline' })).toBeNull()
  })
})

// --- Errors ------------------------------------------------------------------------------

describe('FileOutline errors', () => {
  it.each([
    [401, 'Could not validate credentials'],
    [404, 'File not found'],
    [409, 'File content is not available; try again later'],
    [429, 'Too many attempts; please try again later'],
  ])('shows the exact server detail for %i', async (status, detail) => {
    renderOutline({ status, data: { detail } })
    await show()
    expect((await screen.findByRole('alert')).textContent).toBe(detail)
    expect(showButton().textContent).toBe('Show outline')
  })

  it('shows the validation messages for 422', async () => {
    renderOutline({
      status: 422,
      data: { detail: [{ loc: ['query', 'path'], msg: 'String should have at most 4096 characters' }] },
    })
    await show()
    expect((await screen.findByRole('alert')).textContent).toBe(
      'String should have at most 4096 characters',
    )
  })

  it('uses the existing network error message', async () => {
    renderOutline(() => {
      throw new AxiosError('Network Error', AxiosError.ERR_NETWORK)
    })
    await show()
    expect((await screen.findByRole('alert')).textContent).toBe('Could not reach the server')
  })

  it('falls back to the outline message without a server detail', async () => {
    renderOutline({ status: 500, data: {} })
    await show()
    expect((await screen.findByRole('alert')).textContent).toBe('Could not load the outline')
  })
})

// --- Symbol selection and safety ---------------------------------------------------------

describe('FileOutline symbol selection and safety', () => {
  it('passes the exact path and line of a clicked symbol', async () => {
    const onSelectLine = vi.fn()
    renderOutline({ status: 200, data: outline() }, onSelectLine)
    await show()
    const list = await outlineList()
    const buttons = within(list).getAllByRole('button')
    expect(buttons.map((button) => button.getAttribute('aria-label'))).toEqual([
      'Go to class Widget (line 3)',
      'Go to method render (line 4)',
      'Go to method fetch (line 7)',
      'Go to function main (line 10)',
    ])
    fireEvent.click(buttons[2])
    fireEvent.click(buttons[0])
    fireEvent.click(buttons[0])
    expect(onSelectLine.mock.calls).toEqual([
      ['app/widget.py', 7],
      ['app/widget.py', 3],
      ['app/widget.py', 3],
    ])
  })

  it('makes no button for an invalid line', async () => {
    const odd = [
      { kind: 'function', async: false, name: 'zero', line: 0, end_line: 0, depth: 0 },
      { kind: 'function', async: false, name: 'half', line: 2.5, end_line: 3, depth: 0 },
      { kind: 'function', async: false, name: 'text', line: '4', end_line: 4, depth: 0 },
      { kind: 'function', async: false, name: 'ok', line: 5, end_line: 5, depth: 0 },
    ] as unknown as OutlineSymbol[]
    const onSelectLine = vi.fn()
    renderOutline({ status: 200, data: outline({ symbols: odd }) }, onSelectLine)
    await show()
    const list = await outlineList()
    expect(within(list).getAllByRole('button').map((b) => b.getAttribute('aria-label'))).toEqual([
      'Go to function ok (line 5)',
    ])
    expect(within(list).getAllByTestId('outline-symbol')).toHaveLength(4)
  })

  it('makes symbols plain text without onSelectLine', async () => {
    renderOutline({ status: 200, data: outline() })
    await show()
    const list = await outlineList()
    expect(within(list).queryAllByRole('button')).toHaveLength(0)
  })

  it('renders malicious names and kinds as literal text', async () => {
    const evil = [
      { kind: '<script>x</script>', async: true, name: HTML, line: 1, end_line: 2, depth: 0 },
      { kind: 'function', async: false, name: '<a href="https://example.invalid">x</a>', line: 3, end_line: 3, depth: 0 },
    ] as unknown as OutlineSymbol[]
    renderOutline({ status: 200, data: outline({ symbols: evil }) }, vi.fn())
    await show()
    const items = await rows()
    expect(items[0].textContent).toBe(`<script>x</script>async${HTML}line 1–2`)
    expect(items[1].textContent).toBe('function<a href="https://example.invalid">x</a>line 3–3')
    expect(document.querySelector('img, script, a, iframe')).toBeNull()
    expect(document.querySelector('[href], [src], [onerror]')).toBeNull()
  })

  it('never renders unexpected fields', async () => {
    const extra = { source: 'FAKE-SOURCE-MARKER', docstring: 'FAKE-DOC-MARKER', sha256: 'FAKE-SHA-MARKER', id: 'FAKE-ID-MARKER' }
    const symbols = SYMBOLS.map((symbol) => ({ ...symbol, ...extra }))
    renderOutline({ status: 200, data: { ...outline(), symbols, ...extra } }, vi.fn())
    await show()
    await outlineList()
    const text = document.body.textContent ?? ''
    for (const marker of Object.values(extra)) {
      expect(text).not.toContain(marker)
    }
    for (const button of screen.getAllByRole('button', { name: /^Go to/ })) {
      expect(button.getAttribute('aria-label')).not.toMatch(/FAKE/)
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
      const { requests } = renderOutline({ status: 200, data: outline() }, vi.fn())
      await show()
      await outlineList()
      fireEvent.click(screen.getAllByRole('button', { name: /^Go to/ })[0])
      // The only read is the existing apiClient's access token lookup (services/session.ts).
      expect(getItem.mock.calls).toEqual([['ai_se_access_token']])
      for (const spy of writes) {
        expect(spy).not.toHaveBeenCalled()
      }
      expect(cookieGet).not.toHaveBeenCalled()
      expect(cookieSet).not.toHaveBeenCalled()
      expect(requests.map((r) => `${r.method} ${r.url}`)).toEqual([GET_OUTLINE])
    } finally {
      for (const spy of [getItem, ...writes]) {
        spy.mockRestore()
      }
      delete (document as { cookie?: unknown }).cookie // back to the prototype accessor
    }
  })
})

// --- FileViewer --------------------------------------------------------------------------

function renderViewer(path: string, routes: Record<string, Handler>) {
  const requests = mockApi(routes)
  const client = createTestQueryClient()
  const ui = (selectedPath: string) => (
    <QueryClientProvider client={client}>
      <FileViewer projectId={P} repositoryId={R} selectedPath={selectedPath} onSelectLine={vi.fn()} />
    </QueryClientProvider>
  )
  const view = render(ui(path))
  return { requests, rerenderPath: (next: string) => view.rerender(ui(next)), ...view }
}

describe('FileViewer outline integration', () => {
  it.each([
    ['README.md', '# readme\n', {}],
    ['app/blob.py', null, { reason: 'binary' as const }],
    ['app/huge.py', null, { reason: 'too_large' as const }],
    ['app/empty.py', '', {}],
  ])('shows no outline button for %s', async (path, text, extra) => {
    const { requests } = renderViewer(path, { [GET_CONTENT]: content(path, text, extra) })
    await screen.findByTestId('file-header')
    expect(screen.queryByRole('region', { name: 'File outline' })).toBeNull()
    expect(screen.queryByRole('button', { name: /outline/i })).toBeNull()
    expect(requests.filter((r) => r.url === OUTLINE_PATH)).toHaveLength(0)
  })

  it('keeps Explain this file and the code unchanged next to the outline', async () => {
    renderViewer('app/widget.py', { [GET_CONTENT]: content('app/widget.py', SOURCE) })
    expect(await screen.findByRole('button', { name: 'Explain this file' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Show outline' })).toBeTruthy()
    const code = document.querySelector('pre code') as HTMLElement
    expect(code.textContent).toBe(SOURCE)
    expect(code.childNodes).toHaveLength(1)
  })

  // other.py is opened first so its content is cached: switching back to it renders the
  // viewer immediately (no loading state), which is when the per-path key matters.
  it('starts fresh when another, already loaded file is opened', async () => {
    const { rerenderPath } = renderViewer('app/other.py', {
      [GET_CONTENT]: (config) => {
        const path = (config.params as { path: string }).path
        return content(path, path === 'app/widget.py' ? SOURCE : 'def other():\n    pass\n')
      },
      [GET_OUTLINE]: (config) => ({
        status: 200,
        data: outline({}, (config.params as { path: string }).path),
      }),
    })
    await waitFor(() => expect(document.querySelector('pre code')?.textContent).toBe('def other():\n    pass\n'))
    rerenderPath('app/widget.py')
    await waitFor(() => expect(document.querySelector('pre code')?.textContent).toBe(SOURCE))
    fireEvent.click(await screen.findByRole('button', { name: 'Show outline' }))
    await outlineList()
    rerenderPath('app/other.py')
    await waitFor(() => expect(document.querySelector('pre code')?.textContent).toBe('def other():\n    pass\n'))
    expect(screen.queryByRole('list', { name: 'Outline' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Show outline' })).toBeTruthy()
  })

  it('never shows a slow outline for a file that is no longer open, even a loaded one', async () => {
    let resolveA: (value: { data: CodeOutline }) => void = () => {}
    const realGet = apiClient.get.bind(apiClient)
    const get = vi.spyOn(apiClient, 'get').mockImplementation(((url: string, config?: { params?: { path: string } }) =>
      url === OUTLINE_PATH && config?.params?.path === 'app/widget.py'
        ? new Promise((r) => (resolveA = r as typeof resolveA))
        : realGet(url, config)) as typeof apiClient.get)
    try {
      const { rerenderPath } = renderViewer('app/other.py', {
        [GET_CONTENT]: (config) => {
          const path = (config.params as { path: string }).path
          return content(path, path === 'app/widget.py' ? SOURCE : 'def other():\n    pass\n')
        },
      })
      await waitFor(() => expect(document.querySelector('pre code')?.textContent).toBe('def other():\n    pass\n'))
      rerenderPath('app/widget.py')
      await waitFor(() => expect(document.querySelector('pre code')?.textContent).toBe(SOURCE))
      fireEvent.click(await screen.findByRole('button', { name: 'Show outline' }))
      await waitFor(() => expect(screen.getByRole('button', { name: 'Loading outline…' })).toBeTruthy())
      rerenderPath('app/other.py')
      await waitFor(() => expect(document.querySelector('pre code')?.textContent).toBe('def other():\n    pass\n'))
      resolveA({ data: outline() })
      await sleep(50)
      expect(screen.queryByRole('list', { name: 'Outline' })).toBeNull()
      expect(screen.queryByText('Widget')).toBeNull()
      expect(screen.getByRole('button', { name: 'Show outline' })).toBeTruthy()
    } finally {
      get.mockRestore()
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
        { type: 'file', name: 'widget.py', path: 'app/widget.py', extension: 'py', language: 'Python', size_bytes: 120 },
      ],
    },
    { type: 'file', name: 'README.md', path: 'README.md', extension: 'md', language: 'Markdown', size_bytes: 9 },
  ],
}

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
  const requests = mockApi({
    [`GET ${BASE}`]: { status: 200, data: { ...ZIP_REPOSITORY, status: 'ready' } },
    [`GET ${BASE}/tree`]: {
      status: 200,
      data: { repository_id: R, status: 'ready', file_count: 2, total_bytes: 129, tree: TREE },
    },
    [`GET ${BASE}/languages`]: {
      status: 200,
      data: { repository_id: R, status: 'ready', total_files: 2, total_bytes: 129, languages: [] },
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
    [GET_CONTENT]: (config) => {
      const path = (config.params as { path: string }).path
      return content(path, path === 'app/widget.py' ? SOURCE : '# readme\n')
    },
    [GET_OUTLINE]: { status: 200, data: outline() },
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
  return { requests }
}

const fileCard = () => screen.getByRole('region', { name: 'File' })
const highlighted = () => screen.queryByTestId('selected-line')
const tree = () => within(screen.getByRole('navigation', { name: 'File tree' }))

async function openWidgetOutline() {
  const nav = within(await screen.findByRole('navigation', { name: 'File tree' }))
  fireEvent.click(await nav.findByRole('button', { name: /widget\.py/ }))
  fireEvent.click(await within(fileCard()).findByRole('button', { name: 'Show outline' }))
  await within(fileCard()).findByRole('list', { name: 'Outline' })
}

describe('RepositoryPage outline navigation', () => {
  it('highlights the clicked symbol line without scrolling the File card', async () => {
    const { requests } = renderPage()
    await openWidgetOutline()
    expect(requests.filter((r) => r.url === OUTLINE_PATH)).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: 'Go to method fetch (line 7)' }))
    await waitFor(() => expect(highlighted()?.textContent).toBe('    async def fetch(self):'))
    expect(within(fileCard()).getByText('Showing line 7')).toBeTruthy()
    expect(document.querySelector('pre code')?.textContent).toBe(SOURCE)
    await waitFor(() => expect(scrollTopSpy).toHaveBeenCalledWith(300))
    expect(scrollIntoViewSpy).not.toHaveBeenCalled()
    // The outline stays open while navigating within the same file.
    expect(within(fileCard()).getByRole('list', { name: 'Outline' })).toBeTruthy()
  })

  it('scrolls the code box again when the same symbol is clicked twice', async () => {
    renderPage()
    await openWidgetOutline()
    fireEvent.click(screen.getByRole('button', { name: 'Go to class Widget (line 3)' }))
    await waitFor(() => expect(scrollTopSpy).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByRole('button', { name: 'Go to class Widget (line 3)' }))
    await waitFor(() => expect(scrollTopSpy).toHaveBeenCalledTimes(2))
    expect(highlighted()?.textContent).toBe('class Widget:')
    expect(scrollIntoViewSpy).not.toHaveBeenCalled()
  })

  it('still clears the line on tree selection and resets the outline for another file', async () => {
    renderPage()
    await openWidgetOutline()
    fireEvent.click(screen.getByRole('button', { name: 'Go to function main (line 10)' }))
    await waitFor(() => expect(highlighted()?.textContent).toBe('def main():'))

    fireEvent.click(tree().getByRole('button', { name: /README\.md/ }))
    await waitFor(() => expect(document.querySelector('pre code')?.textContent).toBe('# readme\n'))
    expect(highlighted()).toBeNull()
    expect(screen.queryByRole('region', { name: 'File outline' })).toBeNull() // not Python

    fireEvent.click(tree().getByRole('button', { name: /widget\.py/ }))
    await waitFor(() => expect(document.querySelector('pre code')?.textContent).toBe(SOURCE))
    expect(highlighted()).toBeNull()
    expect(screen.queryByRole('list', { name: 'Outline' })).toBeNull()
    expect(within(fileCard()).getByRole('button', { name: 'Show outline' })).toBeTruthy()
    expect(scrollIntoViewSpy).not.toHaveBeenCalled()
  })
})
