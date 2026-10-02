import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { AxiosError, type InternalAxiosRequestConfig } from 'axios'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { RepositoryPage } from '../pages/RepositoryPage'
import { apiClient } from '../services/client'
import { mockApi, type MockResponse, PROJECT, ZIP_REPOSITORY } from '../test/mockApi'
import { createTestQueryClient } from '../test/render'
import type { CodeImports, CodeOutline, FileContent, ImportEntry, TreeDirectory } from '../types/api'
import {
  FileImports,
  NO_IMPORTS,
  STATUS_MESSAGES,
  TRUNCATED_NOTICE,
  UNKNOWN_STATUS,
} from './FileImports'
import { FileViewer } from './FileViewer'

const P = PROJECT.id
const R = ZIP_REPOSITORY.id
const BASE = `/projects/${P}/repositories/${R}`
const IMPORTS_PATH = `${BASE}/files/imports`
const GET_IMPORTS = `GET ${IMPORTS_PATH}`
const GET_OUTLINE = `GET ${BASE}/files/outline`
const GET_CONTENT = `GET ${BASE}/files/content`
const HTML = '<img src=x onerror=alert(1)>'

const MAIN = 'import os\nfrom app.services import search\nimport utils\n\ndef run():\n    pass\n'

// Server order (source order), deliberately not alphabetical.
const IMPORTS: ImportEntry[] = [
  {
    line: 1,
    statement: 'import',
    module: null,
    level: 0,
    names: ['os'],
    targets: [],
    resolution: 'not_found',
  },
  {
    line: 2,
    statement: 'from',
    module: 'app.services',
    level: 0,
    names: ['search', 'helper'],
    targets: ['app/services/__init__.py', 'app/services/search.py'],
    resolution: 'resolved',
  },
  {
    line: 3,
    statement: 'import',
    module: null,
    level: 0,
    names: ['utils'],
    targets: ['lib/utils.py', 'tools/utils.py'],
    resolution: 'ambiguous',
  },
]

function imports(extra: Partial<CodeImports> = {}, path = 'app/main.py'): CodeImports {
  return { path, status: 'parsed', imports: IMPORTS, truncated: false, ...extra }
}

function content(path: string, text: string | null, extra: Partial<FileContent> = {}): MockResponse {
  const language = path.endsWith('.py') ? 'Python' : path.endsWith('.md') ? 'Markdown' : null
  return {
    status: 200,
    data: { path, language, size_bytes: text?.length ?? 0, content: text, reason: null, ...extra },
  }
}

type Handler = MockResponse | ((config: InternalAxiosRequestConfig) => MockResponse)

function renderImports(route: Handler, onSelectFile?: (path: string) => void) {
  const requests = mockApi({ [GET_IMPORTS]: route })
  const view = render(
    <QueryClientProvider client={createTestQueryClient()}>
      <FileImports
        projectId={P}
        repositoryId={R}
        path="app/main.py"
        language="Python"
        onSelectFile={onSelectFile}
      />
    </QueryClientProvider>,
  )
  return { requests, ...view }
}

const importsButton = () =>
  within(screen.getByRole('region', { name: 'File imports' })).getByRole('button', {
    name: /imports|Retry/,
  }) as HTMLButtonElement
const importList = () => screen.findByRole('list', { name: 'Imports' })
const entries = async () => {
  await importList()
  return screen.getAllByTestId('import-entry')
}
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

// --- Visibility and requests ---------------------------------------------------------------

