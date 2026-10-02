import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { AxiosError, type InternalAxiosRequestConfig } from 'axios'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { RepositoryPage } from '../pages/RepositoryPage'
import { apiClient } from '../services/client'
import { mockApi, type MockResponse, PROJECT, ZIP_REPOSITORY } from '../test/mockApi'
import { createTestQueryClient } from '../test/render'
import type { CodeImporters, FileContent, Importer, ImporterImport, TreeDirectory } from '../types/api'
import {
  FileImporters,
  NO_IMPORTERS,
  statementText,
  summaryText,
  TRUNCATED_NOTICE,
  UNKNOWN_STATUS,
  UNSUPPORTED,
} from './FileImporters'
import { FileViewer } from './FileViewer'

const P = PROJECT.id
const R = ZIP_REPOSITORY.id
const BASE = `/projects/${P}/repositories/${R}`
const IMPORTERS_PATH = `${BASE}/files/importers`
const GET_IMPORTERS = `GET ${IMPORTERS_PATH}`
const GET_IMPORTS = `GET ${BASE}/files/imports`
const GET_OUTLINE = `GET ${BASE}/files/outline`
const GET_CONTENT = `GET ${BASE}/files/content`
const HTML = '<img src=x onerror=alert(1)>'
const CHANGED = 'Repository changed during analysis; run it again'

function statement(extra: Partial<ImporterImport>): ImporterImport {
  return {
    line: 1,
    statement: 'from',
    module: 'app.services',
    level: 0,
    names: ['auth'],
    resolution: 'resolved',
    ...extra,
  }
}

// Server order (by path), deliberately not what a client-side sort would produce.
const IMPORTERS: Importer[] = [
  {
    path: 'app/services/project.py',
    imports: [
      statement({ line: 17, statement: 'import', module: null, names: ['app.services.auth'] }),
    ],
  },
  {
    path: 'app/api/routes/users.py',
    imports: [
      statement({ line: 3 }),
      statement({ line: 8, module: 'services', level: 2, names: ['auth', 'helpers'] }),
      statement({ line: 21, statement: 'import', module: null, names: ['utils'], resolution: 'ambiguous' }),
    ],
  },
]

const SUMMARY = { files_scanned: 12, too_large: 1, binary: 2, syntax_error: 3 }

function importers(extra: Partial<CodeImporters> = {}): CodeImporters {
  return {
    path: 'app/services/auth.py',
    status: 'scanned',
    importers: IMPORTERS,
    summary: SUMMARY,
    truncated: false,
    ...extra,
  }
}

function content(path: string, text: string | null, extra: Partial<FileContent> = {}): MockResponse {
  const language = path.endsWith('.py') ? 'Python' : path.endsWith('.md') ? 'Markdown' : null
  return {
    status: 200,
    data: { path, language, size_bytes: text?.length ?? 0, content: text, reason: null, ...extra },
  }
}

type Handler = MockResponse | ((config: InternalAxiosRequestConfig) => MockResponse)

function renderImporters(route: Handler, onSelectFile?: (path: string) => void) {
  const requests = mockApi({ [GET_IMPORTERS]: route })
  const view = render(
    <QueryClientProvider client={createTestQueryClient()}>
      <FileImporters
        projectId={P}
        repositoryId={R}
        path="app/services/auth.py"
        language="Python"
        onSelectFile={onSelectFile}
      />
    </QueryClientProvider>,
  )
  return { requests, ...view }
}

const section = () => within(screen.getByRole('region', { name: 'File importers' }))
const actionButton = () =>
  section().getByRole('button', { name: /importers|Retry/ }) as HTMLButtonElement
const importerList = () => screen.findByRole('list', { name: 'Importers' })
const importerRows = async () => {
  await importerList()
  return screen.getAllByTestId('importer')
}
const statementsOf = (row: HTMLElement) =>
  within(row).getAllByTestId('importer-statement').map((item) => item.textContent)
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

// --- Statement text ----------------------------------------------------------------------

