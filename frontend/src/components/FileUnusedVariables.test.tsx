import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { AxiosError, type AxiosRequestConfig, type InternalAxiosRequestConfig } from 'axios'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { apiClient } from '../services/client'
import { mockApi, type MockResponse, PROJECT, type RecordedRequest, ZIP_REPOSITORY } from '../test/mockApi'
import { createTestQueryClient } from '../test/render'
import type { FileContent, UnusedVariable, UnusedVariablesResult, UnusedVariablesSummary } from '../types/api'
import {
  EMPTY_RESULT,
  FileUnusedVariables,
  HEADING,
  STATUS_MESSAGES,
  TRUNCATED_NOTICE,
  UNKNOWN_STATUS,
  unsupportedNote,
  unusedSummary,
} from './FileUnusedVariables'
import { FileViewer, type LineSelection } from './FileViewer'

const P = PROJECT.id
const R = ZIP_REPOSITORY.id
const BASE = `/projects/${P}/repositories/${R}`
const UNUSED_PATH = `${BASE}/files/unused-variables`
const GET_UNUSED = `GET ${UNUSED_PATH}`
const GET_CONTENT = `GET ${BASE}/files/content`

const MAIN = 'def build(rows):\n    header = 1\n    return rows\n'
const OTHER = 'def other():\n    tmp = 2\n'

// The approved wording, written here independently of the component.
const HEADING_TEXT = 'Local variables that are never read'
const EMPTY_TEXT = 'No unused variables found.'
const TRUNCATED_TEXT = 'Unused variable results are truncated: only the first 500 are shown.'
const UNKNOWN_TEXT = 'Unused variables are unavailable.'
const FALLBACK = 'Could not check unused variables'
const CHANGED = 'Repository changed during analysis; run it again'
const MESSAGES = {
  syntax_error: 'Unused variables are unavailable: this file could not be parsed as Python.',
  too_large: 'Unused variables are unavailable: this file is too large to analyze (limit 256 KiB).',
  binary: 'Unused variables are unavailable: this file is not text.',
  unsupported_language: 'Unused variables are only available for Python files.',
}

// Server order, deliberately not sorted by name or line, with a repeated variable.
const VARIABLES: UnusedVariable[] = [
  { line: 5, column: 9, name: 'total' },
  { line: 2, column: 5, name: 'zeta' },
  { line: 7, column: 5, name: 'total' },
  { line: 9, column: 13, name: 'alpha' },
]
const ROW_TEXTS = [
  'total line 5, column 9',
  'zeta line 2, column 5',
  'total line 7, column 5',
  'alpha line 9, column 13',
]
const ROW_LABELS = [
  'Go to total at line 5, column 9',
  'Go to zeta at line 2, column 5',
  'Go to total at line 7, column 5',
  'Go to alpha at line 9, column 13',
]
// Every count differs from the rows (4 assignments, 3 distinct names), so a recount shows.
const SUMMARY: UnusedVariablesSummary = { unused: 7, distinct: 5, unsupported: 3 }
const SUMMARY_TEXT = 'Unused: 7 assignments to 5 variables.'
const NOTE_TEXT = 'Not listed: 3 variables possibly read dynamically or through string annotations.'
const ZERO: UnusedVariablesSummary = { unused: 0, distinct: 0, unsupported: 0 }

function unusedResult(extra: Partial<UnusedVariablesResult> = {}): UnusedVariablesResult {
  return { path: 'app/main.py', status: 'parsed', variables: VARIABLES, summary: SUMMARY, truncated: false, ...extra }
}