describe('FileImports visibility and requests', () => {
  it('shows "Show imports" for Python files and requests nothing before the click', async () => {
    const seen: InternalAxiosRequestConfig[] = []
    const { requests } = renderImports((config) => {
      seen.push(config)
      return { status: 200, data: imports() }
    })
    expect(importsButton().textContent).toBe('Show imports')
    await sleep(50)
    expect(requests).toHaveLength(0)

    fireEvent.click(importsButton())
    await importList()
    expect(requests).toHaveLength(1)
    expect(requests[0].method).toBe('GET')
    expect(requests[0].url).toBe(`/projects/${P}/repositories/${R}/files/imports`)
    expect(requests[0].url).not.toContain('main')
    expect(seen[0].params).toEqual({ path: 'app/main.py' })
    expect(importsButton().textContent).toBe('Refresh imports')
  })

  it('uses the exact project, repository and path it is given', async () => {
    const seen: InternalAxiosRequestConfig[] = []
    const requests = mockApi({
      'GET /projects/proj-x/repositories/repo-y/files/imports': (config) => {
        seen.push(config)
        return { status: 200, data: imports({ imports: [] }) }
      },
    })
    const path = `dir with space/${HTML}/ü%2e.py`
    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <FileImports projectId="proj-x" repositoryId="repo-y" path={path} language="Python" />
      </QueryClientProvider>,
    )
    fireEvent.click(importsButton())
    expect(await screen.findByText(NO_IMPORTS)).toBeTruthy()
    expect(requests.map((r) => `${r.method} ${r.url}`)).toEqual([
      'GET /projects/proj-x/repositories/repo-y/files/imports',
    ])
    expect(seen[0].params).toEqual({ path })
  })

  it.each([['Markdown'], ['JavaScript'], [null], ['python']])(
    'renders nothing for the language %s',
    async (language) => {
      const requests = mockApi({ [GET_IMPORTS]: { status: 200, data: imports() } })
      const { container } = render(
        <QueryClientProvider client={createTestQueryClient()}>
          <FileImports projectId={P} repositoryId={R} path="README.md" language={language} />
        </QueryClientProvider>,
      )
      expect(container.childElementCount).toBe(0)
      await sleep(30)
      expect(requests).toHaveLength(0)
    },
  )

  it('shows the loading state and disables the button while waiting', async () => {
    let resolve: (value: { data: CodeImports }) => void = () => {}
    const get = vi
      .spyOn(apiClient, 'get')
      .mockImplementation(() => new Promise((r) => (resolve = r as typeof resolve)))
    try {
      render(
        <QueryClientProvider client={createTestQueryClient()}>
          <FileImports projectId={P} repositoryId={R} path="app/main.py" language="Python" />
        </QueryClientProvider>,
      )
      fireEvent.click(importsButton())
      await waitFor(() => expect(importsButton().textContent).toBe('Loading imports…'))
      expect(importsButton().disabled).toBe(true)
      fireEvent.click(importsButton())
      expect(get).toHaveBeenCalledTimes(1)
      expect(get).toHaveBeenCalledWith(IMPORTS_PATH, { params: { path: 'app/main.py' } })
      resolve({ data: imports() })
      await importList()
      expect(importsButton().disabled).toBe(false)
    } finally {
      get.mockRestore()
    }
  })

  it('refreshes on an explicit second click', async () => {
    let calls = 0
    renderImports(() => {
      calls += 1
      return { status: 200, data: calls === 1 ? imports() : imports({ imports: [IMPORTS[2]] }) }
    })
    fireEvent.click(importsButton())
    expect(await entries()).toHaveLength(3)
    fireEvent.click(importsButton())
    await waitFor(() => expect(screen.getAllByTestId('import-entry')).toHaveLength(1))
    expect(calls).toBe(2)
  })
})

// --- Results -----------------------------------------------------------------------------

