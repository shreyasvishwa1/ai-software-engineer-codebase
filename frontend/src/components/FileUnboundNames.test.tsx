import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { AxiosError, type AxiosRequestConfig, type InternalAxiosRequestConfig } from 'axios'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { apiClient } from '../services/client'
import { mockApi, type MockResponse, PROJECT, type RecordedRequest, ZIP_REPOSITORY } from '../test/mockApi'
import { createTestQueryClient } from '../test/render'
import type { FileContent, UnboundName, UnboundNamesResult, UnboundNamesSummary } from '../types/api'
import {
  EMPTY_RESULT,
  FileUnboundNames,
  HEADING,
  STATUS_MESSAGES,
  TRUNCATED_NOTICE,
  UNKNOWN_STATUS,
  starImportNote,
  unboundSummary,
} from './FileUnboundNames'
import { FileViewer, type LineSelection } from './FileViewer'

const P = PROJECT.id
const R = ZIP_REPOSITORY.id
const BASE = `/projects/${P}/repositories/${R}`
const UNBOUND_PATH = `${BASE}/files/unbound-names`
const GET_UNBOUND = `GET ${UNBOUND_PATH}`
const GET_CONTENT = `GET ${BASE}/files/content`

const MAIN = 'import os\nprint(os, missing)\n'
const OTHER = 'print(other_missing)\n'

// The approved wording, written here independently of the component.
const HEADING_TEXT = 'Names with no statically known binding'
const EMPTY_TEXT = 'No unbound names found.'
const TRUNCATED_TEXT = 'Unbound name results are truncated.'
const UNKNOWN_TEXT = 'Unbound names are unavailable.'
const FALLBACK = 'Could not check unbound names'
const MESSAGES = {
  syntax_error: 'Unbound names are unavailable: this file could not be parsed as Python.',
  too_large: 'Unbound names are unavailable: this file is too large to analyze (limit 256 KiB).',
  binary: 'Unbound names are unavailable: this file is not text.',
  unsupported_language: 'Unbound names are only available for Python files.',
}

// Server order, deliberately not sorted by name or line, with a duplicate name.
const NAMES: UnboundName[] = [
  { line: 2, column: 11, name: 'missing' },
  { line: 4, column: 7, name: 'zeta' },
  { line: 8, column: 3, name: 'missing' },
  { line: 9, column: 1, name: 'alpha' },
]
const ROW_TEXTS = [
  'missing line 2, column 11',
  'zeta line 4, column 7',
  'missing line 8, column 3',
  'alpha line 9, column 1',
]
const ROW_LABELS = [
  'Go to missing at line 2, column 11',
  'Go to zeta at line 4, column 7',
  'Go to missing at line 8, column 3',
  'Go to alpha at line 9, column 1',
]
// Every count differs from the rows (4 occurrences, 3 distinct), so a recount shows.
const SUMMARY: UnboundNamesSummary = { unbound: 7, distinct: 5, unsupported: 3, implicit: 2 }
const SUMMARY_TEXT = 'Unbound: 7 occurrences of 5 names.'
const STAR_NOTE = '3 names may come from a star import and are not listed.'
const ZERO: UnboundNamesSummary = { unbound: 0, distinct: 0, unsupported: 0, implicit: 0 }

function unboundResult(extra: Partial<UnboundNamesResult> = {}): UnboundNamesResult {
  return { path: 'app/main.py', status: 'parsed', names: NAMES, summary: SUMMARY, truncated: false, ...extra }
}

function notParsed(status: string): UnboundNamesResult {
  return {
    path: 'app/main.py',
    status: status as UnboundNamesResult['status'],
    names: [],
    summary: ZERO,
    truncated: false,
  }
}

function content(path: string, text: string | null, extra: Partial<FileContent> = {}): MockResponse {
  const language = path.endsWith('.py') ? 'Python' : path.endsWith('.md') ? 'Markdown' : null
  return {
    status: 200,
    data: { path, language, size_bytes: text?.length ?? 0, content: text, reason: null, ...extra },
  }
}

const contentRoute = (config: InternalAxiosRequestConfig): MockResponse => {
  const path = (config.params as { path: string }).path
  return content(path, path === 'app/main.py' ? MAIN : path === 'app/other.py' ? OTHER : '# Notes\n')
}