describe('statementText', () => {
  it('assembles statements only from the returned fields', () => {
    expect(statementText(statement({}))).toBe('from app.services import auth')
    expect(statementText(statement({ module: 'services', level: 2 }))).toBe('from ..services import auth')
    expect(statementText(statement({ module: null, level: 1, names: ['auth', 'search'] }))).toBe(
      'from . import auth, search',
    )
    expect(statementText(statement({ names: ['*'] }))).toBe('from app.services import *')
    expect(statementText(statement({ names: [] }))).toBe('from app.services import')
    expect(statementText(statement({ statement: 'import', module: null, names: ['app.services.auth'] }))).toBe(
      'import app.services.auth',
    )
    expect(statementText(statement({ statement: 'import', module: null, names: ['os', 'sys'] }))).toBe(
      'import os, sys',
    )
    expect(statementText(statement({ statement: 'import', module: null, names: [] }))).toBe('import')
  })

  it('never crashes on a malformed level', () => {
    for (const level of [-1, 2.5, Number.NaN, '3' as unknown as number]) {
      expect(statementText(statement({ level }))).toBe('from app.services import auth')
    }
    expect(statementText(statement({ level: 1_000_000, module: 'x' }))).toBe(`from ${'.'.repeat(100)}x import auth`)
  })

  it('formats the summary from the server values', () => {
    expect(summaryText(SUMMARY)).toBe('Scanned 12 files · 1 too large · 2 binary · 3 with syntax errors.')
  })
})

// --- Visibility and requests ---------------------------------------------------------------

describe('FileImporters visibility and requests', () => {
  it('shows "Show importers" for Python files and requests nothing before the click', async () => {
    const seen: InternalAxiosRequestConfig[] = []
    const { requests } = renderImporters((config) => {
      seen.push(config)
      return { status: 200, data: importers() }
    })
    expect(section().getByRole('heading', { name: 'Imported by' })).toBeTruthy()
    expect(actionButton().textContent).toBe('Show importers')
    await sleep(50)
    expect(requests).toHaveLength(0)

    fireEvent.click(actionButton())
    await importerList()
    expect(requests).toHaveLength(1)
    expect(requests[0].method).toBe('GET')
    expect(requests[0].url).toBe(`/projects/${P}/repositories/${R}/files/importers`)
    expect(requests[0].url).not.toContain('auth')
    expect(seen[0].params).toEqual({ path: 'app/services/auth.py' })
    expect(actionButton().textContent).toBe('Refresh importers')
  })

  it('uses the exact project, repository and path it is given', async () => {
    const seen: InternalAxiosRequestConfig[] = []
    const requests = mockApi({
      'GET /projects/proj-x/repositories/repo-y/files/importers': (config) => {
        seen.push(config)
        return { status: 200, data: importers({ importers: [] }) }
      },
    })
    const path = ` Dir With Space/${HTML}/Ü%2E.py `
    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <FileImporters projectId="proj-x" repositoryId="repo-y" path={path} language="Python" />
      </QueryClientProvider>,
    )
    fireEvent.click(actionButton())
    expect(await screen.findByText(NO_IMPORTERS)).toBeTruthy()
    expect(requests.map((r) => `${r.method} ${r.url}`)).toEqual([
      'GET /projects/proj-x/repositories/repo-y/files/importers',
    ])
    expect(seen[0].params).toEqual({ path })
  })

  it.each([['Markdown'], ['JavaScript'], [null], ['python']])(
    'renders nothing for the language %s',
    async (language) => {
      const requests = mockApi({ [GET_IMPORTERS]: { status: 200, data: importers() } })
      const { container } = render(
        <QueryClientProvider client={createTestQueryClient()}>
          <FileImporters projectId={P} repositoryId={R} path="README.md" language={language} />
        </QueryClientProvider>,
      )
      expect(container.childElementCount).toBe(0)
      await sleep(30)
      expect(requests).toHaveLength(0)
    },
  )

  it('shows the loading state, disables the button and ignores a second click', async () => {
    let resolve: (value: { data: CodeImporters }) => void = () => {}
    const get = vi
      .spyOn(apiClient, 'get')
      .mockImplementation(() => new Promise((r) => (resolve = r as typeof resolve)))
    try {
      render(
        <QueryClientProvider client={createTestQueryClient()}>
          <FileImporters projectId={P} repositoryId={R} path="app/services/auth.py" language="Python" />
        </QueryClientProvider>,
      )
      fireEvent.click(actionButton())
      await waitFor(() => expect(actionButton().textContent).toBe('Loading importers…'))
      expect(actionButton().disabled).toBe(true)
      fireEvent.click(actionButton())
      expect(get).toHaveBeenCalledTimes(1)
      expect(get).toHaveBeenCalledWith(IMPORTERS_PATH, { params: { path: 'app/services/auth.py' } })
      resolve({ data: importers() })
      await importerList()
      expect(actionButton().disabled).toBe(false)
    } finally {
      get.mockRestore()
    }
  })

  it('refreshes on an explicit second click', async () => {
    let calls = 0
    renderImporters(() => {
      calls += 1
      return { status: 200, data: calls === 1 ? importers() : importers({ importers: [IMPORTERS[1]] }) }
    })
    fireEvent.click(actionButton())
    expect(await importerRows()).toHaveLength(2)
    fireEvent.click(actionButton())
    await waitFor(() => expect(screen.getAllByTestId('importer')).toHaveLength(1))
    expect(calls).toBe(2)
  })
})