describe('FileImports results', () => {
  it('renders every entry in exact server order with all fields', async () => {
    renderImports({ status: 200, data: imports() }, vi.fn())
    fireEvent.click(importsButton())
    const rows = await entries()
    const fields = rows.map((row) => ({
      statement: within(row).getByTestId('import-statement').textContent,
      module: within(row).queryByTestId('import-module')?.textContent ?? null,
      names: within(row).getByTestId('import-names').textContent,
      resolution: within(row).getByTestId('import-resolution').textContent,
      line: within(row).getByText(/^line /).textContent,
      targets: within(row).queryAllByRole('button').map((button) => button.textContent),
    }))
    expect(fields).toEqual([
      { statement: 'import', module: null, names: 'os', resolution: 'not_found', line: 'line 1', targets: [] },
      {
        statement: 'from',
        module: 'app.services',
        names: 'search, helper',
        resolution: 'resolved',
        line: 'line 2',
        targets: ['app/services/__init__.py', 'app/services/search.py'],
      },
      {
        statement: 'import',
        module: null,
        names: 'utils',
        resolution: 'ambiguous',
        line: 'line 3',
        targets: ['lib/utils.py', 'tools/utils.py'],
      },
    ])
  })

  it('keeps the exact server order of entries and targets', async () => {
    const order: ImportEntry[] = [
      { ...IMPORTS[2], line: 9, targets: ['z/utils.py', 'a/utils.py', 'm/utils.py'] },
      { ...IMPORTS[0], line: 2 },
    ]
    renderImports({ status: 200, data: imports({ imports: order }) }, vi.fn())
    fireEvent.click(importsButton())
    const rows = await entries()
    expect(rows.map((row) => within(row).getByText(/^line /).textContent)).toEqual(['line 9', 'line 2'])
    expect(within(rows[0]).getAllByRole('button').map((b) => b.textContent)).toEqual([
      'z/utils.py',
      'a/utils.py',
      'm/utils.py',
    ])
  })

  it('shows the server resolution as given, never derived from the target count', async () => {
    const odd: ImportEntry[] = [
      { ...IMPORTS[1], targets: ['a.py', 'b.py'], resolution: 'resolved' },
      { ...IMPORTS[2], targets: ['only.py'], resolution: 'ambiguous' },
    ]
    renderImports({ status: 200, data: imports({ imports: odd }) }, vi.fn())
    fireEvent.click(importsButton())
    const rows = await entries()
    expect(rows.map((row) => within(row).getByTestId('import-resolution').textContent)).toEqual([
      'resolved',
      'ambiguous',
    ])
    expect(within(rows[1]).getAllByRole('button').map((b) => b.textContent)).toEqual(['only.py'])
  })

  it('shows every ambiguous target and never picks one', async () => {
    const five = ['a/u.py', 'b/u.py', 'c/u.py', 'd/u.py', 'e/u.py']
    renderImports(
      { status: 200, data: imports({ imports: [{ ...IMPORTS[2], targets: five }] }) },
      vi.fn(),
    )
    fireEvent.click(importsButton())
    const [row] = await entries()
    expect(within(row).getAllByRole('button').map((b) => b.textContent)).toEqual(five)
    expect(within(row).getByTestId('import-resolution').textContent).toBe('ambiguous')
  })

  it('shows not_found with nothing to click', async () => {
    renderImports({ status: 200, data: imports({ imports: [IMPORTS[0]] }) }, vi.fn())
    fireEvent.click(importsButton())
    const [row] = await entries()
    expect(within(row).getByTestId('import-resolution').textContent).toBe('not_found')
    expect(within(row).queryAllByRole('button')).toHaveLength(0)
    expect(within(row).queryByRole('list')).toBeNull()
  })

  it('shows "No imports found." for an empty list', async () => {
    renderImports({ status: 200, data: imports({ imports: [] }) })
    fireEvent.click(importsButton())
    expect(await screen.findByText(NO_IMPORTS)).toBeTruthy()
    expect(NO_IMPORTS).toBe('No imports found.')
    expect(screen.queryByRole('list', { name: 'Imports' })).toBeNull()
  })

  it('shows the truncated notice only when truncated is true', async () => {
    const { unmount } = renderImports({ status: 200, data: imports({ truncated: true }) })
    fireEvent.click(importsButton())
    expect(await screen.findByText(TRUNCATED_NOTICE)).toBeTruthy()
    unmount()
    renderImports({ status: 200, data: imports() })
    fireEvent.click(importsButton())
    await importList()
    expect(screen.queryByText(TRUNCATED_NOTICE)).toBeNull()
  })

  it.each([
    ['syntax_error', 'The imports are unavailable: this file could not be parsed as Python.'],
    ['too_large', 'The imports are unavailable: this file is too large to analyze (limit 256 KiB).'],
    ['binary', 'The imports are unavailable: this file is not text.'],
    ['unsupported_language', 'Imports are only available for Python files.'],
  ] as const)('shows the message for %s', async (status, message) => {
    expect(STATUS_MESSAGES[status]).toBe(message)
    renderImports({ status: 200, data: imports({ status, imports: [] }) })
    fireEvent.click(importsButton())
    expect(await screen.findByText(message)).toBeTruthy()
  })

  it('handles an unknown status safely, even with entries', async () => {
    renderImports({ status: 200, data: { ...imports(), status: 'exploded' } })
    fireEvent.click(importsButton())
    expect(await screen.findByText(UNKNOWN_STATUS)).toBeTruthy()
    expect(screen.queryByRole('list', { name: 'Imports' })).toBeNull()
  })
})