type Handler = MockResponse | ((config: InternalAxiosRequestConfig) => MockResponse)
type Params = Record<string, unknown>
type Navigate = (path: string, line: number) => void

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))
const codeText = () => document.querySelector('pre code')?.textContent
const region = () => screen.getByRole('region', { name: 'Unbound names' })
const actionButton = () => within(region()).getByRole('button', { name: /unbound names/i }) as HTMLButtonElement
const rowTexts = () => screen.queryAllByTestId('unbound-name').map((row) => row.textContent)
const rowButtons = () => within(region()).queryAllByRole('button', { name: /^Go to / }) as HTMLButtonElement[]
const regionText = () => region().textContent ?? ''
const byUrl = (requests: RecordedRequest[], url: string) => requests.filter((r) => r.url === url)
const loaded = () =>
  waitFor(() =>
    expect(
      screen.queryByRole('button', { name: 'Refresh unbound names' }) ?? screen.queryByRole('alert'),
    ).not.toBeNull(),
  )

function renderPanel(
  handler: Handler = { status: 200, data: unboundResult() },
  props: { path?: string; language?: string | null; onSelectLine?: Navigate } = {},
) {
  const seen: Params[] = []
  const requests = mockApi({
    [GET_UNBOUND]: (config) => {
      seen.push({ ...(config.params as Params) })
      return typeof handler === 'function' ? handler(config) : handler
    },
  })
  const view = render(
    <QueryClientProvider client={createTestQueryClient()}>
      <FileUnboundNames
        projectId={P}
        repositoryId={R}
        path={props.path ?? 'app/main.py'}
        language={props.language === undefined ? 'Python' : props.language}
        onSelectLine={props.onSelectLine}
      />
    </QueryClientProvider>,
  )
  return { seen, requests, ...view }
}

/** Shows `data` (any JSON) in a bare panel after one explicit click. */
async function panelResult(data: unknown, props: { path?: string; onSelectLine?: Navigate } = {}) {
  const view = renderPanel({ status: 200, data }, props)
  fireEvent.click(actionButton())
  await loaded()
  return view
}

function renderViewer(path: string, routes: Record<string, Handler>) {
  const requests = mockApi(routes)
  const client = createTestQueryClient()
  const onSelectLine = vi.fn<Navigate>()
  const ui = (selectedPath: string, selectedLine: LineSelection | null = null) => (
    <QueryClientProvider client={client}>
      <FileViewer
        projectId={P}
        repositoryId={R}
        selectedPath={selectedPath}
        selectedLine={selectedLine}
        onSelectLine={onSelectLine}
        onSelectFile={vi.fn()}
      />
    </QueryClientProvider>
  )
  const view = render(ui(path))
  return {
    requests,
    onSelectLine,
    rerenderPath: (next: string, line: LineSelection | null = null) => view.rerender(ui(next, line)),
    ...view,
  }
}

/** Holds every unbound-names request until released or failed; others go through. */
function holdUnbound() {
  const held: { params: Params; release: (data: UnboundNamesResult) => void }[] = []
  const realGet = apiClient.get.bind(apiClient)
  const spy = vi.spyOn(apiClient, 'get').mockImplementation(((url: string, config?: AxiosRequestConfig) => {
    if (url === UNBOUND_PATH) {
      return new Promise<unknown>((resolve) => {
        held.push({ params: { ...(config?.params as Params) }, release: (data) => resolve({ data }) })
      })
    }
    return realGet(url, config)
  }) as typeof apiClient.get)
  return { held, spy }
}

afterEach(() => {
  vi.restoreAllMocks()
})

// --- A. Visibility ------------------------------------------------------------------------------