// --- Results -----------------------------------------------------------------------------

describe('FileImporters results', () => {
  it('lists importers and every statement in exact server order', async () => {
    renderImporters({ status: 200, data: importers() }, vi.fn())
    fireEvent.click(actionButton())
    const rows = await importerRows()
    expect(rows.map((row) => within(row).getByRole('button').textContent)).toEqual([
      'app/services/project.py',
      'app/api/routes/users.py',
    ])
    expect(statementsOf(rows[0])).toEqual(['line 17 · import app.services.auth · resolved'])
    expect(statementsOf(rows[1])).toEqual([
      'line 3 · from app.services import auth · resolved',
      'line 8 · from ..services import auth, helpers · resolved',
      'line 21 · import utils · ambiguous',
    ])
  })

  it('keeps each importer once with all its statements, without filtering duplicates', async () => {
    const repeated: Importer = {
      path: 'b/user.py',
      imports: [statement({ line: 9 }), statement({ line: 2 }), statement({ line: 9 })],
    }
    const twice = [repeated, { ...repeated }]
    renderImporters({ status: 200, data: importers({ importers: twice }) }, vi.fn())
    fireEvent.click(actionButton())
    const rows = await importerRows()
    expect(rows).toHaveLength(2) // exactly what the server returned
    expect(statementsOf(rows[0]).map((text) => text?.split(' · ')[0])).toEqual(['line 9', 'line 2', 'line 9'])
  })

  it('shows the server resolution even when it looks inconsistent', async () => {
    const odd: Importer = {
      path: 'b/user.py',
      imports: [
        statement({ line: 1, statement: 'import', module: null, names: ['a.b'], resolution: 'ambiguous' }),
        statement({ line: 2, resolution: 'not_found' }),
      ],
    }
    renderImporters({ status: 200, data: importers({ importers: [odd] }) }, vi.fn())
    fireEvent.click(actionButton())
    const [row] = await importerRows()
    expect(statementsOf(row)).toEqual([
      'line 1 · import a.b · ambiguous',
      'line 2 · from app.services import auth · not_found',
    ])
  })

  it('shows the empty state and the summary', async () => {
    renderImporters({ status: 200, data: importers({ importers: [] }) })
    fireEvent.click(actionButton())
    expect(await screen.findByText(NO_IMPORTERS)).toBeTruthy()
    expect(NO_IMPORTERS).toBe('No importers found.')
    expect(screen.queryByRole('list', { name: 'Importers' })).toBeNull()
    expect(screen.getByTestId('importers-summary').textContent).toBe(
      'Scanned 12 files · 1 too large · 2 binary · 3 with syntax errors.',
    )
    expect(screen.queryByText(TRUNCATED_NOTICE)).toBeNull()
  })

  it('shows the truncated notice only when truncated, also with an empty list', async () => {
    const { unmount } = renderImporters({ status: 200, data: importers({ truncated: true, importers: [] }) })
    fireEvent.click(actionButton())
    expect(await screen.findByText(TRUNCATED_NOTICE)).toBeTruthy()
    expect(TRUNCATED_NOTICE).toBe(
      'The importer list may be incomplete: a scan or result limit was reached (at most 200 importers and 20 statements each).',
    )
    expect(screen.getByText(NO_IMPORTERS)).toBeTruthy()
    unmount()
    renderImporters({ status: 200, data: importers({ truncated: true }) })
    fireEvent.click(actionButton())
    await importerList()
    expect(screen.getByText(TRUNCATED_NOTICE)).toBeTruthy()
  })

  it('shows the summary counts exactly as returned', async () => {
    const summary = { files_scanned: 0, too_large: 99, binary: 7, syntax_error: 1 }
    renderImporters({ status: 200, data: importers({ summary }) })
    fireEvent.click(actionButton())
    await importerList()
    expect(screen.getByTestId('importers-summary').textContent).toBe(
      'Scanned 0 files · 99 too large · 7 binary · 1 with syntax errors.',
    )
  })

  it('handles unsupported_language and unknown statuses safely', async () => {
    const { unmount } = renderImporters({
      status: 200,
      data: importers({ status: 'unsupported_language', importers: [] }),
    })
    fireEvent.click(actionButton())
    expect(await screen.findByText(UNSUPPORTED)).toBeTruthy()
    unmount()
    renderImporters({ status: 200, data: { ...importers(), status: 'exploded' } })
    fireEvent.click(actionButton())
    expect(await screen.findByText(UNKNOWN_STATUS)).toBeTruthy()
    expect(screen.queryByRole('list', { name: 'Importers' })).toBeNull()
  })
})