// --- Errors ------------------------------------------------------------------------------

describe('FileImports errors', () => {
  it.each([
    [401, 'Could not validate credentials'],
    [404, 'File not found'],
    [409, 'File content is not available; try again later'],
    [429, 'Too many attempts; please try again later'],
  ])('shows the exact server detail for %i', async (status, detail) => {
    renderImports({ status, data: { detail } })
    fireEvent.click(importsButton())
    expect((await screen.findByRole('alert')).textContent).toBe(detail)
    expect(importsButton().textContent).toBe('Retry')
  })

  it('shows the validation messages for 422', async () => {
    renderImports({
      status: 422,
      data: { detail: [{ loc: ['query', 'path'], msg: 'String should have at least 1 character' }] },
    })
    fireEvent.click(importsButton())
    expect((await screen.findByRole('alert')).textContent).toBe('String should have at least 1 character')
  })

  it('uses the existing network and fallback messages', async () => {
    const { unmount } = renderImports(() => {
      throw new AxiosError('Network Error', AxiosError.ERR_NETWORK)
    })
    fireEvent.click(importsButton())
    expect((await screen.findByRole('alert')).textContent).toBe('Could not reach the server')
    unmount()
    renderImports({ status: 500, data: {} })
    fireEvent.click(importsButton())
    expect((await screen.findByRole('alert')).textContent).toBe('Could not load the imports')
  })

  it('retries after an error and clears it on success', async () => {
    let calls = 0
    renderImports(() => {
      calls += 1
      return calls === 1
        ? { status: 429, data: { detail: 'Too many attempts; please try again later' } }
        : { status: 200, data: imports() }
    })
    fireEvent.click(importsButton())
    await screen.findByRole('alert')
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await entries()).toHaveLength(3)
    expect(screen.queryByRole('alert')).toBeNull()
    expect(calls).toBe(2)
  })
})

// --- Navigation and safety ---------------------------------------------------------------