function notParsed(status: string): UnusedVariablesResult {
  return {
    path: 'app/main.py',
    status: status as UnusedVariablesResult['status'],
    variables: [],
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
const region = () => screen.getByRole('region', { name: 'Unused variables' })
const actionButton = () => within(region()).getByRole('button', { name: /unused variables/i }) as HTMLButtonElement
const rowTexts = () => screen.queryAllByTestId('unused-variable').map((row) => row.textContent)
const rowButtons = () => within(region()).queryAllByRole('button', { name: /^Go to / }) as HTMLButtonElement[]
const regionText = () => region().textContent ?? ''
const byUrl = (requests: RecordedRequest[], url: string) => requests.filter((r) => r.url === url)
const loaded = () =>
  waitFor(() =>
    expect(
      within(region()).queryByRole('button', { name: 'Refresh unused variables' }) ??
        within(region()).queryByRole('alert'),
    ).not.toBeNull(),
  )

function renderPanel(
  handler: Handler = { status: 200, data: unusedResult() },
  props: { path?: string; language?: string | null; onSelectLine?: Navigate } = {},
) {
  const seen: Params[] = []
  const requests = mockApi({
    [GET_UNUSED]: (config) => {
      seen.push({ ...(config.params as Params) })
      return typeof handler === 'function' ? handler(config) : handler
    },
  })
  const client = createTestQueryClient()
  const ui = () => (
    <QueryClientProvider client={client}>
      <FileUnusedVariables
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

/** Holds every unused-variables request until released; other requests go through. */
function holdUnused() {
  const held: { params: Params; release: (data: UnusedVariablesResult) => void }[] = []
  const realGet = apiClient.get.bind(apiClient)
  const spy = vi.spyOn(apiClient, 'get').mockImplementation(((url: string, config?: AxiosRequestConfig) => {
    if (url === UNUSED_PATH) {
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

describe('Unused variables visibility', () => {
  it('shows the panel for a Python file without requesting anything on open or line selection', async () => {
    const { requests, rerenderPath } = renderViewer('app/main.py', { [GET_CONTENT]: contentRoute })
    await waitFor(() => expect(codeText()).toBe(MAIN))
    expect([actionButton().textContent, actionButton().disabled]).toEqual(['Find unused variables', false])
    rerenderPath('app/main.py', { line: 2 }) // a line selected in the open file
    fireEvent.mouseUp(document.querySelector('pre') as HTMLPreElement)
    await sleep(50)
    expect(byUrl(requests, UNUSED_PATH)).toHaveLength(0)
    expect(screen.queryByText(HEADING_TEXT)).toBeNull()
  })

  it.each([['Markdown'], [null], ['JavaScript']])(
    'renders nothing and requests nothing for the language %s',
    async (language) => {
      const { container, requests } = renderPanel(undefined, { language })
      expect(container.innerHTML).toBe('')
      expect(screen.queryByRole('button')).toBeNull()
      expect(screen.queryByRole('region', { name: 'Unused variables' })).toBeNull()
      await sleep(30)
      expect(requests).toHaveLength(0)
    },
  )

  it('shows no unused variables panel for a non-Python file in the viewer', async () => {
    const { requests } = renderViewer('docs/notes.md', { [GET_CONTENT]: contentRoute })
    await waitFor(() => expect(codeText()).toBe('# Notes\n'))
    expect(screen.queryByRole('region', { name: 'Unused variables' })).toBeNull()
    await sleep(30)
    expect(byUrl(requests, UNUSED_PATH)).toHaveLength(0)
  })
})

// --- B. Requests --------------------------------------------------------------------------------

describe('Unused variables requests', () => {
  it('sends exactly one GET to the endpoint with params exactly { path } on an explicit click', async () => {
    const { seen, requests } = renderPanel()
    fireEvent.click(actionButton())
    await loaded()
    expect(seen).toEqual([{ path: 'app/main.py' }])
    expect(Object.keys(seen[0])).toEqual(['path'])
    for (const forbidden of ['line', 'column', 'name', 'cursor', 'limit', 'kind']) {
      expect(seen[0]).not.toHaveProperty(forbidden)
    }
    expect(requests.map((r) => [r.method, r.url, r.baseURL, r.data])).toEqual([
      ['GET', UNUSED_PATH, '/api/v1', undefined],
    ])
    await sleep(50)
    expect(byUrl(requests, UNUSED_PATH)).toHaveLength(1)
  })

  it('sends an unusual server path unchanged, as a query parameter only', async () => {
    const { seen, requests } = await panelResult(unusedResult(), { path: 'a b/ü&x=1#.py' })
    expect(seen).toEqual([{ path: 'a b/ü&x=1#.py' }])
    expect(requests.map((r) => r.url)).toEqual([UNUSED_PATH]) // never concatenated into the URL
  })

  it('requests nothing on mount or on re-render', async () => {
    const { requests, rerenderPanel } = renderPanel()
    rerenderPanel()
    rerenderPanel()
    await sleep(50)
    expect(requests).toHaveLength(0)
    expect(actionButton().textContent).toBe('Find unused variables')
  })

  it('shows the loading label, disables the button while pending and sends no second request', async () => {
    const hold = holdUnused()
    renderPanel()
    fireEvent.click(actionButton())
    await screen.findByRole('button', { name: 'Finding unused variables…' })
    expect(actionButton().disabled).toBe(true)
    fireEvent.click(actionButton())
    fireEvent.click(actionButton())
    await sleep(20)
    expect(hold.held).toHaveLength(1)
    expect(hold.held[0].params).toEqual({ path: 'app/main.py' })
    hold.held[0].release(unusedResult())
    await screen.findByRole('button', { name: 'Refresh unused variables' })
    expect(actionButton().disabled).toBe(false)
  })

  it('offers Refresh after a result and requests again, freshly, on every click', async () => {
    const { seen } = await panelResult(unusedResult())
    expect(actionButton().textContent).toBe('Refresh unused variables')
    await sleep(30)
    expect(seen).toHaveLength(1)
    fireEvent.click(actionButton())
    await waitFor(() => expect(seen).toHaveLength(2))
    fireEvent.click(actionButton())
    await waitFor(() => expect(seen).toHaveLength(3))
    expect(seen).toEqual([{ path: 'app/main.py' }, { path: 'app/main.py' }, { path: 'app/main.py' }])
  })

  it('caches nothing: a remounted panel starts fresh and requests again', async () => {
    const { seen, client, unmount } = await panelResult(unusedResult())
    unmount()
    render(
      <QueryClientProvider client={client}>
        <FileUnusedVariables projectId={P} repositoryId={R} path="app/main.py" language="Python" />
      </QueryClientProvider>,
    )
    expect([actionButton().textContent, rowTexts()]).toEqual(['Find unused variables', []])
    expect(screen.queryByText(HEADING_TEXT)).toBeNull()
    await sleep(30)
    expect(seen).toHaveLength(1)
    fireEvent.click(actionButton())
    await loaded()
    expect(seen).toHaveLength(2)
  })
})

// --- C. Results ---------------------------------------------------------------------------------

describe('Unused variables results', () => {
  it('renders every assignment with its exact text in server order, duplicates included', async () => {
    await panelResult(unusedResult())
    expect(screen.getByRole('heading', { name: HEADING_TEXT })).toBeTruthy()
    expect(rowTexts()).toEqual(ROW_TEXTS)
    expect(screen.getByRole('list', { name: 'Unused variable assignments' })).toBeTruthy()
    expect(regionText()).not.toContain('unavailable')
    expect(regionText()).not.toContain(EMPTY_TEXT)
  })

  it('shows the server summary even when it differs from the rows', async () => {
    await panelResult(unusedResult())
    expect(screen.getByTestId('unused-summary').textContent).toBe(SUMMARY_TEXT)
    expect(rowTexts()).toHaveLength(4) // 4 rows, 3 distinct names: the summary is never recounted
  })

  it.each([
    ['one of each', { unused: 1, distinct: 1, unsupported: 0 }, 'Unused: 1 assignment to 1 variable.'],
    ['plural both', { unused: 3, distinct: 2, unsupported: 0 }, 'Unused: 3 assignments to 2 variables.'],
    ['plural then singular', { unused: 2, distinct: 1, unsupported: 0 }, 'Unused: 2 assignments to 1 variable.'],
  ])('words the server summary with singular and plural nouns (%s)', async (_name, summary, text) => {
    await panelResult(unusedResult({ summary }))
    expect(screen.getByTestId('unused-summary').textContent).toBe(text)
  })

  it.each([
    [3, 'Not listed: 3 variables possibly read dynamically or through string annotations.'],
    [1, 'Not listed: 1 variable possibly read dynamically or through string annotations.'],
    [0, null],
  ])('shows the unsupported note only when the server count is above zero (%i)', async (unsupported, note) => {
    await panelResult(unusedResult({ summary: { ...SUMMARY, unsupported } }))
    if (note === null) {
      expect(regionText()).not.toContain('Not listed')
    } else {
      expect(regionText()).toContain(note)
    }
  })

  it.each([
    ['true with four rows', true, VARIABLES],
    ['false with 500 rows', false, Array.from({ length: 500 }, (_, i) => ({ line: i + 1, column: 5, name: `v${i}` }))],
  ])('follows the server truncation flag only: %s', async (_name, truncated, variables) => {
    await panelResult(unusedResult({ truncated, variables }))
    expect(regionText().includes(TRUNCATED_TEXT)).toBe(truncated)
    expect(rowTexts()).toHaveLength(variables.length) // the server's rows, never trimmed here
  })

  it('shows the empty message for a parsed result without assignments', async () => {
    await panelResult(unusedResult({ variables: [], summary: ZERO }))
    expect(regionText()).toContain(EMPTY_TEXT)
    expect(screen.getByTestId('unused-summary').textContent).toBe('Unused: 0 assignments to 0 variables.')
    expect(screen.queryByRole('list', { name: 'Unused variable assignments' })).toBeNull()
  })
})

// --- D. Navigation ------------------------------------------------------------------------------

describe('Unused variables navigation', () => {
  it("navigates every assignment to the server's path at its line, never the column", async () => {
    const onSelectLine = vi.fn<Navigate>()
    // The requested path and the server's path deliberately differ: the server's is used.
    await panelResult(unusedResult({ path: 'server/actual.py' }), { path: 'requested/asked.py', onSelectLine })
    const buttons = rowButtons()
    expect(buttons.map((b) => b.getAttribute('aria-label'))).toEqual(ROW_LABELS)
    expect(buttons.map((b) => b.textContent)).toEqual(ROW_TEXTS) // the column is shown
    buttons.forEach((button) => fireEvent.click(button))
    expect(onSelectLine.mock.calls).toEqual([
      ['server/actual.py', 5],
      ['server/actual.py', 2],
      ['server/actual.py', 7], // the repeated variable navigates to its own line
      ['server/actual.py', 9],
    ])
    expect(onSelectLine.mock.calls.every((call) => call.length === 2)).toBe(true)
  })

  it('renders assignments as plain text without onSelectLine', async () => {
    await panelResult(unusedResult())
    expect(rowTexts()).toEqual(ROW_TEXTS)
    expect(rowButtons()).toHaveLength(0)
  })

  it('goes from Find to a row to the open file at the assignment line in the viewer', async () => {
    const { onSelectLine } = renderViewer('app/main.py', {
      [GET_CONTENT]: contentRoute,
      [GET_UNUSED]: { status: 200, data: unusedResult() },
    })
    await waitFor(() => expect(codeText()).toBe(MAIN))
    fireEvent.click(actionButton())
    await loaded()
    fireEvent.click(within(region()).getByRole('button', { name: 'Go to zeta at line 2, column 5' }))
    expect(onSelectLine).toHaveBeenCalledWith('app/main.py', 2)
  })
})

// --- E. Statuses --------------------------------------------------------------------------------

describe('Unused variables statuses', () => {
  it.each(Object.entries(MESSAGES))('status %s shows only its message', async (status, message) => {
    renderPanel({ status: 200, data: { ...notParsed(status), summary: SUMMARY, truncated: true } })
    fireEvent.click(actionButton())
    await screen.findByText(message)
    expect(screen.queryByTestId('unused-summary')).toBeNull()
    expect(screen.queryByText(HEADING_TEXT)).toBeNull()
    expect(screen.queryByText(EMPTY_TEXT)).toBeNull()
    expect(regionText()).not.toContain('Not listed')
    expect(regionText()).not.toContain('truncated')
    expect(rowTexts()).toHaveLength(0)
  })

  it('shows a safe fallback for an unknown status', async () => {
    renderPanel({ status: 200, data: notParsed('something_new') })
    fireEvent.click(actionButton())
    await screen.findByText(UNKNOWN_TEXT)
    expect(screen.queryByText(EMPTY_TEXT)).toBeNull()
    expect(screen.queryByTestId('unused-summary')).toBeNull()
  })

  it('has the approved wording', () => {
    expect(STATUS_MESSAGES).toEqual(MESSAGES)
    expect([HEADING, EMPTY_RESULT, TRUNCATED_NOTICE, UNKNOWN_STATUS]).toEqual([
      HEADING_TEXT,
      EMPTY_TEXT,
      TRUNCATED_TEXT,
      UNKNOWN_TEXT,
    ])
    expect(unusedSummary(SUMMARY)).toBe(SUMMARY_TEXT)
    expect(unsupportedNote(3)).toBe(NOTE_TEXT)
  })
})

// --- F. Errors ----------------------------------------------------------------------------------

describe('Unused variables errors', () => {
  it.each([
    [404, 'Repository not found'],
    [409, CHANGED],
    [429, 'Too many attempts; please try again later'],
  ])('shows the server detail for %i exactly, without retrying', async (status, detail) => {
    const { requests } = renderPanel({ status, data: { detail } })
    fireEvent.click(actionButton())
    const alerts = await screen.findAllByRole('alert')
    expect(alerts.map((a) => a.textContent)).toEqual([detail])
    await sleep(50)
    expect(byUrl(requests, UNUSED_PATH)).toHaveLength(1)
    expect(rowTexts()).toHaveLength(0)
  })

  it('shows the fallback for a 500 without a usable detail, without retrying', async () => {
    const { requests } = renderPanel({ status: 500, data: {} })
    fireEvent.click(actionButton())
    expect((await screen.findByRole('alert')).textContent).toBe(FALLBACK)
    await sleep(50)
    expect(byUrl(requests, UNUSED_PATH)).toHaveLength(1)
  })

  it('uses the existing network message without retrying', async () => {
    const { requests } = renderPanel(() => {
      throw new AxiosError('Network Error', AxiosError.ERR_NETWORK)
    })
    fireEvent.click(actionButton())
    expect((await screen.findByRole('alert')).textContent).toBe('Could not reach the server')
    await sleep(50)
    expect(byUrl(requests, UNUSED_PATH)).toHaveLength(1)
  })

  it('returns to the Find label after an error', async () => {
    renderPanel({ status: 429, data: { detail: 'Too many attempts; please try again later' } })
    fireEvent.click(actionButton())
    await screen.findByRole('alert')
    expect([actionButton().textContent, actionButton().disabled]).toEqual(['Find unused variables', false])
  })

  it('clears a previous error when a later request succeeds', async () => {
    let calls = 0
    const { seen } = renderPanel(() =>
      ++calls === 1 ? { status: 409, data: { detail: CHANGED } } : { status: 200, data: unusedResult() },
    )
    fireEvent.click(actionButton())
    expect((await screen.findByRole('alert')).textContent).toBe(CHANGED)
    fireEvent.click(actionButton())
    await screen.findByRole('button', { name: 'Refresh unused variables' })
    expect(screen.queryByRole('alert')).toBeNull()
    expect(rowTexts()).toEqual(ROW_TEXTS)
    expect(seen).toHaveLength(2)
  })
})

// --- G. Stale results ---------------------------------------------------------------------------

describe('Unused variables never show a stale result', () => {
  it('never shows a pending request’s result in another file', async () => {
    const hold = holdUnused()
    const { rerenderPath } = renderViewer('app/main.py', { [GET_CONTENT]: contentRoute })
    await waitFor(() => expect(codeText()).toBe(MAIN))
    fireEvent.click(actionButton())
    await screen.findByRole('button', { name: 'Finding unused variables…' })
    rerenderPath('app/other.py')
    await waitFor(() => expect(codeText()).toBe(OTHER))
    hold.held[0].release(unusedResult())
    await sleep(50)
    expect([actionButton().textContent, actionButton().disabled]).toEqual(['Find unused variables', false])
    expect(rowTexts()).toHaveLength(0)
    expect(screen.queryByText(HEADING_TEXT)).toBeNull()
  })

  it('starts fresh after leaving a loaded result, with no request when returning', async () => {
    const { requests, rerenderPath } = renderViewer('app/main.py', {
      [GET_CONTENT]: contentRoute,
      [GET_UNUSED]: { status: 200, data: unusedResult() },
    })
    await waitFor(() => expect(codeText()).toBe(MAIN))
    fireEvent.click(actionButton())
    await loaded()
    expect(rowTexts()).toEqual(ROW_TEXTS)
    rerenderPath('app/other.py')
    await waitFor(() => expect(codeText()).toBe(OTHER))
    expect([actionButton().textContent, rowTexts()]).toEqual(['Find unused variables', []])
    rerenderPath('app/main.py')
    await waitFor(() => expect(codeText()).toBe(MAIN))
    await sleep(50)
    expect([actionButton().textContent, rowTexts()]).toEqual(['Find unused variables', []])
    expect(byUrl(requests, UNUSED_PATH)).toHaveLength(1)
  })

  it('never carries a result into another already-loaded file', async () => {
    const { rerenderPath } = await openBothFiles({ [GET_UNUSED]: { status: 200, data: unusedResult() } })
    fireEvent.click(actionButton())
    await loaded()
    expect(rowTexts()).toEqual(ROW_TEXTS)
    rerenderPath('app/other.py')
    expect(codeText()).toBe(OTHER) // shown at once: the panel stays mounted unless keyed by path
    expect([actionButton().textContent, actionButton().disabled, rowTexts()]).toEqual([
      'Find unused variables',
      false,
      [],
    ])
    expect(screen.queryByText(HEADING_TEXT)).toBeNull()
    expect(screen.queryByTestId('unused-summary')).toBeNull()
  })

  it('never shows a late response of the previous file in another already-loaded file', async () => {
    const hold = holdUnused()
    const { rerenderPath } = await openBothFiles()
    fireEvent.click(actionButton())
    await screen.findByRole('button', { name: 'Finding unused variables…' })
    rerenderPath('app/other.py')
    expect(codeText()).toBe(OTHER)
    expect([actionButton().textContent, actionButton().disabled]).toEqual(['Find unused variables', false])
    hold.held[0].release(unusedResult())
    await sleep(50)
    expect([actionButton().textContent, actionButton().disabled, rowTexts()]).toEqual([
      'Find unused variables',
      false,
      [],
    ])
    expect(screen.queryByText(HEADING_TEXT)).toBeNull()
  })

  it('ignores a late response after switching away and back between already-loaded files', async () => {
    const hold = holdUnused()
    const { rerenderPath } = await openBothFiles()
    fireEvent.click(actionButton())
    await screen.findByRole('button', { name: 'Finding unused variables…' })
    rerenderPath('app/other.py')
    expect(codeText()).toBe(OTHER)
    rerenderPath('app/main.py') // back to the file that sent the request
    expect(codeText()).toBe(MAIN)
    expect([actionButton().textContent, actionButton().disabled]).toEqual(['Find unused variables', false])
    hold.held[0].release(unusedResult())
    await sleep(50)
    expect([actionButton().textContent, rowTexts()]).toEqual(['Find unused variables', []])
    expect(screen.queryByText(HEADING_TEXT)).toBeNull()
  })

  it('never carries an error into another already-loaded file', async () => {
    const { rerenderPath } = await openBothFiles({ [GET_UNUSED]: { status: 409, data: { detail: CHANGED } } })
    fireEvent.click(actionButton())
    await screen.findByRole('alert')
    rerenderPath('app/other.py')
    expect(codeText()).toBe(OTHER)
    expect(within(region()).queryByRole('alert')).toBeNull()
    expect(actionButton().textContent).toBe('Find unused variables')
  })

  it('shows a fresh panel when switching from a non-Python file to a Python file', async () => {
    const { requests, rerenderPath } = renderViewer('docs/notes.md', { [GET_CONTENT]: contentRoute })
    await waitFor(() => expect(codeText()).toBe('# Notes\n'))
    expect(screen.queryByRole('region', { name: 'Unused variables' })).toBeNull()
    rerenderPath('app/main.py')
    await waitFor(() => expect(codeText()).toBe(MAIN))
    expect([actionButton().textContent, actionButton().disabled, rowTexts()]).toEqual([
      'Find unused variables',
      false,
      [],
    ])
    await sleep(30)
    expect(byUrl(requests, UNUSED_PATH)).toHaveLength(0)
  })
})

// --- H. Security --------------------------------------------------------------------------------

describe('Unused variables security', () => {
  const HOSTILE: UnusedVariable[] = [
    { line: 1, column: 1, name: '<dangerous>' },
    { line: 2, column: 5, name: '<img src=x onerror=alert(1)>' },
    { line: 3, column: 9, name: '<b>bold</b>' },
  ]

  it.each([
    ['as buttons', true],
    ['as plain text', false],
  ])('renders markup-like names as literal text %s', async (_name, navigable) => {
    await panelResult(unusedResult({ variables: HOSTILE }), navigable ? { onSelectLine: vi.fn<Navigate>() } : {})
    expect(rowTexts()).toEqual([
      '<dangerous> line 1, column 1',
      '<img src=x onerror=alert(1)> line 2, column 5',
      '<b>bold</b> line 3, column 9',
    ])
    expect(document.querySelector('dangerous, img, script, iframe, b')).toBeNull()
    expect(document.querySelector('[src], [onerror]')).toBeNull()
    expect(region().innerHTML).toContain('&lt;b&gt;bold&lt;/b&gt;') // escaped, never parsed as HTML
    if (navigable) {
      expect(rowButtons()[1].getAttribute('aria-label')).toBe('Go to <img src=x onerror=alert(1)> at line 2, column 5')
    }
  })

  it('shows only the named fields of the response', async () => {
    const extra = { snippet: 'FAKE-SNIPPET-MARKER', sha256: 'FAKE-SHA-MARKER', id: 'FAKE-ID-MARKER' }
    await panelResult(
      {
        ...unusedResult({ variables: VARIABLES.map((v) => ({ ...v, ...extra })) as UnusedVariable[] }),
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
      const { requests } = await panelResult(unusedResult(), { onSelectLine: vi.fn<Navigate>() })
      fireEvent.click(rowButtons()[0])
      expect(new Set(getItem.mock.calls.map((call) => call[0]))).toEqual(new Set(['ai_se_access_token']))
      for (const spy of writes) {
        expect(spy).not.toHaveBeenCalled()
      }
      expect([localStorage.length, sessionStorage.length]).toEqual(stored)
      expect(cookieGet).not.toHaveBeenCalled()
      expect(cookieSet).not.toHaveBeenCalled()
      expect(fetchSpy).not.toHaveBeenCalled()
      expect(requests.map((r) => `${r.method} ${r.url}`)).toEqual([GET_UNUSED])
      expect(requests.every((r) => r.baseURL === '/api/v1')).toBe(true)
    } finally {
      globalThis.fetch = realFetch
      delete (document as { cookie?: unknown }).cookie
    }
  })

  it('renders repeated entries without React key warnings', async () => {
    const consoleError = vi.spyOn(console, 'error')
    const duplicates: UnusedVariable[] = [
      { line: 2, column: 5, name: 'x' },
      { line: 2, column: 5, name: 'x' },
      { line: 7, column: 5, name: 'x' },
    ]
    await panelResult(unusedResult({ variables: duplicates }))
    fireEvent.click(actionButton()) // a refresh re-renders the same entries
    await waitFor(() => expect(actionButton().disabled).toBe(false))
    expect(rowTexts()).toEqual(['x line 2, column 5', 'x line 2, column 5', 'x line 7, column 5'])
    const messages = consoleError.mock.calls.map((call) => call.map(String).join(' '))
    expect(messages.filter((m) => /same key|unique "key"/.test(m))).toEqual([])
  })
})

// --- I. Labels ----------------------------------------------------------------------------------

describe('Unused variables labels', () => {
  it('keeps the default labels clear of the frozen test patterns and of file paths', async () => {
    renderViewer('app/main.py', { [GET_CONTENT]: contentRoute })
    await waitFor(() => expect(codeText()).toBe(MAIN))
    const names = within(region())
      .getAllByRole('button')
      .map((b) => b.textContent ?? '')
    expect(names).toEqual(['Find unused variables'])
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
      /occurrences/,
      /importers|Retry/,
      /main\.py/,
    ]
    for (const pattern of frozen) {
      expect(names.some((name) => pattern.test(name))).toBe(false)
      expect(pattern.test('Unused variables')).toBe(false)
    }
    // The frozen go-to-definition helper (/definition/i) still finds exactly one button.
    expect(screen.getAllByRole('button', { name: /definition/i }).map((b) => b.textContent)).toEqual([
      'Go to definition',
    ])
  })
})

// --- J. FileViewer integration ------------------------------------------------------------------

describe('Unused variables in the file viewer', () => {
  it('places the panel right after Unbound names and before Go to definition', async () => {
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
    ])
    expect(screen.getByRole('region', { name: 'Unbound names' }).nextElementSibling).toBe(region())
    expect(region().nextElementSibling).toBe(screen.getByRole('region', { name: 'Go to definition' }))
  })

  it("requests the open file's server path and never puts it in the labels", async () => {
    const seen: Params[] = []
    renderViewer('pkg/a b.py', {
      [GET_CONTENT]: (config) => content((config.params as { path: string }).path, MAIN),
      [GET_UNUSED]: (config) => {
        seen.push({ ...(config.params as Params) })
        return { status: 200, data: unusedResult({ path: 'pkg/a b.py' }) }
      },
    })
    await waitFor(() => expect(codeText()).toBe(MAIN))
    fireEvent.click(actionButton())
    await loaded()
    expect(seen).toEqual([{ path: 'pkg/a b.py' }])
    const names = within(region())
      .getAllByRole('button')
      .map((b) => `${b.getAttribute('aria-label') ?? ''} ${b.textContent ?? ''}`)
    expect(names).toHaveLength(5) // the action and four assignments
    expect(names.some((name) => name.includes('a b.py'))).toBe(false)
  })
})