describe('Unbound names visibility', () => {
  it('shows the panel for a Python file without requesting anything on open or line selection', async () => {
    const { requests, rerenderPath } = renderViewer('app/main.py', { [GET_CONTENT]: contentRoute })
    await waitFor(() => expect(codeText()).toBe(MAIN))
    expect([actionButton().textContent, actionButton().disabled]).toEqual(['Find unbound names', false])
    rerenderPath('app/main.py', { line: 2 }) // a line selected in the open file
    fireEvent.mouseUp(document.querySelector('pre') as HTMLPreElement)
    await sleep(50)
    expect(byUrl(requests, UNBOUND_PATH)).toHaveLength(0)
    expect(screen.queryByText(HEADING_TEXT)).toBeNull()
  })

  it.each([['Markdown'], [null], ['JavaScript']])(
    'renders nothing and requests nothing for the language %s',
    async (language) => {
      const { container, requests } = renderPanel(undefined, { language })
      expect(container.innerHTML).toBe('')
      expect(screen.queryByRole('button')).toBeNull()
      expect(screen.queryByRole('region', { name: 'Unbound names' })).toBeNull()
      await sleep(30)
      expect(requests).toHaveLength(0)
    },
  )

  it('shows no unbound names panel for a non-Python file in the viewer', async () => {
    const { requests } = renderViewer('docs/notes.md', { [GET_CONTENT]: contentRoute })
    await waitFor(() => expect(codeText()).toBe('# Notes\n'))
    expect(screen.queryByRole('region', { name: 'Unbound names' })).toBeNull()
    await sleep(30)
    expect(byUrl(requests, UNBOUND_PATH)).toHaveLength(0)
  })
})

// --- B. Requests --------------------------------------------------------------------------------

describe('Unbound names requests', () => {
  it('sends exactly one GET with params exactly { path } on an explicit click', async () => {
    const { seen, requests } = renderPanel()
    fireEvent.click(actionButton())
    await loaded()
    expect(seen).toEqual([{ path: 'app/main.py' }])
    expect(Object.keys(seen[0])).toEqual(['path'])
    for (const forbidden of ['line', 'column', 'name', 'cursor', 'end_line', 'kind']) {
      expect(seen[0]).not.toHaveProperty(forbidden)
    }
    expect(byUrl(requests, UNBOUND_PATH).map((r) => [r.method, r.data])).toEqual([['GET', undefined]])
    await sleep(50)
    expect(byUrl(requests, UNBOUND_PATH)).toHaveLength(1)
  })

  it('sends an unusual server path unchanged', async () => {
    const { seen } = await panelResult(unboundResult(), { path: 'a b/ü&x=1#.py' })
    expect(seen).toEqual([{ path: 'a b/ü&x=1#.py' }])
  })

  it('shows the loading label, disables the button while pending and sends no second request', async () => {
    const hold = holdUnbound()
    renderPanel()
    fireEvent.click(actionButton())
    await screen.findByRole('button', { name: 'Finding unbound names…' })
    expect(actionButton().disabled).toBe(true)
    fireEvent.click(actionButton())
    fireEvent.click(actionButton())
    await sleep(20)
    expect(hold.held).toHaveLength(1)
    expect(hold.held[0].params).toEqual({ path: 'app/main.py' })
    hold.held[0].release(unboundResult())
    await screen.findByRole('button', { name: 'Refresh unbound names' })
    expect(actionButton().disabled).toBe(false)
  })

  it('offers Refresh after a result and requests again only when clicked', async () => {
    const { seen } = await panelResult(unboundResult())
    expect(actionButton().textContent).toBe('Refresh unbound names')
    await sleep(30)
    expect(seen).toHaveLength(1)
    fireEvent.click(actionButton())
    await waitFor(() => expect(seen).toHaveLength(2))
    expect(seen).toEqual([{ path: 'app/main.py' }, { path: 'app/main.py' }])
  })
})

// --- C. Results ---------------------------------------------------------------------------------

