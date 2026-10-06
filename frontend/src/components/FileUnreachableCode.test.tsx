import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { AxiosError, type AxiosRequestConfig, type InternalAxiosRequestConfig } from 'axios'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { apiClient } from '../services/client'
import { mockApi, type MockResponse, PROJECT, type RecordedRequest, ZIP_REPOSITORY } from '../test/mockApi'
import { createTestQueryClient } from '../test/render'
import type { FileContent, UnreachableCodeResult, UnreachableCodeSummary, UnreachableRegion } from '../types/api'
import {
  CAUSES,
  EMPTY_RESULT,
  FileUnreachableCode,
  HEADING,
  lineRange,
  STATUS_MESSAGES,
  TRUNCATED_NOTICE,
  UNKNOWN_CAUSE,
  UNKNOWN_STATUS,
  unreachableSummary,
} from './FileUnreachableCode'
import { FileViewer, type LineSelection } from './FileViewer'

const P = PROJECT.id
const R = ZIP_REPOSITORY.id
const BASE = `/projects/${P}/repositories/${R}`
const UNREACHABLE_PATH = `${BASE}/files/unreachable-code`
const GET_UNREACHABLE = `GET ${UNREACHABLE_PATH}`
const GET_CONTENT = `GET ${BASE}/files/content`

const MAIN = 'def f():\n    return 1\n    dead()\n'
const OTHER = 'def other(): ...\n'

// The approved wording, written here independently of the component.
const HEADING_TEXT = 'Statements that can never run'
const EMPTY_TEXT = 'No unreachable code found.'
const TRUNCATED_TEXT = 'Unreachable code results are truncated: only the first 500 regions are shown.'
const UNKNOWN_TEXT = 'Unreachable code is unavailable.'
const FALLBACK = 'Could not check unreachable code'
const CHANGED = 'Repository changed during analysis; run it again'
const MESSAGES = {
  syntax_error: 'Unreachable code is unavailable: this file could not be parsed as Python.',
  too_large: 'Unreachable code is unavailable: this file is too large to analyze (limit 256 KiB).',
  binary: 'Unreachable code is unavailable: this file is not text.',
  unsupported_language: 'Unreachable code is only available for Python files.',
}
const CAUSE_TEXTS = {
  return: 'after a return',
  raise: 'after a raise',
  break: 'after a break',
  continue: 'after a continue',
  if: 'after an if statement whose every branch leaves',
  while: 'after a loop that never ends',
}

// Server order, deliberately not sorted by line; single- and multi-line regions.
const REGIONS: UnreachableRegion[] = [
  { line: 12, end_line: 14, statements: 2, cause: 'return', cause_line: 11 },
  { line: 4, end_line: 4, statements: 1, cause: 'raise', cause_line: 3 },
  { line: 30, end_line: 31, statements: 2, cause: 'while', cause_line: 25 },
  { line: 20, end_line: 20, statements: 1, cause: 'if', cause_line: 16 },
]
const ROW_TEXTS = [
  'lines 12–14 (2 statements) after a return on line 11',
  'line 4 (1 statement) after a raise on line 3',
  'lines 30–31 (2 statements) after a loop that never ends on line 25',
  'line 20 (1 statement) after an if statement whose every branch leaves on line 16',
]
const ROW_LABELS = [
  'Go to the unreachable code at lines 12–14',
  'Go to the unreachable code at line 4',
  'Go to the unreachable code at lines 30–31',
  'Go to the unreachable code at line 20',
]
const CAUSE_LABELS = [
  'Go to the statement that makes lines 12–14 unreachable, on line 11',
  'Go to the statement that makes line 4 unreachable, on line 3',
  'Go to the statement that makes lines 30–31 unreachable, on line 25',
  'Go to the statement that makes line 20 unreachable, on line 16',
]
// Both counts differ from the rows (4 regions, 6 statements), so a recount shows.
const SUMMARY: UnreachableCodeSummary = { regions: 7, statements: 12 }
const SUMMARY_TEXT = 'Never run: 12 statements in 7 regions.'
const ZERO: UnreachableCodeSummary = { regions: 0, statements: 0 }

function unreachableResult(extra: Partial<UnreachableCodeResult> = {}): UnreachableCodeResult {
  return { path: 'app/main.py', status: 'parsed', regions: REGIONS, summary: SUMMARY, truncated: false, ...extra }
}