describe('FileImports navigation and safety', () => {
  it('opens the exact target path that was clicked', async () => {
    const onSelectFile = vi.fn()
    // Mixed case, spaces, escapes and encoded-looking parts must all pass through untouched.
    const odd = ` Dir With Space/${HTML}/Ü%2E%2e/Mod.PY `
    const backslash = 'Src\\Win Path\\%20x.py'
    renderImports(
      {
        status: 200,
        data: imports({ imports: [{ ...IMPORTS[2], targets: ['tools/utils.py', odd, backslash] }] }),
      },
      onSelectFile,
    )
    fireEvent.click(importsButton())
    const [row] = await entries()
    const buttons = within(row).getAllByRole('button')
    expect(buttons.map((button) => button.textContent)).toEqual(['tools/utils.py', odd, backslash])
    fireEvent.click(buttons[1])
    fireEvent.click(buttons[2])
    fireEvent.click(buttons[0])
    expect(onSelectFile.mock.calls).toEqual([[odd], [backslash], ['tools/utils.py']])
  })

  it('shows targets as plain text without onSelectFile', async () => {
    renderImports({ status: 200, data: imports() })
    fireEvent.click(importsButton())
    await importList()
    const list = screen.getByRole('list', { name: 'Imports' })
    expect(within(list).queryAllByRole('button')).toHaveLength(0)
    expect(within(list).getByText('lib/utils.py')).toBeTruthy()
  })

  it('renders HTML-like modules, names and targets as literal text', async () => {
    const evil: ImportEntry = {
      line: 1,
      statement: 'from',
      module: '<script>alert(1)</script>',
      level: 0,
      names: [HTML, '<a href="https://example.invalid">x</a>'],
      targets: [`${HTML}.py`],
      resolution: 'resolved',
    }
    renderImports({ status: 200, data: imports({ imports: [evil] }) }, vi.fn())
    fireEvent.click(importsButton())
    const [row] = await entries()
    expect(within(row).getByTestId('import-module').textContent).toBe('<script>alert(1)</script>')
    expect(within(row).getByTestId('import-names').textContent).toBe(
      `${HTML}, <a href="https://example.invalid">x</a>`,
    )
    expect(within(row).getByRole('button').textContent).toBe(`${HTML}.py`)
    expect(document.querySelector('img, script, a, iframe')).toBeNull()
    expect(document.querySelector('[href], [src], [onerror]')).toBeNull()
  })

  it('never renders unexpected fields', async () => {
    const extra = { source: 'FAKE-SOURCE-MARKER', sha256: 'FAKE-SHA-MARKER', id: 'FAKE-ID-MARKER', alias: 'FAKE-ALIAS-MARKER' }
    const withExtra = IMPORTS.map((entry) => ({ ...entry, ...extra }))
    renderImports({ status: 200, data: { ...imports(), imports: withExtra, ...extra } }, vi.fn())
    fireEvent.click(importsButton())
    await importList()
    const text = document.body.textContent ?? ''
    for (const marker of Object.values(extra)) {
      expect(text).not.toContain(marker)
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
      const { requests } = renderImports({ status: 200, data: imports() }, vi.fn())
      fireEvent.click(importsButton())
      await importList()
      fireEvent.click(screen.getByRole('button', { name: 'Open lib/utils.py in the viewer' }))
      // The only read is the existing apiClient's access token lookup (services/session.ts).
      expect(getItem.mock.calls).toEqual([['ai_se_access_token']])
      for (const spy of writes) {
        expect(spy).not.toHaveBeenCalled()
      }
      expect(cookieGet).not.toHaveBeenCalled()
      expect(cookieSet).not.toHaveBeenCalled()
      expect(requests.map((r) => `${r.method} ${r.url}`)).toEqual([GET_IMPORTS])
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
      <FileViewer projectId={P} repositoryId={R} selectedPath={selectedPath} onSelectFile={vi.fn()} />
    </QueryClientProvider>
  )
  const view = render(ui(path))
  return { requests, rerenderPath: (next: string) => view.rerender(ui(next)), ...view }
}

const OTHER = 'import json\n'
const contentRoute: Handler = (config) => {
  const path = (config.params as { path: string }).path
  return content(path, path === 'app/main.py' ? MAIN : OTHER)
}
const codeText = () => document.querySelector('pre code')?.textContent

describe('FileViewer imports integration', () => {
  it.each([
    ['README.md', '# readme\n', {}],
    ['app/blob.py', null, { reason: 'binary' as const }],
    ['app/huge.py', null, { reason: 'too_large' as const }],
    ['app/empty.py', '', {}],
  ])('shows no imports UI for %s', async (path, text, extra) => {
    const { requests } = renderViewer(path, { [GET_CONTENT]: content(path, text, extra) })
    await screen.findByTestId('file-header')
    expect(screen.queryByRole('region', { name: 'File imports' })).toBeNull()
    expect(requests.filter((r) => r.url === IMPORTS_PATH)).toHaveLength(0)
  })

  it('sits below the outline, with Explain and the code unchanged', async () => {
    const { requests } = renderViewer('app/main.py', { [GET_CONTENT]: contentRoute })
    const importsSection = await screen.findByRole('region', { name: 'File imports' })
    const outline = screen.getByRole('region', { name: 'File outline' })
    const explain = screen.getByRole('region', { name: 'File explanation' })
    const FOLLOWING = Node.DOCUMENT_POSITION_FOLLOWING
    expect(explain.compareDocumentPosition(outline) & FOLLOWING).toBeTruthy()
    expect(outline.compareDocumentPosition(importsSection) & FOLLOWING).toBeTruthy()
    const code = document.querySelector('pre code') as HTMLElement
    expect(code.textContent).toBe(MAIN)
    expect(code.childNodes).toHaveLength(1)
    await sleep(50)
    expect(requests.filter((r) => r.url === IMPORTS_PATH)).toHaveLength(0) // not on mount
  })

  it('outline and imports work side by side', async () => {
    const outline: CodeOutline = {
      path: 'app/main.py',
      status: 'parsed',
      symbols: [{ kind: 'function', async: false, name: 'run', line: 5, end_line: 6, depth: 0 }],
      truncated: false,
    }
    renderViewer('app/main.py', {
      [GET_CONTENT]: contentRoute,
      [GET_OUTLINE]: { status: 200, data: outline },
      [GET_IMPORTS]: { status: 200, data: imports() },
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Show outline' }))
    await screen.findByRole('list', { name: 'Outline' })
    fireEvent.click(screen.getByRole('button', { name: 'Show imports' }))
    await importList()
    expect(screen.getByRole('list', { name: 'Outline' })).toBeTruthy()
    expect(screen.getAllByTestId('outline-symbol')).toHaveLength(1)
    expect(screen.getAllByTestId('import-entry')).toHaveLength(3)
  })

  // other.py is opened first so its content is cached: switching back to it renders the
  // viewer immediately (no loading state), which is when the per-path key matters.
  it('starts fresh when another, already loaded file is opened', async () => {
    const { rerenderPath, requests } = renderViewer('app/other.py', {
      [GET_CONTENT]: contentRoute,
      [GET_IMPORTS]: (config) => ({
        status: 200,
        data: imports({}, (config.params as { path: string }).path),
      }),
    })
    await waitFor(() => expect(codeText()).toBe(OTHER))
    rerenderPath('app/main.py')
    await waitFor(() => expect(codeText()).toBe(MAIN))
    fireEvent.click(screen.getByRole('button', { name: 'Show imports' }))
    await importList()
    rerenderPath('app/other.py')
    await waitFor(() => expect(codeText()).toBe(OTHER))
    expect(screen.queryByRole('list', { name: 'Imports' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Show imports' })).toBeTruthy()
    await sleep(50)
    expect(requests.filter((r) => r.url === IMPORTS_PATH)).toHaveLength(1) // none on selection
  })

  it('never shows a slow response for a file that is no longer open, even a loaded one', async () => {
    let resolveMain: (value: { data: CodeImports }) => void = () => {}
    const realGet = apiClient.get.bind(apiClient)
    const get = vi.spyOn(apiClient, 'get').mockImplementation(((url: string, config?: { params?: { path: string } }) =>
      url === IMPORTS_PATH && config?.params?.path === 'app/main.py'
        ? new Promise((r) => (resolveMain = r as typeof resolveMain))
        : realGet(url, config)) as typeof apiClient.get)
    try {
      const { rerenderPath } = renderViewer('app/other.py', { [GET_CONTENT]: contentRoute })
      await waitFor(() => expect(codeText()).toBe(OTHER))
      rerenderPath('app/main.py')
      await waitFor(() => expect(codeText()).toBe(MAIN))
      fireEvent.click(screen.getByRole('button', { name: 'Show imports' }))
      await waitFor(() => expect(screen.getByRole('button', { name: 'Loading imports…' })).toBeTruthy())
      rerenderPath('app/other.py')
      await waitFor(() => expect(codeText()).toBe(OTHER))
      resolveMain({ data: imports() })
      await sleep(50)
      expect(screen.queryByRole('list', { name: 'Imports' })).toBeNull()
      expect(screen.queryByText('tools/utils.py')).toBeNull()
      expect(screen.getByRole('button', { name: 'Show imports' })).toBeTruthy()
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
        { type: 'file', name: 'main.py', path: 'app/main.py', extension: 'py', language: 'Python', size_bytes: 60 },
      ],
    },
    {
      type: 'directory',
      name: 'tools',
      path: 'tools',
      children: [
        { type: 'file', name: 'utils.py', path: 'tools/utils.py', extension: 'py', language: 'Python', size_bytes: 12 },
      ],
    },
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

const UTILS = 'def helper():\n    return 1\n'

function renderPage() {
  const contentPaths: string[] = []
  const requests = mockApi({
    [`GET ${BASE}`]: { status: 200, data: { ...ZIP_REPOSITORY, status: 'ready' } },
    [`GET ${BASE}/tree`]: {
      status: 200,
      data: { repository_id: R, status: 'ready', file_count: 2, total_bytes: 72, tree: TREE },
    },
    [`GET ${BASE}/languages`]: {
      status: 200,
      data: { repository_id: R, status: 'ready', total_files: 2, total_bytes: 72, languages: [] },
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
      contentPaths.push(path)
      return content(path, path === 'app/main.py' ? MAIN : UTILS)
    },
    [GET_OUTLINE]: {
      status: 200,
      data: {
        path: 'app/main.py',
        status: 'parsed',
        symbols: [{ kind: 'function', async: false, name: 'run', line: 5, end_line: 6, depth: 0 }],
        truncated: false,
      },
    },
    [GET_IMPORTS]: { status: 200, data: imports() },
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
const highlighted = () => screen.queryByTestId('selected-line')

async function openMain() {
  const tree = within(await screen.findByRole('navigation', { name: 'File tree' }))
  fireEvent.click(await tree.findByRole('button', { name: /main\.py/ }))
  await waitFor(() => expect(codeText()).toBe(MAIN))
}

describe('RepositoryPage imports navigation', () => {
  it('makes no imports request on page mount or file selection', async () => {
    const { requests } = renderPage()
    await screen.findByRole('table', { name: 'Secret scan summary' }) // page settled
    await openMain()
    await sleep(50)
    expect(requests.filter((r) => r.url === IMPORTS_PATH)).toHaveLength(0)
    expect(within(fileCard()).getByRole('button', { name: 'Show imports' })).toBeTruthy()
  })

  it('opens a clicked target in the viewer through the existing selection', async () => {
    const { contentPaths, requests } = renderPage()
    await openMain()
    fireEvent.click(within(fileCard()).getByRole('button', { name: 'Show imports' }))
    await within(fileCard()).findByRole('list', { name: 'Imports' })
    fireEvent.click(screen.getByRole('button', { name: 'Open tools/utils.py in the viewer' }))
    await waitFor(() => expect(codeText()).toBe(UTILS))
    expect(contentPaths).toEqual(['app/main.py', 'tools/utils.py'])
    const tree = within(screen.getByRole('navigation', { name: 'File tree' }))
    expect(tree.getByRole('button', { name: /utils\.py/ }).getAttribute('aria-current')).toBe('true')
    expect(scrollIntoViewSpy).toHaveBeenCalledTimes(1) // the existing panel selection
    expect(scrollIntoViewSpy.mock.contexts[0]).toBe(fileCard())
    // The newly opened file starts fresh: no automatic imports request for it.
    await sleep(50)
    expect(requests.filter((r) => r.url === IMPORTS_PATH)).toHaveLength(1)
    expect(screen.queryByRole('list', { name: 'Imports' })).toBeNull()
  })

  it('keeps outline line navigation intact next to the imports', async () => {
    renderPage()
    await openMain()
    fireEvent.click(within(fileCard()).getByRole('button', { name: 'Show imports' }))
    await within(fileCard()).findByRole('list', { name: 'Imports' })
    fireEvent.click(within(fileCard()).getByRole('button', { name: 'Show outline' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Go to function run (line 5)' }))
    await waitFor(() => expect(highlighted()?.textContent).toBe('def run():'))
    await waitFor(() => expect(scrollTopSpy).toHaveBeenCalled())
    expect(scrollIntoViewSpy).not.toHaveBeenCalled()
    expect(screen.getByRole('list', { name: 'Imports' })).toBeTruthy() // still shown
  })
})