describe('Unbound names results', () => {
  it('renders every occurrence with its exact text in server order, duplicates included', async () => {
    await panelResult(unboundResult())
    expect(screen.getByRole('heading', { name: HEADING_TEXT })).toBeTruthy()
    expect(rowTexts()).toEqual(ROW_TEXTS)
    expect(regionText()).not.toContain('unavailable')
    expect(regionText()).not.toContain(EMPTY_TEXT)
  })

  it('shows the server summary even when it differs from the rows', async () => {
    await panelResult(unboundResult())
    expect(screen.getByTestId('unbound-summary').textContent).toBe(SUMMARY_TEXT)
    expect(rowTexts()).toHaveLength(4) // 4 rows, 3 distinct names: the summary is never recounted
  })

  it.each([
    [3, true],
    [0, false],
  ])('shows the star-import note only when unsupported is above zero (%i)', async (unsupported, shown) => {
    await panelResult(unboundResult({ summary: { ...SUMMARY, unsupported } }))
    expect(regionText().includes(STAR_NOTE)).toBe(shown)
    expect(regionText().includes('star import')).toBe(shown)
  })

  it.each([
    ['true with four rows', true, NAMES],
    ['false with 500 rows', false, Array.from({ length: 500 }, (_, i) => ({ line: i + 1, column: 1, name: `n${i}` }))],
  ])('follows the server truncation flag only: %s', async (_name, truncated, names) => {
    await panelResult(unboundResult({ truncated, names }))
    expect(regionText().includes(TRUNCATED_TEXT)).toBe(truncated)
  })

  it('shows the empty message for a parsed result without occurrences', async () => {
    await panelResult(unboundResult({ names: [], summary: ZERO }))
    expect(regionText()).toContain(EMPTY_TEXT)
    expect(screen.getByTestId('unbound-summary').textContent).toBe('Unbound: 0 occurrences of 0 names.')
    expect(screen.queryByRole('list', { name: 'Unbound name occurrences' })).toBeNull()
  })

  it('never renders the implicit count', async () => {
    await panelResult(unboundResult({ summary: { ...SUMMARY, implicit: 9876 } }))
    expect(regionText()).not.toContain('9876')
    expect(regionText().toLowerCase()).not.toContain('implicit')
  })

  it('never describes the result as errors', async () => {
    await panelResult(unboundResult())
    expect(regionText()).not.toMatch(/error|NameError|runtime|undefined/i)
  })
})

// --- D. Navigation ------------------------------------------------------------------------------

describe('Unbound names navigation', () => {
  it("navigates every occurrence to the server's path at its line, never the column", async () => {
    const onSelectLine = vi.fn<Navigate>()
    // The requested path and the server's path deliberately differ: the server's is used.
    await panelResult(unboundResult({ path: 'server/actual.py' }), { path: 'requested/asked.py', onSelectLine })
    const buttons = rowButtons()
    expect(buttons.map((b) => b.getAttribute('aria-label'))).toEqual(ROW_LABELS)
    expect(buttons.map((b) => b.textContent)).toEqual(ROW_TEXTS) // the column is shown
    buttons.forEach((button) => fireEvent.click(button))
    expect(onSelectLine.mock.calls).toEqual([
      ['server/actual.py', 2],
      ['server/actual.py', 4],
      ['server/actual.py', 8], // the duplicate name navigates to its own line
      ['server/actual.py', 9],
    ])
    expect(onSelectLine.mock.calls.every((call) => call.length === 2)).toBe(true)
  })

  it('renders occurrences as plain text without onSelectLine', async () => {
    await panelResult(unboundResult())
    expect(rowTexts()).toEqual(ROW_TEXTS)
    expect(rowButtons()).toHaveLength(0)
  })

  it('navigates through the viewer to the open file at the occurrence line', async () => {
    const { onSelectLine } = renderViewer('app/main.py', {
      [GET_CONTENT]: contentRoute,
      [GET_UNBOUND]: { status: 200, data: unboundResult() },
    })
    await waitFor(() => expect(codeText()).toBe(MAIN))
    fireEvent.click(actionButton())
    await loaded()
    fireEvent.click(within(region()).getByRole('button', { name: 'Go to zeta at line 4, column 7' }))
    expect(onSelectLine).toHaveBeenCalledWith('app/main.py', 4)
  })
})

// --- E. Statuses --------------------------------------------------------------------------------

describe('Unbound names statuses', () => {
  it.each(Object.entries(MESSAGES))('status %s shows only its message', async (status, message) => {
    renderPanel({ status: 200, data: notParsed(status) })
    fireEvent.click(actionButton())
    await screen.findByText(message)
    expect(screen.queryByTestId('unbound-summary')).toBeNull()
    expect(screen.queryByText(HEADING_TEXT)).toBeNull()
    expect(screen.queryByText(EMPTY_TEXT)).toBeNull()
    expect(rowTexts()).toHaveLength(0)
  })

  it('shows a safe fallback for an unknown status', async () => {
    renderPanel({ status: 200, data: notParsed('something_new') })
    fireEvent.click(actionButton())
    await screen.findByText(UNKNOWN_TEXT)
    expect(screen.queryByText(EMPTY_TEXT)).toBeNull()
  })

  it('has the approved wording', () => {
    expect(STATUS_MESSAGES).toEqual(MESSAGES)
    expect([HEADING, EMPTY_RESULT, TRUNCATED_NOTICE, UNKNOWN_STATUS]).toEqual([
      HEADING_TEXT,
      EMPTY_TEXT,
      TRUNCATED_TEXT,
      UNKNOWN_TEXT,
    ])
    expect(unboundSummary(SUMMARY)).toBe(SUMMARY_TEXT)
    expect(starImportNote(3)).toBe(STAR_NOTE)
  })
})