function notParsed(status: string): UnreachableCodeResult {
  return {
    path: 'app/main.py',
    status: status as UnreachableCodeResult['status'],
    regions: [],
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
const region = () => screen.getByRole('region', { name: 'Unreachable code' })
// The row and cause buttons mention unreachable code too, but always start with "Go to".
const actionButton = () =>
  within(region()).getByRole('button', { name: /^(Find|Finding|Refresh) unreachable code/ }) as HTMLButtonElement
const rowTexts = () => screen.queryAllByTestId('unreachable-region').map((row) => row.textContent)
const rowButtons = () =>
  within(region()).queryAllByRole('button', { name: /^Go to the unreachable code at / }) as HTMLButtonElement[]
const causeButtons = () =>
  within(region()).queryAllByRole('button', { name: /^Go to the statement that makes / }) as HTMLButtonElement[]
const regionText = () => region().textContent ?? ''
const byUrl = (requests: RecordedRequest[], url: string) => requests.filter((r) => r.url === url)
const loaded = () =>
  waitFor(() =>
    expect(
      within(region()).queryByRole('button', { name: 'Refresh unreachable code' }) ??
        within(region()).queryByRole('alert'),
    ).not.toBeNull(),
  )

function renderPanel(
  handler: Handler = { status: 200, data: unreachableResult() },
  props: { path?: string; language?: string | null; onSelectLine?: Navigate } = {},
) {
  const seen: Params[] = []
  const requests = mockApi({
    [GET_UNREACHABLE]: (config) => {
      seen.push({ ...(config.params as Params) })
      return typeof handler === 'function' ? handler(config) : handler
    },
  })
  const client = createTestQueryClient()
  const ui = () => (
    <QueryClientProvider client={client}>
      <FileUnreachableCode
        projectId={P}
        repositoryId={R}
        path={props.path ?? 'app/main.py'}
        language={props.language === undefined ? 'Python' : props.language}
        onSelectLine={props.onSelectLine}
      />
    </QueryClientProvider>
  )
  const view = render(ui())
  return { seen, requests, client, rerenderPanel: () => view.rerender(ui()), ...view }
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

/** Loads app/other.py and then app/main.py, so switching between them shows no loading state. */
async function openBothFiles(routes: Record<string, Handler> = {}) {
  const view = renderViewer('app/other.py', { [GET_CONTENT]: contentRoute, ...routes })
  await waitFor(() => expect(codeText()).toBe(OTHER))
  view.rerenderPath('app/main.py')
  await waitFor(() => expect(codeText()).toBe(MAIN))
  return view
}

/** Holds every unreachable-code request until released; other requests go through. */
function holdUnreachable() {
  const held: { params: Params; release: (data: UnreachableCodeResult) => void }[] = []
  const realGet = apiClient.get.bind(apiClient)
  const spy = vi.spyOn(apiClient, 'get').mockImplementation(((url: string, config?: AxiosRequestConfig) => {
    if (url === UNREACHABLE_PATH) {
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

describe('Unreachable code visibility', () => {
  it('shows the panel for a Python file without requesting anything on open or line selection', async () => {
    const { requests, rerenderPath } = renderViewer('app/main.py', { [GET_CONTENT]: contentRoute })
    await waitFor(() => expect(codeText()).toBe(MAIN))
    expect([actionButton().textContent, actionButton().disabled]).toEqual(['Find unreachable code', false])
    rerenderPath('app/main.py', { line: 2 }) // a line selected in the open file
    fireEvent.mouseUp(document.querySelector('pre') as HTMLPreElement)
    await sleep(50)
    expect(byUrl(requests, UNREACHABLE_PATH)).toHaveLength(0)
    expect(screen.queryByText(HEADING_TEXT)).toBeNull()
  })

  it.each([['Markdown'], [null], ['JavaScript']])(
    'renders nothing and requests nothing for the language %s',
    async (language) => {
      const { container, requests } = renderPanel(undefined, { language })
      expect(container.innerHTML).toBe('')
      expect(screen.queryByRole('button')).toBeNull()
      expect(screen.queryByRole('region', { name: 'Unreachable code' })).toBeNull()
      await sleep(30)
      expect(requests).toHaveLength(0)
    },
  )

  it('shows no unreachable code panel for a non-Python file in the viewer', async () => {
    const { requests } = renderViewer('docs/notes.md', { [GET_CONTENT]: contentRoute })
    await waitFor(() => expect(codeText()).toBe('# Notes\n'))
    expect(screen.queryByRole('region', { name: 'Unreachable code' })).toBeNull()
    await sleep(30)
    expect(byUrl(requests, UNREACHABLE_PATH)).toHaveLength(0)
  })
})

// --- B. Requests --------------------------------------------------------------------------------

describe('Unreachable code requests', () => {
  it('sends exactly one GET to the endpoint with params exactly { path } on an explicit click', async () => {
    const { seen, requests } = renderPanel()
    fireEvent.click(actionButton())
    await loaded()
    expect(seen).toEqual([{ path: 'app/main.py' }])
    expect(Object.keys(seen[0])).toEqual(['path'])
    for (const forbidden of ['line', 'column', 'name', 'scope', 'limit', 'kind']) {
      expect(seen[0]).not.toHaveProperty(forbidden)
    }
    expect(requests.map((r) => [r.method, r.url, r.baseURL, r.data])).toEqual([
      ['GET', UNREACHABLE_PATH, '/api/v1', undefined],
    ])
    await sleep(50)
    expect(byUrl(requests, UNREACHABLE_PATH)).toHaveLength(1)
  })

  it('sends an unusual server path unchanged, as a query parameter only', async () => {
    const { seen, requests } = await panelResult(unreachableResult(), { path: 'a b/ü&x=1#.py' })
    expect(seen).toEqual([{ path: 'a b/ü&x=1#.py' }])
    expect(requests.map((r) => r.url)).toEqual([UNREACHABLE_PATH]) // never concatenated into the URL
  })

  it('requests nothing on mount or on re-render', async () => {
    const { requests, rerenderPanel } = renderPanel()
    rerenderPanel()
    rerenderPanel()
    await sleep(50)
    expect(requests).toHaveLength(0)
    expect(actionButton().textContent).toBe('Find unreachable code')
  })

  it('shows the loading label, disables the button while pending and sends no second request', async () => {
    const hold = holdUnreachable()
    renderPanel()
    fireEvent.click(actionButton())
    await screen.findByRole('button', { name: 'Finding unreachable code…' })
    expect(actionButton().disabled).toBe(true)
    fireEvent.click(actionButton())
    fireEvent.click(actionButton())
    await sleep(20)
    expect(hold.held).toHaveLength(1)
    expect(hold.held[0].params).toEqual({ path: 'app/main.py' })
    hold.held[0].release(unreachableResult())
    await screen.findByRole('button', { name: 'Refresh unreachable code' })
    expect(actionButton().disabled).toBe(false)
  })

  it('offers Refresh after a result and requests again, freshly, on every click', async () => {
    const { seen } = await panelResult(unreachableResult())
    expect(actionButton().textContent).toBe('Refresh unreachable code')
    await sleep(30)
    expect(seen).toHaveLength(1)
    fireEvent.click(actionButton())
    await waitFor(() => expect(seen).toHaveLength(2))
    fireEvent.click(actionButton())
    await waitFor(() => expect(seen).toHaveLength(3))
    expect(seen).toEqual([{ path: 'app/main.py' }, { path: 'app/main.py' }, { path: 'app/main.py' }])
  })

  it('caches nothing: a remounted panel starts fresh and requests again', async () => {
    const { seen, client, unmount } = await panelResult(unreachableResult())
    unmount()
    render(
      <QueryClientProvider client={client}>
        <FileUnreachableCode projectId={P} repositoryId={R} path="app/main.py" language="Python" />
      </QueryClientProvider>,
    )
    expect([actionButton().textContent, rowTexts()]).toEqual(['Find unreachable code', []])
    expect(screen.queryByText(HEADING_TEXT)).toBeNull()
    await sleep(30)
    expect(seen).toHaveLength(1)
    fireEvent.click(actionButton())
    await loaded()
    expect(seen).toHaveLength(2)
  })
})

// --- C. Results ---------------------------------------------------------------------------------

describe('Unreachable code results', () => {
  it('renders every region with its exact text in server order', async () => {
    await panelResult(unreachableResult())
    expect(screen.getByRole('heading', { name: HEADING_TEXT })).toBeTruthy()
    expect(rowTexts()).toEqual(ROW_TEXTS)
    expect(screen.getByRole('list', { name: 'Unreachable regions' })).toBeTruthy()
    expect(regionText()).not.toContain('unavailable')
    expect(regionText()).not.toContain(EMPTY_TEXT)
  })

  it('shows the server summary even when it differs from the rows', async () => {
    await panelResult(unreachableResult())
    expect(screen.getByTestId('unreachable-summary').textContent).toBe(SUMMARY_TEXT)
    expect(rowTexts()).toHaveLength(4) // 4 rows of 6 statements: the summary is never recounted
  })

  it.each([
    ['one of each', { regions: 1, statements: 1 }, 'Never run: 1 statement in 1 region.'],
    ['plural both', { regions: 2, statements: 3 }, 'Never run: 3 statements in 2 regions.'],
    ['plural then singular', { regions: 1, statements: 2 }, 'Never run: 2 statements in 1 region.'],
  ])('words the server summary with singular and plural nouns (%s)', async (_name, summary, text) => {
    await panelResult(unreachableResult({ summary }))
    expect(screen.getByTestId('unreachable-summary').textContent).toBe(text)
  })

  it.each(Object.entries(CAUSE_TEXTS))('names the cause %s', async (cause, text) => {
    const one = { line: 8, end_line: 8, statements: 1, cause, cause_line: 7 } as UnreachableRegion
    await panelResult(unreachableResult({ regions: [one] }))
    expect(rowTexts()).toEqual([`line 8 (1 statement) ${text} on line 7`])
  })

  it('names an unknown cause safely, never echoing it', async () => {
    const odd = { line: 8, end_line: 9, statements: 3, cause: 'goto', cause_line: 7 }
    await panelResult(unreachableResult({ regions: [odd as unknown as UnreachableRegion] }))
    expect(rowTexts()).toEqual(['lines 8–9 (3 statements) after a statement that always leaves on line 7'])
    expect(regionText()).not.toContain('goto')
  })

  it.each([
    ['true with four rows', true, REGIONS],
    [
      'false with 500 rows',
      false,
      Array.from({ length: 500 }, (_, i) => ({
        line: 2 * i + 2,
        end_line: 2 * i + 2,
        statements: 1,
        cause: 'return' as const,
        cause_line: 2 * i + 1,
      })),
    ],
  ])('follows the server truncation flag only: %s', async (_name, truncated, regions) => {
    await panelResult(unreachableResult({ truncated, regions }))
    expect(regionText().includes(TRUNCATED_TEXT)).toBe(truncated)
    expect(rowTexts()).toHaveLength(regions.length) // the server's rows, never trimmed here
  })

  it('shows the empty message for a parsed result without regions', async () => {
    await panelResult(unreachableResult({ regions: [], summary: ZERO }))
    expect(regionText()).toContain(EMPTY_TEXT)
    expect(screen.getByTestId('unreachable-summary').textContent).toBe('Never run: 0 statements in 0 regions.')
    expect(screen.queryByRole('list', { name: 'Unreachable regions' })).toBeNull()
  })
})

// --- D. Navigation ------------------------------------------------------------------------------

describe('Unreachable code navigation', () => {
  it("navigates every region to the server's path at its first line", async () => {
    const onSelectLine = vi.fn<Navigate>()
    // The requested path and the server's path deliberately differ: the server's is used.
    await panelResult(unreachableResult({ path: 'server/actual.py' }), { path: 'requested/asked.py', onSelectLine })
    expect(rowTexts()).toEqual(ROW_TEXTS) // the same text as without navigation
    const buttons = rowButtons()
    expect(buttons.map((b) => b.getAttribute('aria-label'))).toEqual(ROW_LABELS)
    expect(buttons.map((b) => b.textContent)).toEqual([
      'lines 12–14 (2 statements)',
      'line 4 (1 statement)',
      'lines 30–31 (2 statements)',
      'line 20 (1 statement)',
    ])
    buttons.forEach((button) => fireEvent.click(button))
    expect(onSelectLine.mock.calls).toEqual([
      ['server/actual.py', 12],
      ['server/actual.py', 4],
      ['server/actual.py', 30],
      ['server/actual.py', 20],
    ])
    expect(onSelectLine.mock.calls.every((call) => call.length === 2)).toBe(true)
  })

  it("navigates every cause to the server's path at the cause line", async () => {
    const onSelectLine = vi.fn<Navigate>()
    await panelResult(unreachableResult({ path: 'server/actual.py' }), { path: 'requested/asked.py', onSelectLine })
    const buttons = causeButtons()
    expect(buttons.map((b) => b.getAttribute('aria-label'))).toEqual(CAUSE_LABELS)
    expect(buttons.map((b) => b.textContent)).toEqual([
      'after a return on line 11',
      'after a raise on line 3',
      'after a loop that never ends on line 25',
      'after an if statement whose every branch leaves on line 16',
    ])
    buttons.forEach((button) => fireEvent.click(button))
    expect(onSelectLine.mock.calls).toEqual([
      ['server/actual.py', 11],
      ['server/actual.py', 3],
      ['server/actual.py', 25],
      ['server/actual.py', 16],
    ])
  })

  it('renders regions as plain text without onSelectLine', async () => {
    await panelResult(unreachableResult())
    expect(rowTexts()).toEqual(ROW_TEXTS)
    expect(within(region()).queryAllByRole('button', { name: /^Go to / })).toHaveLength(0)
  })

  it('goes from Find to a region and to its cause in the viewer', async () => {
    const { onSelectLine } = renderViewer('app/main.py', {
      [GET_CONTENT]: contentRoute,
      [GET_UNREACHABLE]: { status: 200, data: unreachableResult() },
    })
    await waitFor(() => expect(codeText()).toBe(MAIN))
    fireEvent.click(actionButton())
    await loaded()
    fireEvent.click(within(region()).getByRole('button', { name: 'Go to the unreachable code at line 4' }))
    expect(onSelectLine).toHaveBeenLastCalledWith('app/main.py', 4)
    fireEvent.click(
      within(region()).getByRole('button', { name: 'Go to the statement that makes line 4 unreachable, on line 3' }),
    )
    expect(onSelectLine).toHaveBeenLastCalledWith('app/main.py', 3)
  })
})

// --- E. Statuses --------------------------------------------------------------------------------

describe('Unreachable code statuses', () => {
  it.each(Object.entries(MESSAGES))('status %s shows only its message', async (status, message) => {
    renderPanel({ status: 200, data: { ...notParsed(status), summary: SUMMARY, truncated: true } })
    fireEvent.click(actionButton())
    await screen.findByText(message)
    expect(screen.queryByTestId('unreachable-summary')).toBeNull()
    expect(screen.queryByText(HEADING_TEXT)).toBeNull()
    expect(screen.queryByText(EMPTY_TEXT)).toBeNull()
    expect(regionText()).not.toContain('truncated')
    expect(rowTexts()).toHaveLength(0)
  })

  it('shows a safe fallback for an unknown status', async () => {
    renderPanel({ status: 200, data: notParsed('something_new') })
    fireEvent.click(actionButton())
    await screen.findByText(UNKNOWN_TEXT)
    expect(screen.queryByText(EMPTY_TEXT)).toBeNull()
    expect(screen.queryByTestId('unreachable-summary')).toBeNull()
  })

  it('has the approved wording', () => {
    expect(STATUS_MESSAGES).toEqual(MESSAGES)
    expect(CAUSES).toEqual(CAUSE_TEXTS)
    expect([HEADING, EMPTY_RESULT, TRUNCATED_NOTICE, UNKNOWN_STATUS, UNKNOWN_CAUSE]).toEqual([
      HEADING_TEXT,
      EMPTY_TEXT,
      TRUNCATED_TEXT,
      UNKNOWN_TEXT,
      'after a statement that always leaves',
    ])
    expect(unreachableSummary(SUMMARY)).toBe(SUMMARY_TEXT)
    expect([lineRange(REGIONS[0]), lineRange(REGIONS[1])]).toEqual(['lines 12–14', 'line 4'])
  })
})

// --- F. Errors ----------------------------------------------------------------------------------

describe('Unreachable code errors', () => {
  it.each([
    [404, 'File not found'],
    [409, CHANGED],
    [429, 'Too many attempts; please try again later'],
  ])('shows the server detail for %i exactly, without retrying', async (status, detail) => {
    const { requests } = renderPanel({ status, data: { detail } })
    fireEvent.click(actionButton())
    const alerts = await screen.findAllByRole('alert')
    expect(alerts.map((a) => a.textContent)).toEqual([detail])
    await sleep(50)
    expect(byUrl(requests, UNREACHABLE_PATH)).toHaveLength(1)
    expect(rowTexts()).toHaveLength(0)
  })

  it('shows the fallback for a 500 without a usable detail, without retrying', async () => {
    const { requests } = renderPanel({ status: 500, data: {} })
    fireEvent.click(actionButton())
    expect((await screen.findByRole('alert')).textContent).toBe(FALLBACK)
    await sleep(50)
    expect(byUrl(requests, UNREACHABLE_PATH)).toHaveLength(1)
  })

  it('uses the existing network message without retrying', async () => {
    const { requests } = renderPanel(() => {
      throw new AxiosError('Network Error', AxiosError.ERR_NETWORK)
    })
    fireEvent.click(actionButton())
    expect((await screen.findByRole('alert')).textContent).toBe('Could not reach the server')
    await sleep(50)
    expect(byUrl(requests, UNREACHABLE_PATH)).toHaveLength(1)
  })

  it('returns to the Find label after an error', async () => {
    renderPanel({ status: 409, data: { detail: CHANGED } })
    fireEvent.click(actionButton())
    await screen.findByRole('alert')
    expect([actionButton().textContent, actionButton().disabled]).toEqual(['Find unreachable code', false])
  })

  it('clears a previous error when a later request succeeds', async () => {
    let calls = 0
    const { seen } = renderPanel(() =>
      ++calls === 1 ? { status: 409, data: { detail: CHANGED } } : { status: 200, data: unreachableResult() },
    )
    fireEvent.click(actionButton())
    expect((await screen.findByRole('alert')).textContent).toBe(CHANGED)
    fireEvent.click(actionButton())
    await screen.findByRole('button', { name: 'Refresh unreachable code' })
    expect(screen.queryByRole('alert')).toBeNull()
    expect(rowTexts()).toEqual(ROW_TEXTS)
    expect(seen).toHaveLength(2)
  })
})

// --- G. Stale results and lifecycle -------------------------------------------------------------

describe('Unreachable code never shows a stale result', () => {
  it('never shows a pending request’s result in another file', async () => {
    const hold = holdUnreachable()
    const { rerenderPath } = renderViewer('app/main.py', { [GET_CONTENT]: contentRoute })
    await waitFor(() => expect(codeText()).toBe(MAIN))
    fireEvent.click(actionButton())
    await screen.findByRole('button', { name: 'Finding unreachable code…' })
    rerenderPath('app/other.py')
    await waitFor(() => expect(codeText()).toBe(OTHER))
    hold.held[0].release(unreachableResult())
    await sleep(50)
    expect([actionButton().textContent, actionButton().disabled]).toEqual(['Find unreachable code', false])
    expect(rowTexts()).toHaveLength(0)
    expect(screen.queryByText(HEADING_TEXT)).toBeNull()
  })

  it('starts fresh after leaving a loaded result, with no request when returning', async () => {
    const { requests, rerenderPath } = renderViewer('app/main.py', {
      [GET_CONTENT]: contentRoute,
      [GET_UNREACHABLE]: { status: 200, data: unreachableResult() },
    })
    await waitFor(() => expect(codeText()).toBe(MAIN))
    fireEvent.click(actionButton())
    await loaded()
    expect(rowTexts()).toEqual(ROW_TEXTS)
    rerenderPath('app/other.py')
    await waitFor(() => expect(codeText()).toBe(OTHER))
    expect([actionButton().textContent, rowTexts()]).toEqual(['Find unreachable code', []])
    rerenderPath('app/main.py')
    await waitFor(() => expect(codeText()).toBe(MAIN))
    await sleep(50)
    expect([actionButton().textContent, rowTexts()]).toEqual(['Find unreachable code', []])
    expect(byUrl(requests, UNREACHABLE_PATH)).toHaveLength(1)
  })

  it('never carries a result into another already-loaded file', async () => {
    const { rerenderPath } = await openBothFiles({ [GET_UNREACHABLE]: { status: 200, data: unreachableResult() } })
    fireEvent.click(actionButton())
    await loaded()
    expect(rowTexts()).toEqual(ROW_TEXTS)
    rerenderPath('app/other.py')
    expect(codeText()).toBe(OTHER) // shown at once: the panel stays mounted unless keyed by path
    expect([actionButton().textContent, actionButton().disabled, rowTexts()]).toEqual([
      'Find unreachable code',
      false,
      [],
    ])
    expect(screen.queryByText(HEADING_TEXT)).toBeNull()
    expect(screen.queryByTestId('unreachable-summary')).toBeNull()
  })

  it('never shows a late response of the previous file in another already-loaded file', async () => {
    const hold = holdUnreachable()
    const { rerenderPath } = await openBothFiles()
    fireEvent.click(actionButton())
    await screen.findByRole('button', { name: 'Finding unreachable code…' })
    rerenderPath('app/other.py')
    expect(codeText()).toBe(OTHER)
    expect([actionButton().textContent, actionButton().disabled]).toEqual(['Find unreachable code', false])
    hold.held[0].release(unreachableResult())
    await sleep(50)
    expect([actionButton().textContent, actionButton().disabled, rowTexts()]).toEqual([
      'Find unreachable code',
      false,
      [],
    ])
    expect(screen.queryByText(HEADING_TEXT)).toBeNull()
  })

  it('ignores a late response after switching away and back between already-loaded files', async () => {
    const hold = holdUnreachable()
    const { rerenderPath } = await openBothFiles()
    fireEvent.click(actionButton())
    await screen.findByRole('button', { name: 'Finding unreachable code…' })
    rerenderPath('app/other.py')
    expect(codeText()).toBe(OTHER)
    rerenderPath('app/main.py') // back to the file that sent the request
    expect(codeText()).toBe(MAIN)
    expect([actionButton().textContent, actionButton().disabled]).toEqual(['Find unreachable code', false])
    hold.held[0].release(unreachableResult())
    await sleep(50)
    expect([actionButton().textContent, rowTexts()]).toEqual(['Find unreachable code', []])
    expect(screen.queryByText(HEADING_TEXT)).toBeNull()
  })

  it('never carries an error into another already-loaded file', async () => {
    const { rerenderPath } = await openBothFiles({ [GET_UNREACHABLE]: { status: 409, data: { detail: CHANGED } } })
    fireEvent.click(actionButton())
    await screen.findByRole('alert')
    rerenderPath('app/other.py')
    expect(codeText()).toBe(OTHER)
    expect(within(region()).queryByRole('alert')).toBeNull()
    expect(actionButton().textContent).toBe('Find unreachable code')
  })

  it('shows a fresh panel when switching from a non-Python file to a Python file', async () => {
    const { requests, rerenderPath } = renderViewer('docs/notes.md', { [GET_CONTENT]: contentRoute })
    await waitFor(() => expect(codeText()).toBe('# Notes\n'))
    expect(screen.queryByRole('region', { name: 'Unreachable code' })).toBeNull()
    rerenderPath('app/main.py')
    await waitFor(() => expect(codeText()).toBe(MAIN))
    expect([actionButton().textContent, actionButton().disabled, rowTexts()]).toEqual([
      'Find unreachable code',
      false,
      [],
    ])
    await sleep(30)
    expect(byUrl(requests, UNREACHABLE_PATH)).toHaveLength(0)
  })

  it('stays silent when a response arrives after the panel is unmounted', async () => {
    const consoleError = vi.spyOn(console, 'error')
    const hold = holdUnreachable()
    const { unmount } = renderPanel()
    fireEvent.click(actionButton())
    await screen.findByRole('button', { name: 'Finding unreachable code…' })
    unmount()
    hold.held[0].release(unreachableResult())
    await sleep(50)
    expect(screen.queryByRole('region', { name: 'Unreachable code' })).toBeNull()
    expect(rowTexts()).toHaveLength(0)
    expect(consoleError).not.toHaveBeenCalled()
  })
})

// --- H. Security --------------------------------------------------------------------------------

describe('Unreachable code security', () => {
  it.each([
    ['as buttons', true],
    ['as plain text', false],
  ])('never renders a markup-like cause from the server %s', async (_name, navigable) => {
    const hostile = { line: 2, end_line: 2, statements: 1, cause: '<img src=x onerror=alert(1)>', cause_line: 1 }
    await panelResult(
      unreachableResult({ regions: [hostile as unknown as UnreachableRegion] }),
      navigable ? { onSelectLine: vi.fn<Navigate>() } : {},
    )
    expect(rowTexts()).toEqual(['line 2 (1 statement) after a statement that always leaves on line 1'])
    expect(document.querySelector('img, script, iframe, [src], [onerror]')).toBeNull()
    expect(region().innerHTML).not.toContain('onerror')
  })

  it('shows only the named fields of the response', async () => {
    const extra = { snippet: 'FAKE-SNIPPET-MARKER', sha256: 'FAKE-SHA-MARKER', id: 'FAKE-ID-MARKER' }
    await panelResult(
      {
        ...unreachableResult({
          regions: REGIONS.map((r) => ({ ...r, ...extra })) as UnreachableRegion[],
        }),
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
    expect(regionText()).not.toContain('app/main.py') // the path is used for navigation, never shown
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
      const { requests } = await panelResult(unreachableResult(), { onSelectLine: vi.fn<Navigate>() })
      fireEvent.click(rowButtons()[0])
      fireEvent.click(causeButtons()[0])
      expect(new Set(getItem.mock.calls.map((call) => call[0]))).toEqual(new Set(['ai_se_access_token']))
      for (const spy of writes) {
        expect(spy).not.toHaveBeenCalled()
      }
      expect([localStorage.length, sessionStorage.length]).toEqual(stored)
      expect(cookieGet).not.toHaveBeenCalled()
      expect(cookieSet).not.toHaveBeenCalled()
      expect(fetchSpy).not.toHaveBeenCalled()
      expect(requests.map((r) => `${r.method} ${r.url}`)).toEqual([GET_UNREACHABLE])
      expect(requests.every((r) => r.baseURL === '/api/v1')).toBe(true)
    } finally {
      globalThis.fetch = realFetch
      delete (document as { cookie?: unknown }).cookie
    }
  })

  it('renders identical regions without React key warnings', async () => {
    const consoleError = vi.spyOn(console, 'error')
    const same = { line: 5, end_line: 5, statements: 1, cause: 'return' as const, cause_line: 4 }
    await panelResult(unreachableResult({ regions: [same, same, { ...same, line: 9, end_line: 9 }] }))
    fireEvent.click(actionButton()) // a refresh re-renders the same entries
    await waitFor(() => expect(actionButton().disabled).toBe(false))
    expect(rowTexts()).toEqual([
      'line 5 (1 statement) after a return on line 4',
      'line 5 (1 statement) after a return on line 4',
      'line 9 (1 statement) after a return on line 4',
    ])
    const messages = consoleError.mock.calls.map((call) => call.map(String).join(' '))
    expect(messages.filter((m) => /same key|unique "key"/.test(m))).toEqual([])
  })
})

// --- I. Labels ----------------------------------------------------------------------------------

describe('Unreachable code labels', () => {
  it('keeps the default labels clear of the frozen test patterns and of file paths', async () => {
    renderViewer('app/main.py', { [GET_CONTENT]: contentRoute })
    await waitFor(() => expect(codeText()).toBe(MAIN))
    const names = within(region())
      .getAllByRole('button')
      .map((b) => b.textContent ?? '')
    expect(names).toEqual(['Find unreachable code'])
    const frozen = [
      /definition/i,
      /outline/i,
      /^Go to/,
      /^Open/,
      /^(Jump|Open) /,
      /^Find references/,
      /^Finding references/,
      /in the viewer/,
      /^Call hierarchy/,
      /hierarchy for/i,
      /method overrides for/i,
      /^Search/,
      /^Cancel /,
      /^Loading/,
      /^Line /,
      /unbound names/i,
      /unused variables/i,
      /unused imports/i,
      /redefined names/i,
      /occurrences/,
      /importers|Retry/,
      /main\.py/,
    ]
    for (const pattern of frozen) {
      expect(names.some((name) => pattern.test(name))).toBe(false)
      expect(pattern.test('Unreachable code')).toBe(false)
    }
    // The frozen go-to-definition helper (/definition/i) still finds exactly one button.
    expect(screen.getAllByRole('button', { name: /definition/i }).map((b) => b.textContent)).toEqual([
      'Go to definition',
    ])
  })
})

// --- J. FileViewer integration ------------------------------------------------------------------

describe('Unreachable code in the file viewer', () => {
  it('places the panel right after Redefined names, keeping every other panel in order', async () => {
    renderViewer('app/main.py', { [GET_CONTENT]: contentRoute })
    await waitFor(() => expect(codeText()).toBe(MAIN))
    expect(screen.getAllByRole('region').map((r) => r.getAttribute('aria-label'))).toEqual([
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
    expect(screen.getByRole('region', { name: 'Redefined names' }).nextElementSibling).toBe(region())
  })

  it("requests the open file's server path and never puts it in the labels", async () => {
    const seen: Params[] = []
    renderViewer('pkg/a b.py', {
      [GET_CONTENT]: (config) => content((config.params as { path: string }).path, MAIN),
      [GET_UNREACHABLE]: (config) => {
        seen.push({ ...(config.params as Params) })
        return { status: 200, data: unreachableResult({ path: 'pkg/a b.py' }) }
      },
    })
    await waitFor(() => expect(codeText()).toBe(MAIN))
    fireEvent.click(actionButton())
    await loaded()
    expect(seen).toEqual([{ path: 'pkg/a b.py' }])
    const names = within(region())
      .getAllByRole('button')
      .map((b) => `${b.getAttribute('aria-label') ?? ''} ${b.textContent ?? ''}`)
    expect(names).toHaveLength(9) // the action, four regions and their four causes
    expect(names.some((name) => name.includes('a b.py'))).toBe(false)
  })
})