// --- Errors ------------------------------------------------------------------------------

describe('FileImporters errors', () => {
  it('shows the exact 409 detail, no partial results, and a Retry', async () => {
    renderImporters({ status: 409, data: { detail: CHANGED, importers: IMPORTERS } })
    fireEvent.click(actionButton())
    expect((await screen.findByRole('alert')).textContent).toBe(CHANGED)
    expect(actionButton().textContent).toBe('Retry')
    expect(screen.queryByRole('list', { name: 'Importers' })).toBeNull()
    expect(screen.queryByText('app/services/project.py')).toBeNull()
  })

  it.each([
    [401, 'Could not validate credentials'],
    [404, 'File not found'],
    [429, 'Too many attempts; please try again later'],
  ])('shows the exact server detail for %i', async (status, detail) => {
    renderImporters({ status, data: { detail } })
    fireEvent.click(actionButton())
    expect((await screen.findByRole('alert')).textContent).toBe(detail)
  })

  it('shows the validation messages for 422', async () => {
    renderImporters({
      status: 422,
      data: { detail: [{ loc: ['query', 'path'], msg: 'String should have at least 1 character' }] },
    })
    fireEvent.click(actionButton())
    expect((await screen.findByRole('alert')).textContent).toBe('String should have at least 1 character')
  })

  it('uses the existing network and fallback messages', async () => {
    const { unmount } = renderImporters(() => {
      throw new AxiosError('Network Error', AxiosError.ERR_NETWORK)
    })
    fireEvent.click(actionButton())
    expect((await screen.findByRole('alert')).textContent).toBe('Could not reach the server')
    unmount()
    renderImporters({ status: 500, data: {} })
    fireEvent.click(actionButton())
    expect((await screen.findByRole('alert')).textContent).toBe('Could not load the importers')
  })

  it('retries after a 409 and clears the error on success', async () => {
    let calls = 0
    renderImporters(() => {
      calls += 1
      return calls === 1 ? { status: 409, data: { detail: CHANGED } } : { status: 200, data: importers() }
    })
    fireEvent.click(actionButton())
    await screen.findByRole('alert')
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await importerRows()).toHaveLength(2)
    expect(screen.queryByRole('alert')).toBeNull()
    expect(calls).toBe(2)
  })
})

// --- Navigation and safety ---------------------------------------------------------------