// --- F. Errors ----------------------------------------------------------------------------------

describe('Unbound names errors', () => {
  it.each([
    [404, 'Repository not found'],
    [409, 'Repository changed during analysis; run it again'],
    [429, 'Too many attempts; please try again later'],
  ])('shows the server detail for %i exactly, without retrying', async (status, detail) => {
    const { requests } = renderPanel({ status, data: { detail } })
    fireEvent.click(actionButton())
    const alerts = await screen.findAllByRole('alert')
    expect(alerts.map((a) => a.textContent)).toEqual([detail])
    await sleep(50)
    expect(byUrl(requests, UNBOUND_PATH)).toHaveLength(1)
    expect(rowTexts()).toHaveLength(0)
  })

  it('shows the fallback for a 500 without a usable detail, without retrying', async () => {
    const { requests } = renderPanel({ status: 500, data: {} })
    fireEvent.click(actionButton())
    expect((await screen.findByRole('alert')).textContent).toBe(FALLBACK)
    await sleep(50)
    expect(byUrl(requests, UNBOUND_PATH)).toHaveLength(1)
  })

  it('uses the existing network message without retrying', async () => {
    const { requests } = renderPanel(() => {
      throw new AxiosError('Network Error', AxiosError.ERR_NETWORK)
    })
    fireEvent.click(actionButton())
    expect((await screen.findByRole('alert')).textContent).toBe('Could not reach the server')
    await sleep(50)
    expect(byUrl(requests, UNBOUND_PATH)).toHaveLength(1)
  })
})

// --- G. Stale results ---------------------------------------------------------------------------