describe('FileImporters navigation and safety', () => {
  it('opens the exact importer path that was clicked', async () => {
    const onSelectFile = vi.fn()
    const odd = [
      'App/Services/Mixed Case.py',
      '  padded/path.py  ',
      'Src\\Win Path\\x.py',
      'enc/%2E%2e/%20x.py',
    ]
    renderImporters(
      { status: 200, data: importers({ importers: odd.map((path) => ({ path, imports: [statement({})] })) }) },
      onSelectFile,
    )
    fireEvent.click(actionButton())
    const rows = await importerRows()
    const buttons = rows.map((row) => within(row).getByRole('button'))
    expect(buttons.map((button) => button.textContent)).toEqual(odd)
    expect(buttons.map((button) => button.getAttribute('aria-label'))).toEqual(
      odd.map((path) => `Open ${path} in the viewer`),
    )
    for (const button of [...buttons].reverse()) {
      fireEvent.click(button)
    }
    expect(onSelectFile.mock.calls).toEqual([...odd].reverse().map((path) => [path]))
  })

  it('shows importer paths as plain text without onSelectFile', async () => {
    renderImporters({ status: 200, data: importers() })
    fireEvent.click(actionButton())
    const list = await importerList()
    expect(within(list).queryAllByRole('button')).toHaveLength(0)
    expect(within(list).getByText('app/api/routes/users.py')).toBeTruthy()
  })

  it('renders HTML-like paths, modules and names as literal text', async () => {
    const evil: Importer = {
      path: `${HTML}.py`,
      imports: [
        statement({ module: '<script>alert(1)</script>', names: ['<a href="https://example.invalid">x</a>'] }),
      ],
    }
    renderImporters({ status: 200, data: importers({ importers: [evil] }) }, vi.fn())
    fireEvent.click(actionButton())
    const [row] = await importerRows()
    expect(within(row).getByRole('button').textContent).toBe(`${HTML}.py`)
    expect(statementsOf(row)).toEqual([
      'line 1 · from <script>alert(1)</script> import <a href="https://example.invalid">x</a> · resolved',
    ])
    expect(document.querySelector('img, script, a, iframe')).toBeNull()
    expect(document.querySelector('[href], [src], [onerror]')).toBeNull()
  })

  it('never renders unexpected fields', async () => {
    const extra = { source: 'FAKE-SOURCE-MARKER', sha256: 'FAKE-SHA-MARKER', id: 'FAKE-ID-MARKER', alias: 'FAKE-ALIAS-MARKER' }
    const withExtra = IMPORTERS.map((importer) => ({
      ...importer,
      ...extra,
      imports: importer.imports.map((entry) => ({ ...entry, ...extra })),
    }))
    renderImporters(
      { status: 200, data: { ...importers(), importers: withExtra, summary: { ...SUMMARY, ...extra }, ...extra } },
      vi.fn(),
    )
    fireEvent.click(actionButton())
    await importerList()
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
      const { requests } = renderImporters({ status: 200, data: importers() }, vi.fn())
      fireEvent.click(actionButton())
      await importerList()
      fireEvent.click(screen.getByRole('button', { name: 'Open app/api/routes/users.py in the viewer' }))
      // The only read is the existing apiClient's access token lookup (services/session.ts).
      expect(getItem.mock.calls).toEqual([['ai_se_access_token']])
      for (const spy of writes) {
        expect(spy).not.toHaveBeenCalled()
      }
      expect(cookieGet).not.toHaveBeenCalled()
      expect(cookieSet).not.toHaveBeenCalled()
      expect(requests.map((r) => `${r.method} ${r.url}`)).toEqual([GET_IMPORTERS])
    } finally {
      for (const spy of [getItem, ...writes]) {
        spy.mockRestore()
      }
      delete (document as { cookie?: unknown }).cookie // back to the prototype accessor
    }
  })
})

// --- FileViewer --------------------------------------------------------------------------

const AUTH = 'def login():\n    pass\n'
const OTHER = 'import json\n'
const contentRoute: Handler = (config) => {
  const path = (config.params as { path: string }).path
  return content(path, path === 'app/services/auth.py' ? AUTH : OTHER)
}
const codeText = () => document.querySelector('pre code')?.textContent

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