describe('Unbound names never show a stale result', () => {
  it('never shows a pending request’s result in another file', async () => {
    const hold = holdUnbound()
    const { rerenderPath } = renderViewer('app/main.py', { [GET_CONTENT]: contentRoute })
    await waitFor(() => expect(codeText()).toBe(MAIN))
    fireEvent.click(actionButton())
    await screen.findByRole('button', { name: 'Finding unbound names…' })
    rerenderPath('app/other.py')
    await waitFor(() => expect(codeText()).toBe(OTHER))
    hold.held[0].release(unboundResult())
    await sleep(50)
    expect([actionButton().textContent, actionButton().disabled]).toEqual(['Find unbound names', false])
    expect(rowTexts()).toHaveLength(0)
    expect(screen.queryByText(HEADING_TEXT)).toBeNull()
  })

  it('starts fresh after leaving a loaded result, with no request when returning', async () => {
    const { requests, rerenderPath } = renderViewer('app/main.py', {
      [GET_CONTENT]: contentRoute,
      [GET_UNBOUND]: { status: 200, data: unboundResult() },
    })
    await waitFor(() => expect(codeText()).toBe(MAIN))
    fireEvent.click(actionButton())
    await loaded()
    expect(rowTexts()).toEqual(ROW_TEXTS)
    rerenderPath('app/other.py')
    await waitFor(() => expect(codeText()).toBe(OTHER))
    expect([actionButton().textContent, rowTexts()]).toEqual(['Find unbound names', []])
    rerenderPath('app/main.py')
    await waitFor(() => expect(codeText()).toBe(MAIN))
    await sleep(50)
    expect([actionButton().textContent, rowTexts()]).toEqual(['Find unbound names', []])
    expect(byUrl(requests, UNBOUND_PATH)).toHaveLength(1)
  })

  it('never carries an error into another file', async () => {
    const { rerenderPath } = renderViewer('app/main.py', {
      [GET_CONTENT]: contentRoute,
      [GET_UNBOUND]: { status: 409, data: { detail: 'Repository changed during analysis; run it again' } },
    })
    await waitFor(() => expect(codeText()).toBe(MAIN))
    fireEvent.click(actionButton())
    await screen.findByRole('alert')
    rerenderPath('app/other.py')
    await waitFor(() => expect(codeText()).toBe(OTHER))
    expect(within(region()).queryByRole('alert')).toBeNull()
    expect(actionButton().textContent).toBe('Find unbound names')
  })

  /** Loads app/other.py and then app/main.py, so switching between them shows no loading state. */
  async function openBothFiles(routes: Record<string, Handler> = {}) {
    const view = renderViewer('app/other.py', { [GET_CONTENT]: contentRoute, ...routes })
    await waitFor(() => expect(codeText()).toBe(OTHER))
    view.rerenderPath('app/main.py')
    await waitFor(() => expect(codeText()).toBe(MAIN))
    return view
  }

  it('never carries a result into another already-loaded file', async () => {
    const { rerenderPath } = await openBothFiles({ [GET_UNBOUND]: { status: 200, data: unboundResult() } })
    fireEvent.click(actionButton())
    await loaded()
    expect(rowTexts()).toEqual(ROW_TEXTS)
    rerenderPath('app/other.py')
    expect(codeText()).toBe(OTHER) // shown at once: the panel stays mounted unless keyed by path
    expect([actionButton().textContent, actionButton().disabled, rowTexts()]).toEqual([
      'Find unbound names',
      false,
      [],
    ])
    expect(screen.queryByText(HEADING_TEXT)).toBeNull()
    expect(screen.queryByTestId('unbound-summary')).toBeNull()
  })

  it('never shows a pending request’s result in another already-loaded file', async () => {
    const hold = holdUnbound()
    const { rerenderPath } = await openBothFiles()
    fireEvent.click(actionButton())
    await screen.findByRole('button', { name: 'Finding unbound names…' })
    rerenderPath('app/other.py')
    expect(codeText()).toBe(OTHER)
    expect([actionButton().textContent, actionButton().disabled]).toEqual(['Find unbound names', false])
    hold.held[0].release(unboundResult())
    await sleep(50)
    expect([actionButton().textContent, actionButton().disabled, rowTexts()]).toEqual([
      'Find unbound names',
      false,
      [],
    ])
    expect(screen.queryByText(HEADING_TEXT)).toBeNull()
  })

  it('never carries an error into another already-loaded file', async () => {
    const { rerenderPath } = await openBothFiles({
      [GET_UNBOUND]: { status: 409, data: { detail: 'Repository changed during analysis; run it again' } },
    })
    fireEvent.click(actionButton())
    await screen.findByRole('alert')
    rerenderPath('app/other.py')
    expect(codeText()).toBe(OTHER)
    expect(within(region()).queryByRole('alert')).toBeNull()
    expect(actionButton().textContent).toBe('Find unbound names')
  })
})

// --- H. Security --------------------------------------------------------------------------------

describe('Unbound names security', () => {
  const HOSTILE: UnboundName[] = [
    { line: 1, column: 1, name: '<dangerous>' },
    { line: 2, column: 5, name: '<img src=x onerror=alert(1)>' },
    { line: 3, column: 9, name: '<b>bold</b>' },
  ]

  it.each([
    ['as buttons', true],
    ['as plain text', false],
  ])('renders markup-like names as literal text %s', async (_name, navigable) => {
    await panelResult(unboundResult({ names: HOSTILE }), navigable ? { onSelectLine: vi.fn<Navigate>() } : {})
    expect(rowTexts()).toEqual([
      '<dangerous> line 1, column 1',
      '<img src=x onerror=alert(1)> line 2, column 5',
      '<b>bold</b> line 3, column 9',
    ])
    expect(document.querySelector('dangerous, img, script, iframe, b')).toBeNull()
    expect(document.querySelector('[src], [onerror]')).toBeNull()
    expect(region().innerHTML).toContain('&lt;b&gt;bold&lt;/b&gt;') // escaped, never parsed as HTML
  })

  it('shows only the named fields of the response', async () => {
    const extra = { snippet: 'FAKE-SNIPPET-MARKER', sha256: 'FAKE-SHA-MARKER', id: 'FAKE-ID-MARKER' }
    await panelResult(
      {
        ...unboundResult({ names: NAMES.map((n) => ({ ...n, ...extra })) as UnboundName[] }),
        summary: { ...SUMMARY, ...extra },
        ...extra,
      },
      { onSelectLine: vi.fn<Navigate>() },
    )
    const labels = Array.from(region().querySelectorAll('[aria-label]')).map((e) => e.getAttribute('aria-label'))
    for (const marker of Object.values(extra)) {
      expect(regionText()).not.toContain(marker)
      expect(labels.join(' ')).not.toContain(marker)
    }
    expect(regionText()).not.toContain('{')
  })

  it('uses no browser storage, cookies or direct fetch, only the app API', async () => {
    const getItem = vi.spyOn(Storage.prototype, 'getItem')
    const writes = [
      vi.spyOn(Storage.prototype, 'setItem'),
      vi.spyOn(Storage.prototype, 'removeItem'),
      vi.spyOn(Storage.prototype, 'clear'),
    ]
    const cookieGet = vi.fn(() => '')
    const cookieSet = vi.fn()
    Object.defineProperty(document, 'cookie', { configurable: true, get: cookieGet, set: cookieSet })
    const stored = [localStorage.length, sessionStorage.length]
    const realFetch = globalThis.fetch
    const fetchSpy = vi.fn()
    globalThis.fetch = fetchSpy as unknown as typeof fetch
    try {
      const { requests } = await panelResult(unboundResult(), { onSelectLine: vi.fn<Navigate>() })
      fireEvent.click(rowButtons()[0])
      expect(new Set(getItem.mock.calls.map((call) => call[0]))).toEqual(new Set(['ai_se_access_token']))
      for (const spy of writes) {
        expect(spy).not.toHaveBeenCalled()
      }
      expect([localStorage.length, sessionStorage.length]).toEqual(stored)
      expect(cookieGet).not.toHaveBeenCalled()
      expect(cookieSet).not.toHaveBeenCalled()
      expect(fetchSpy).not.toHaveBeenCalled()
      expect(requests.map((r) => `${r.method} ${r.url}`)).toEqual([GET_UNBOUND])
      expect(requests.every((r) => r.baseURL === '/api/v1')).toBe(true)
    } finally {
      globalThis.fetch = realFetch
      delete (document as { cookie?: unknown }).cookie
    }
  })

  it('renders duplicate names without React key warnings', async () => {
    const consoleError = vi.spyOn(console, 'error')
    const duplicates: UnboundName[] = [
      { line: 2, column: 1, name: 'x' },
      { line: 2, column: 5, name: 'x' },
      { line: 7, column: 1, name: 'x' },
    ]
    await panelResult(unboundResult({ names: duplicates }))
    fireEvent.click(actionButton()) // a refresh re-renders the same occurrences
    await waitFor(() => expect(actionButton().disabled).toBe(false))
    expect(rowTexts()).toEqual(['x line 2, column 1', 'x line 2, column 5', 'x line 7, column 1'])
    const messages = consoleError.mock.calls.map((call) => call.map(String).join(' '))
    expect(messages.filter((m) => /same key|unique "key"/.test(m))).toEqual([])
  })
})

// --- I. FileViewer integration ------------------------------------------------------------------

describe('FileViewer integration', () => {
  it('places the panel after the importers and before go-to-definition', async () => {
    renderViewer('app/main.py', { [GET_CONTENT]: contentRoute })
    await waitFor(() => expect(codeText()).toBe(MAIN))
    const panels = screen.getAllByRole('region').map((r) => r.getAttribute('aria-label'))
    expect(panels).toEqual([
      'File explanation',
      'File outline',
      'File imports',
      'File importers',
      'Unused imports',
      'Unbound names',
      'Unused variables',
      'Go to definition',
      'Name occurrences',
      'Redefined names',
      'Unreachable code',
    ])
  })

  it("never puts the open file's path in its labels", async () => {
    renderViewer('app/main.py', {
      [GET_CONTENT]: contentRoute,
      [GET_UNBOUND]: { status: 200, data: unboundResult() },
    })
    await waitFor(() => expect(codeText()).toBe(MAIN))
    fireEvent.click(actionButton())
    await loaded()
    const names = within(region())
      .getAllByRole('button')
      .map((b) => `${b.getAttribute('aria-label') ?? ''} ${b.textContent ?? ''}`)
    expect(names).toHaveLength(5) // the action and four occurrences
    expect(names.some((name) => name.includes('main.py'))).toBe(false)
  })
})