describe('FileViewer importers integration', () => {
  it.each([
    ['README.md', '# readme\n', {}],
    ['app/blob.py', null, { reason: 'binary' as const }],
    ['app/huge.py', null, { reason: 'too_large' as const }],
    ['app/empty.py', '', {}],
  ])('shows no importers section for %s', async (path, text, extra) => {
    const { requests } = renderViewer(path, { [GET_CONTENT]: content(path, text, extra) })
    await screen.findByTestId('file-header')
    expect(screen.queryByRole('region', { name: 'File importers' })).toBeNull()
    expect(requests.filter((r) => r.url === IMPORTERS_PATH)).toHaveLength(0)
  })

  it('sits after Explain, Outline and Imports, with the code unchanged and no request', async () => {
    const { requests } = renderViewer('app/services/auth.py', { [GET_CONTENT]: contentRoute })
    const importersSection = await screen.findByRole('region', { name: 'File importers' })
    const explain = screen.getByRole('region', { name: 'File explanation' })
    const outline = screen.getByRole('region', { name: 'File outline' })
    const imports = screen.getByRole('region', { name: 'File imports' })
    const FOLLOWING = Node.DOCUMENT_POSITION_FOLLOWING
    expect(explain.compareDocumentPosition(outline) & FOLLOWING).toBeTruthy()
    expect(outline.compareDocumentPosition(imports) & FOLLOWING).toBeTruthy()
    expect(imports.compareDocumentPosition(importersSection) & FOLLOWING).toBeTruthy()
    const code = document.querySelector('pre code') as HTMLElement
    expect(code.textContent).toBe(AUTH)
    expect(code.childNodes).toHaveLength(1)
    await sleep(50)
    expect(requests.filter((r) => r.url === IMPORTERS_PATH)).toHaveLength(0)
  })

  it('works side by side with the Step 40 imports', async () => {
    renderViewer('app/services/auth.py', {
      [GET_CONTENT]: contentRoute,
      [GET_IMPORTERS]: { status: 200, data: importers() },
      [GET_IMPORTS]: {
        status: 200,
        data: {
          path: 'app/services/auth.py',
          status: 'parsed',
          imports: [
            { line: 1, statement: 'import', module: null, level: 0, names: ['os'], targets: [], resolution: 'not_found' },
          ],
          truncated: false,
        },
      },
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Show imports' }))
    await screen.findByRole('list', { name: 'Imports' })
    fireEvent.click(screen.getByRole('button', { name: 'Show importers' }))
    await importerList()
    expect(screen.getAllByTestId('import-entry')).toHaveLength(1)
    expect(screen.getAllByTestId('importer')).toHaveLength(2)
  })

  // other.py is opened first so its content is cached: switching back to it renders the
  // viewer immediately (no loading state), which is when the per-path key matters.
  it('starts fresh when another, already loaded file is opened', async () => {
    const { rerenderPath, requests } = renderViewer('app/other.py', {
      [GET_CONTENT]: contentRoute,
      [GET_IMPORTERS]: { status: 200, data: importers() },
    })
    await waitFor(() => expect(codeText()).toBe(OTHER))
    rerenderPath('app/services/auth.py')
    await waitFor(() => expect(codeText()).toBe(AUTH))
    fireEvent.click(screen.getByRole('button', { name: 'Show importers' }))
    await importerList()
    rerenderPath('app/other.py')
    await waitFor(() => expect(codeText()).toBe(OTHER))
    expect(screen.queryByRole('list', { name: 'Importers' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Show importers' })).toBeTruthy()
    await sleep(50)
    expect(requests.filter((r) => r.url === IMPORTERS_PATH)).toHaveLength(1)
  })

  it('never shows a slow response for a file that is no longer open, even a loaded one', async () => {
    let resolveAuth: (value: { data: CodeImporters }) => void = () => {}
    const realGet = apiClient.get.bind(apiClient)
    const get = vi.spyOn(apiClient, 'get').mockImplementation(((url: string, config?: { params?: { path: string } }) =>
      url === IMPORTERS_PATH && config?.params?.path === 'app/services/auth.py'
        ? new Promise((r) => (resolveAuth = r as typeof resolveAuth))
        : realGet(url, config)) as typeof apiClient.get)
    try {
      const { rerenderPath } = renderViewer('app/other.py', { [GET_CONTENT]: contentRoute })
      await waitFor(() => expect(codeText()).toBe(OTHER))
      rerenderPath('app/services/auth.py')
      await waitFor(() => expect(codeText()).toBe(AUTH))
      fireEvent.click(screen.getByRole('button', { name: 'Show importers' }))
      await waitFor(() => expect(screen.getByRole('button', { name: 'Loading importers…' })).toBeTruthy())
      rerenderPath('app/other.py')
      await waitFor(() => expect(codeText()).toBe(OTHER))
      resolveAuth({ data: importers() })
      await sleep(50)
      expect(screen.queryByRole('list', { name: 'Importers' })).toBeNull()
      expect(screen.queryByText('app/api/routes/users.py')).toBeNull()
      expect(screen.getByRole('button', { name: 'Show importers' })).toBeTruthy()
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
        { type: 'file', name: 'auth.py', path: 'app/auth.py', extension: 'py', language: 'Python', size_bytes: 22 },
        { type: 'file', name: 'users.py', path: 'app/users.py', extension: 'py', language: 'Python', size_bytes: 40 },
      ],
    },
  ],
}

const USERS = 'from app import auth\n\ndef show():\n    return auth\n'
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
      data: { repository_id: R, status: 'ready', file_count: 2, total_bytes: 62, tree: TREE },
    },
    [`GET ${BASE}/languages`]: {
      status: 200,
      data: { repository_id: R, status: 'ready', total_files: 2, total_bytes: 62, languages: [] },
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
      return content(path, path === 'app/auth.py' ? AUTH : USERS)
    },
    [GET_OUTLINE]: {
      status: 200,
      data: {
        path: 'app/auth.py',
        status: 'parsed',
        symbols: [{ kind: 'function', async: false, name: 'login', line: 1, end_line: 2, depth: 0 }],
        truncated: false,
      },
    },
    [GET_IMPORTERS]: {
      status: 200,
      data: importers({
        path: 'app/auth.py',
        importers: [{ path: 'app/users.py', imports: [statement({ line: 1, module: 'app', names: ['auth'] })] }],
      }),
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

async function openAuth() {
  const tree = within(await screen.findByRole('navigation', { name: 'File tree' }))
  fireEvent.click(await tree.findByRole('button', { name: /auth\.py/ }))
  await waitFor(() => expect(codeText()).toBe(AUTH))
}

describe('RepositoryPage imported-by navigation', () => {
  it('makes no importers request on page mount or file selection', async () => {
    const { requests } = renderPage()
    await screen.findByRole('table', { name: 'Secret scan summary' }) // page settled
    await openAuth()
    await sleep(50)
    expect(requests.filter((r) => r.url === IMPORTERS_PATH)).toHaveLength(0)
    expect(within(fileCard()).getByRole('button', { name: 'Show importers' })).toBeTruthy()
  })

  it('opens a clicked importer through the existing selection', async () => {
    const { contentPaths, requests } = renderPage()
    await openAuth()
    fireEvent.click(within(fileCard()).getByRole('button', { name: 'Show importers' }))
    await within(fileCard()).findByRole('list', { name: 'Importers' })
    fireEvent.click(screen.getByRole('button', { name: 'Open app/users.py in the viewer' }))
    await waitFor(() => expect(codeText()).toBe(USERS))
    expect(contentPaths).toEqual(['app/auth.py', 'app/users.py'])
    const tree = within(screen.getByRole('navigation', { name: 'File tree' }))
    expect(tree.getByRole('button', { name: /users\.py/ }).getAttribute('aria-current')).toBe('true')
    expect(scrollIntoViewSpy).toHaveBeenCalledTimes(1) // the existing panel selection
    expect(scrollIntoViewSpy.mock.contexts[0]).toBe(fileCard())
    await sleep(50)
    expect(requests.filter((r) => r.url === IMPORTERS_PATH)).toHaveLength(1) // none for the new file
    expect(screen.queryByRole('list', { name: 'Importers' })).toBeNull()
  })

  it('keeps outline line navigation intact next to the importers', async () => {
    renderPage()
    await openAuth()
    fireEvent.click(within(fileCard()).getByRole('button', { name: 'Show importers' }))
    await within(fileCard()).findByRole('list', { name: 'Importers' })
    fireEvent.click(within(fileCard()).getByRole('button', { name: 'Show outline' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Go to function login (line 1)' }))
    await waitFor(() => expect(screen.queryByTestId('selected-line')?.textContent).toBe('def login():'))
    await waitFor(() => expect(scrollTopSpy).toHaveBeenCalled())
    expect(scrollIntoViewSpy).not.toHaveBeenCalled()
    expect(screen.getByRole('list', { name: 'Importers' })).toBeTruthy()
  })
})
