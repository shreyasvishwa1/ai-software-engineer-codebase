import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { AxiosError, type AxiosRequestConfig, type InternalAxiosRequestConfig } from 'axios'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { apiClient } from '../services/client'
import { mockApi, type MockResponse, PROJECT, type RecordedRequest, ZIP_REPOSITORY } from '../test/mockApi'
import { createTestQueryClient } from '../test/render'
import type {
  CodeOccurrencesResult,
  FileContent,
  Occurrence,
  OccurrenceKind,
  OccurrenceScope,
  OccurrencesSummary,
} from '../types/api'
import {
  CodeOccurrences,
  EMPTY_RESULT,
  MODULE_NOTE,
  NO_POSITION,
  occurrenceLabel,
  STATUS_MESSAGES,
  summaryText,
  TRUNCATED_NOTICE,
  UNKNOWN_STATUS,
} from './CodeOccurrences'
import { FileViewer, type LineSelection } from './FileViewer'

const P = PROJECT.id
const R = ZIP_REPOSITORY.id
const BASE = `/projects/${P}/repositories/${R}`
const OCCURRENCES_PATH = `${BASE}/files/occurrences`
const GET_OCCURRENCES = `GET ${OCCURRENCES_PATH}`
const GET_CONTENT = `GET ${BASE}/files/content`
const GET_DEFINITION = `GET ${BASE}/files/definition`
const HTML = '<img src=x onerror=alert(1)>'
const CHANGED = 'Repository changed during analysis; run it again'

const MAIN = 'def total(items):\n    acc = 0\n    for item in items:\n        acc += item\n    return acc\n'
const OTHER = 'value = 1\nprint(value)\n'
const ACC_OFFSET = MAIN.indexOf('return acc') + 'return '.length // the returned acc: line 5, column 12

// The approved wording, written here independently of the component.
const NO_POSITION_TEXT = 'Place the cursor on a name in the code first.'
const MODULE_TEXT = 'Only this file is searched.'
const TRUNCATED_TEXT = 'Occurrence results are truncated: only the first 1,000 are shown.'
const EMPTY_TEXT = 'No occurrences found.'
const UNKNOWN_TEXT = 'Name occurrences are unavailable.'
const FALLBACK = 'Could not find the occurrences'
const MESSAGES = {
  not_bound: 'No binding for this name in this file.',
  unsupported: 'Occurrences are not supported for this name.',
  no_identifier: 'No name at this position.',
  syntax_error: 'Name occurrences are unavailable: this file could not be parsed as Python.',
  too_large: 'Name occurrences are unavailable: this file is too large to analyze (limit 256 KiB).',
  binary: 'Name occurrences are unavailable: this file is not text.',
  unsupported_language: 'Name occurrences are only available for Python files.',
}

// Server (source) order, with every count differing from the rows, so a recount would show.
const OCCURRENCES: Occurrence[] = [
  { line: 2, column: 5, kind: 'write' },
  { line: 4, column: 9, kind: 'write' },
  { line: 5, column: 12, kind: 'read' },
]
const ROW_TEXTS = ['Line 2, column 5: write', 'Line 4, column 9: write', 'Line 5, column 12: read']
const SUMMARY: OccurrencesSummary = { occurrences: 7, reads: 1, writes: 2, deletes: 3, bindings: 1 }
const SUMMARY_TEXT = '7 occurrences · 1 read · 2 writes · 3 deletes · 1 binding'
const ZERO: OccurrencesSummary = { occurrences: 0, reads: 0, writes: 0, deletes: 0, bindings: 0 }

function found(extra: Partial<CodeOccurrencesResult> = {}): CodeOccurrencesResult {
  return {
    status: 'found',
    reference: { path: 'app/main.py', line: 5, column: 12, name: 'acc' },
    scope: 'function',
    occurrences: OCCURRENCES,
    summary: SUMMARY,
    truncated: false,
    ...extra,
  }
}

function notFound(status: string): CodeOccurrencesResult {
  return {
    status: status as CodeOccurrencesResult['status'],
    reference: null,
    scope: null,
    occurrences: [],
    summary: ZERO,
    truncated: false,
  }
}

// The function parameter items, as the server reports it.
const ITEMS = found({
  reference: { path: 'app/main.py', line: 1, column: 11, name: 'items' },
  occurrences: [
    { line: 1, column: 11, kind: 'parameter' },
    { line: 3, column: 17, kind: 'read' },
  ],
  summary: { occurrences: 2, reads: 1, writes: 0, deletes: 0, bindings: 1 },
})

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

/** found() for the returned acc, ITEMS for the parameter items, not_bound anywhere else. */
const byPosition = (config: InternalAxiosRequestConfig): MockResponse => {
  const { line, column } = config.params as { line: number; column: number }
  if (line === 5 && column === 12) {
    return { status: 200, data: found() }
  }
  if (line === 1 && column === 11) {
    return { status: 200, data: ITEMS }
  }
  return { status: 200, data: notFound('not_bound') }
}

type Handler = MockResponse | ((config: InternalAxiosRequestConfig) => MockResponse)
type Params = Record<string, unknown>
type Navigate = (path: string, line: number) => void

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))
const codeText = () => document.querySelector('pre code')?.textContent
const region = () => screen.getByRole('region', { name: 'Name occurrences' })
const findButton = () => within(region()).getByRole('button', { name: /occurrences/ }) as HTMLButtonElement
const rowTexts = () => screen.queryAllByTestId('occurrence').map((row) => row.textContent)
const rowButtons = () => within(region()).queryAllByRole('button', { name: /^Line / }) as HTMLButtonElement[]
const summary = () => within(region()).findByTestId('occurrences-summary')
const byUrl = (requests: RecordedRequest[], url: string) => requests.filter((r) => r.url === url)

/** The text node and local offset at a UTF-16 offset of `root`'s text. */
function pointAt(root: Node, offset: number): [Node, number] {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  let remaining = offset
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const length = (node as Text).data.length
    if (remaining <= length) {
      return [node, remaining]
    }
    remaining -= length
  }
  throw new Error('offset outside the code')
}

/** A real browser selection (or caret) inside the displayed code, at UTF-16 offsets. */
function selectCode(start: number, end = start) {
  const pre = document.querySelector('pre') as HTMLPreElement
  const [startNode, startOffset] = pointAt(pre, start)
  const [endNode, endOffset] = pointAt(pre, end)
  document.getSelection()?.setBaseAndExtent(startNode, startOffset, endNode, endOffset)
}

/** Renders the panel alone, with a fixed selection offset into `content`. */
function renderPanel(
  handler: Handler = { status: 200, data: found() },
  props: {
    path?: string
    language?: string | null
    content?: string
    offset?: number | null
    onSelectLine?: Navigate
  } = {},
) {
  const seen: Params[] = []
  const requests = mockApi({
    [GET_OCCURRENCES]: (config) => {
      seen.push({ ...(config.params as Params) })
      return typeof handler === 'function' ? handler(config) : handler
    },
  })
  const client = createTestQueryClient()
  const getOffset = vi.fn(() => (props.offset === undefined ? ACC_OFFSET : props.offset))
  const ui = () => (
    <QueryClientProvider client={client}>
      <CodeOccurrences
        projectId={P}
        repositoryId={R}
        path={props.path ?? 'app/main.py'}
        language={props.language === undefined ? 'Python' : props.language}
        content={props.content ?? MAIN}
        getOffset={getOffset}
        onSelectLine={props.onSelectLine}
      />
    </QueryClientProvider>
  )
  const view = render(ui())
  return { seen, requests, getOffset, rerenderPanel: () => view.rerender(ui()), ...view }
}

/** Shows `data` (any JSON) in a bare panel after one explicit click. */
async function panelResult(data: unknown, props: Parameters<typeof renderPanel>[1] = {}) {
  const view = renderPanel({ status: 200, data }, props)
  fireEvent.click(findButton())
  await waitFor(() => expect(view.seen).toHaveLength(1))
  await waitFor(() => expect(findButton().textContent).toBe('Find occurrences'))
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

/** Opens `text` as `path` in the viewer, recording the params of every occurrences request. */
async function openFile(text: string, handler: Handler = byPosition, path = 'app/main.py') {
  const seen: Params[] = []
  const view = renderViewer(path, {
    [GET_CONTENT]: content(path, text),
    [GET_OCCURRENCES]: (config) => {
      seen.push({ ...(config.params as Params) })
      return typeof handler === 'function' ? handler(config) : handler
    },
  })
  await waitFor(() => expect(codeText()).toBe(text))
  return { seen, ...view }
}

/** Holds every occurrences request until released; other requests go through. */
function holdOccurrences() {
  const held: { params: Params; release: (data: CodeOccurrencesResult) => void }[] = []
  const realGet = apiClient.get.bind(apiClient)
  vi.spyOn(apiClient, 'get').mockImplementation(((url: string, config?: AxiosRequestConfig) => {
    if (url === OCCURRENCES_PATH) {
      return new Promise<unknown>((resolve) => {
        held.push({ params: { ...(config?.params as Params) }, release: (data) => resolve({ data }) })
      })
    }
    return realGet(url, config)
  }) as typeof apiClient.get)
  return held
}

afterEach(() => {
  document.getSelection()?.removeAllRanges()
  vi.restoreAllMocks()
})

// --- 1. Visibility ----------------------------------------------------------------------------

describe('Name occurrences visibility', () => {
  it('shows the panel for Python files and requests nothing on open or line selection', async () => {
    const { requests, rerenderPath } = renderViewer('app/main.py', { [GET_CONTENT]: contentRoute })
    await waitFor(() => expect(codeText()).toBe(MAIN))
    expect([findButton().textContent, findButton().disabled]).toEqual(['Find occurrences', false])
    rerenderPath('app/main.py', { line: 4 })
    await waitFor(() => expect(screen.getByTestId('selected-line').textContent).toBe('        acc += item'))
    await sleep(30)
    expect(byUrl(requests, OCCURRENCES_PATH)).toHaveLength(0)
  })

  it.each([['docs/notes.md'], ['app/script.js']])(
    'renders nothing and requests nothing for %s',
    async (path) => {
      const { requests } = renderViewer(path, { [GET_CONTENT]: contentRoute })
      await waitFor(() => expect(codeText()).toBe('# Notes\n'))
      expect(screen.queryByRole('region', { name: 'Name occurrences' })).toBeNull()
      await sleep(30)
      expect(byUrl(requests, OCCURRENCES_PATH)).toHaveLength(0)
    },
  )

  it.each([['Markdown'], [null], ['JavaScript']])(
    'renders nothing for language %s, even standalone',
    (language) => {
      const { requests, container } = renderPanel(undefined, { language })
      expect(container.innerHTML).toBe('')
      expect(requests).toHaveLength(0)
    },
  )
})

// --- 2. Selection -----------------------------------------------------------------------------

describe('Name occurrences selection', () => {
  it('needs a caret inside the code and requests nothing without one', async () => {
    const { seen } = await openFile(MAIN)
    fireEvent.click(findButton())
    expect(within(region()).getByText(NO_POSITION_TEXT)).toBeTruthy()
    // A selection outside the code (the file header) is not a position either.
    const header = screen.getByTestId('file-header').firstChild?.firstChild as Node
    document.getSelection()?.setBaseAndExtent(header, 1, header, 1)
    fireEvent.click(findButton())
    await sleep(50)
    expect(seen).toHaveLength(0)
    selectCode(ACC_OFFSET)
    fireEvent.click(findButton())
    await summary()
    expect(within(region()).queryByText(NO_POSITION_TEXT)).toBeNull()
    expect(seen).toEqual([{ path: 'app/main.py', line: 5, column: 12 }])
  })

  it.each([
    ['the returned name', MAIN, 'return acc', 7, { line: 5, column: 12 }],
    ['a parameter', MAIN, 'items)', 0, { line: 1, column: 11 }],
    ['after astral characters', "s = '😀😀'; acc = 1\nprint(acc)\n", 'acc', 0, { line: 1, column: 11 }],
    ['after a tab', 'def f():\n\tacc = 1\n\treturn acc\n', 'return acc', 7, { line: 3, column: 9 }],
    ['after CRLF line breaks', 'x = 1\r\nacc = 2\r\nprint(acc)\r\n', 'print(acc)', 6, { line: 3, column: 7 }],
    ['after CR line breaks', 'x = 1\racc = 2\rprint(acc)\r', 'print(acc)', 6, { line: 3, column: 7 }],
  ])('sends exactly the path, line and column of a caret on %s', async (_label, text, needle, shift, position) => {
    const { seen } = await openFile(text, { status: 200, data: notFound('not_bound') })
    selectCode(text.indexOf(needle) + shift)
    fireEvent.click(findButton())
    await within(region()).findByText(MESSAGES.not_bound)
    expect(seen).toEqual([{ path: 'app/main.py', ...position }])
  })

  it('uses the start of a selected range', async () => {
    const { seen } = await openFile(MAIN, { status: 200, data: notFound('not_bound') })
    selectCode(ACC_OFFSET, ACC_OFFSET + 3)
    fireEvent.click(findButton())
    await within(region()).findByText(MESSAGES.not_bound)
    expect(seen).toEqual([{ path: 'app/main.py', line: 5, column: 12 }])
  })

  it('keeps the code selection when the button is pressed', async () => {
    await openFile(MAIN)
    selectCode(ACC_OFFSET)
    expect(fireEvent.mouseDown(findButton())).toBe(false) // default prevented
  })

  it('reads the position only when clicked', () => {
    const { getOffset, rerenderPanel } = renderPanel()
    rerenderPanel()
    expect(getOffset).not.toHaveBeenCalled()
    fireEvent.click(findButton())
    expect(getOffset).toHaveBeenCalledTimes(1)
  })
})

// --- 3. Requests ------------------------------------------------------------------------------

describe('Name occurrences requests', () => {
  it('sends exactly one GET to the occurrences endpoint with exactly path, line and column', async () => {
    const { seen, requests } = await panelResult(found())
    expect(requests.map((r) => [r.method, r.url, r.baseURL, r.data])).toEqual([
      ['GET', OCCURRENCES_PATH, '/api/v1', undefined],
    ])
    expect(seen).toEqual([{ path: 'app/main.py', line: 5, column: 12 }])
    expect(Object.keys(seen[0]).sort()).toEqual(['column', 'line', 'path'])
  })

  it('sends an unusual path unchanged', async () => {
    const { seen } = await panelResult(found(), { path: 'docs/with space/a#b?.py' })
    expect(seen[0].path).toBe('docs/with space/a#b?.py')
  })

  it('shows the pending label, disables the button and sends nothing more meanwhile', async () => {
    const held = holdOccurrences()
    renderPanel()
    fireEvent.click(findButton())
    await waitFor(() => expect(findButton().textContent).toBe('Finding occurrences…'))
    expect(findButton().disabled).toBe(true)
    fireEvent.click(findButton())
    await sleep(20)
    expect(held).toHaveLength(1)
    held[0].release(found())
    await summary()
    expect([findButton().textContent, findButton().disabled]).toEqual(['Find occurrences', false])
  })

  it('requests afresh on every click, even for the same position', async () => {
    let answers = 0
    const { seen } = renderPanel(() => ({
      status: 200,
      data: found({ summary: { ...SUMMARY, occurrences: ++answers } }),
    }))
    fireEvent.click(findButton())
    await waitFor(() => expect(within(region()).getByTestId('occurrences-summary').textContent).toMatch(/^1 occurrence /))
    fireEvent.click(findButton())
    await waitFor(() => expect(within(region()).getByTestId('occurrences-summary').textContent).toMatch(/^2 occurrences /))
    expect(seen).toEqual([
      { path: 'app/main.py', line: 5, column: 12 },
      { path: 'app/main.py', line: 5, column: 12 },
    ])
  })

  it('never requests on mount or on re-render', async () => {
    const { requests, rerenderPanel } = renderPanel()
    rerenderPanel()
    rerenderPanel()
    await sleep(30)
    expect(requests).toHaveLength(0)
  })

  it('does not retry a failed request', async () => {
    const { seen } = renderPanel({ status: 500, data: { detail: 'boom' } })
    fireEvent.click(findButton())
    await within(region()).findByRole('alert')
    await sleep(50)
    expect(seen).toHaveLength(1)
  })
})

// --- 4. Results -------------------------------------------------------------------------------

describe('Name occurrences results', () => {
  it('shows the subject, the server summary and every row in server order', async () => {
    await panelResult(found(), { onSelectLine: vi.fn<Navigate>() })
    expect(within(region()).getByTestId('occurrences-subject').textContent).toBe(
      'Occurrences of acc (function scope)',
    )
    expect(within(region()).getByTestId('occurrences-summary').textContent).toBe(SUMMARY_TEXT)
    expect(rowTexts()).toEqual(ROW_TEXTS)
    expect(rowButtons().map((b) => b.textContent)).toEqual(ROW_TEXTS)
    expect(within(region()).queryByText(MODULE_TEXT)).toBeNull()
  })

  it('shows the server summary even when it differs from the rows', async () => {
    await panelResult(found({ occurrences: [OCCURRENCES[0]] }))
    expect(rowTexts()).toEqual([ROW_TEXTS[0]])
    expect(within(region()).getByTestId('occurrences-summary').textContent).toBe(SUMMARY_TEXT)
  })

  it.each([
    [true, true],
    [false, false],
  ])('truncated %s: the notice is shown only when the server says so', async (truncated, shown) => {
    await panelResult(found({ truncated }))
    expect(within(region()).queryByText(TRUNCATED_TEXT) !== null).toBe(shown)
    expect(rowTexts()).toEqual(ROW_TEXTS) // never inferred from the row count
  })

  it('keeps duplicates and the exact server order', async () => {
    const rows: Occurrence[] = [
      { line: 9, column: 1, kind: 'read' },
      { line: 2, column: 5, kind: 'write' },
      { line: 9, column: 1, kind: 'read' },
      { line: 4, column: 3, kind: 'delete' },
    ]
    await panelResult(found({ occurrences: rows }))
    expect(rowTexts()).toEqual([
      'Line 9, column 1: read',
      'Line 2, column 5: write',
      'Line 9, column 1: read',
      'Line 4, column 3: delete',
    ])
  })

  it('labels every kind as the server names it', async () => {
    const kinds: OccurrenceKind[] = ['read', 'write', 'delete', 'parameter', 'definition']
    await panelResult(found({ occurrences: kinds.map((kind, i) => ({ line: i + 1, column: 2, kind })) }))
    expect(rowTexts()).toEqual(kinds.map((kind, i) => `Line ${i + 1}, column 2: ${kind}`))
  })

  it('notes that only this file is searched for module-scope names', async () => {
    await panelResult(found({ scope: 'module' }))
    expect(within(region()).getByTestId('occurrences-subject').textContent).toBe(
      'Occurrences of acc (module scope)',
    )
    expect(within(region()).getByText(MODULE_TEXT)).toBeTruthy()
  })

  it.each([['class'], ['lambda'], ['comprehension']])('names the %s scope', async (scope) => {
    await panelResult(found({ scope: scope as OccurrenceScope }))
    expect(within(region()).getByTestId('occurrences-subject').textContent).toBe(
      `Occurrences of acc (${scope} scope)`,
    )
    expect(within(region()).queryByText(MODULE_TEXT)).toBeNull()
  })

  it('shows an empty note for a found answer without rows', async () => {
    await panelResult(found({ occurrences: [] }))
    expect(within(region()).getByText(EMPTY_TEXT)).toBeTruthy()
    expect(rowTexts()).toEqual([])
  })

  it('writes counts of one in the singular and every other count in the plural', () => {
    expect(summaryText({ occurrences: 1, reads: 1, writes: 1, deletes: 1, bindings: 1 })).toBe(
      '1 occurrence · 1 read · 1 write · 1 delete · 1 binding',
    )
    expect(summaryText(ZERO)).toBe('0 occurrences · 0 reads · 0 writes · 0 deletes · 0 bindings')
    expect(occurrenceLabel({ line: 3, column: 7, kind: 'parameter' })).toBe('Line 3, column 7: parameter')
  })
})

// --- 5. Statuses ------------------------------------------------------------------------------

describe('Name occurrences statuses', () => {
  it.each(Object.entries(MESSAGES))('shows the %s message and nothing else', async (status, message) => {
    await panelResult(notFound(status))
    expect(within(region()).getByText(message)).toBeTruthy()
    expect(within(region()).queryByTestId('occurrences-summary')).toBeNull()
    expect(within(region()).queryByTestId('occurrences-subject')).toBeNull()
    expect(rowTexts()).toEqual([])
  })

  it('shows a safe fallback for an unknown status', async () => {
    await panelResult(notFound('mystery'))
    expect(within(region()).getByText(UNKNOWN_TEXT)).toBeTruthy()
    expect(within(region()).queryByTestId('occurrences-summary')).toBeNull()
  })

  it('uses the approved wording', () => {
    expect(STATUS_MESSAGES).toEqual(MESSAGES)
    expect([NO_POSITION, MODULE_NOTE, TRUNCATED_NOTICE, EMPTY_RESULT, UNKNOWN_STATUS]).toEqual([
      NO_POSITION_TEXT,
      MODULE_TEXT,
      TRUNCATED_TEXT,
      EMPTY_TEXT,
      UNKNOWN_TEXT,
    ])
  })
})

// --- 6. Errors --------------------------------------------------------------------------------

describe('Name occurrences errors', () => {
  it.each([
    [404, 'File not found'],
    [409, CHANGED],
    [429, 'Too many attempts; please try again later'],
  ])('shows the exact %s detail, after one request', async (status, detail) => {
    const { seen } = renderPanel({ status, data: { detail } })
    fireEvent.click(findButton())
    expect((await within(region()).findByRole('alert')).textContent).toBe(detail)
    expect(findButton().textContent).toBe('Find occurrences')
    expect(seen).toHaveLength(1)
  })

  it('shows the fallback for a server error without detail', async () => {
    renderPanel({ status: 500, data: {} })
    fireEvent.click(findButton())
    expect((await within(region()).findByRole('alert')).textContent).toBe(FALLBACK)
  })

  it('shows a network error without leaking the error object', async () => {
    vi.spyOn(apiClient, 'get').mockRejectedValue(new AxiosError('Network Error', 'ERR_NETWORK'))
    renderPanel()
    fireEvent.click(findButton())
    expect((await within(region()).findByRole('alert')).textContent).toBe('Could not reach the server')
    expect(region().textContent).not.toContain('ERR_NETWORK')
  })
})

// --- 7. Navigation ----------------------------------------------------------------------------

describe('Name occurrences navigation', () => {
  it("opens rows with the server's reference path and each occurrence's line", async () => {
    const onSelectLine = vi.fn<Navigate>()
    await panelResult(
      found({ reference: { path: 'server/actual.py', line: 5, column: 12, name: 'acc' } }),
      { path: 'requested/asked.py', onSelectLine },
    )
    fireEvent.click(rowButtons()[1])
    fireEvent.click(rowButtons()[0])
    expect(onSelectLine.mock.calls).toEqual([
      ['server/actual.py', 4],
      ['server/actual.py', 2],
    ])
  })

  it('navigates duplicate rows independently', async () => {
    const onSelectLine = vi.fn<Navigate>()
    const rows: Occurrence[] = [
      { line: 6, column: 2, kind: 'read' },
      { line: 6, column: 2, kind: 'read' },
    ]
    await panelResult(found({ occurrences: rows }), { onSelectLine })
    fireEvent.click(rowButtons()[1])
    fireEvent.click(rowButtons()[0])
    expect(onSelectLine.mock.calls).toEqual([
      ['app/main.py', 6],
      ['app/main.py', 6],
    ])
  })

  it('shows plain text rows without onSelectLine', async () => {
    await panelResult(found())
    expect(rowTexts()).toEqual(ROW_TEXTS)
    expect(rowButtons()).toHaveLength(0)
  })

  it('shows plain text rows when the server reports no reference', async () => {
    await panelResult(found({ reference: null }), { onSelectLine: vi.fn<Navigate>() })
    expect(rowTexts()).toEqual(ROW_TEXTS)
    expect(rowButtons()).toHaveLength(0)
    expect(within(region()).queryByTestId('occurrences-subject')).toBeNull()
  })

  it('in the viewer, a row highlights its line while the results stay, with no new request', async () => {
    const { seen, onSelectLine, rerenderPath } = await openFile(MAIN)
    selectCode(ACC_OFFSET)
    fireEvent.click(findButton())
    await summary()
    fireEvent.click(within(region()).getByRole('button', { name: 'Line 4, column 9: write' }))
    expect(onSelectLine.mock.calls).toEqual([['app/main.py', 4]])
    rerenderPath('app/main.py', { line: 4 }) // what the repository page does with the selection
    await waitFor(() => expect(screen.getByTestId('selected-line').textContent).toBe('        acc += item'))
    expect(rowTexts()).toEqual(ROW_TEXTS)
    await sleep(30)
    expect(seen).toHaveLength(1)
  })
})

// --- 8. Stale state ---------------------------------------------------------------------------

describe('Name occurrences never show a stale result', () => {
  /** Loads app/other.py and then app/main.py, so switching between them shows no loading state. */
  async function openBothFiles(routes: Record<string, Handler> = {}) {
    const view = renderViewer('app/other.py', {
      [GET_CONTENT]: contentRoute,
      [GET_OCCURRENCES]: byPosition,
      ...routes,
    })
    await waitFor(() => expect(codeText()).toBe(OTHER))
    view.rerenderPath('app/main.py')
    await waitFor(() => expect(codeText()).toBe(MAIN))
    return view
  }

  it('never carries a result into another already-loaded file, nor back', async () => {
    const { requests, rerenderPath } = await openBothFiles()
    selectCode(ACC_OFFSET)
    fireEvent.click(findButton())
    await summary()
    expect(rowTexts()).toEqual(ROW_TEXTS)
    rerenderPath('app/other.py')
    expect(codeText()).toBe(OTHER) // shown at once: the panel stays mounted unless keyed by path
    expect([findButton().textContent, findButton().disabled, rowTexts()]).toEqual(['Find occurrences', false, []])
    expect(within(region()).queryByTestId('occurrences-summary')).toBeNull()
    rerenderPath('app/main.py')
    expect(codeText()).toBe(MAIN)
    expect(rowTexts()).toEqual([])
    await sleep(30)
    expect(byUrl(requests, OCCURRENCES_PATH)).toHaveLength(1)
  })

  it('never shows a pending request’s result in another already-loaded file', async () => {
    const held = holdOccurrences()
    const { rerenderPath } = await openBothFiles()
    selectCode(ACC_OFFSET)
    fireEvent.click(findButton())
    await waitFor(() => expect(findButton().textContent).toBe('Finding occurrences…'))
    rerenderPath('app/other.py')
    expect(codeText()).toBe(OTHER)
    expect([findButton().textContent, findButton().disabled]).toEqual(['Find occurrences', false])
    held[0].release(found())
    await sleep(50)
    expect(rowTexts()).toEqual([])
    expect(within(region()).queryByTestId('occurrences-summary')).toBeNull()
  })

  it('never carries an error into another already-loaded file', async () => {
    const { rerenderPath } = await openBothFiles({
      [GET_OCCURRENCES]: { status: 409, data: { detail: CHANGED } },
    })
    selectCode(ACC_OFFSET)
    fireEvent.click(findButton())
    expect((await within(region()).findByRole('alert')).textContent).toBe(CHANGED)
    rerenderPath('app/other.py')
    expect(codeText()).toBe(OTHER)
    expect(within(region()).queryByRole('alert')).toBeNull()
  })

  it('replaces the first lookup with a second one', async () => {
    const { seen } = await openFile(MAIN)
    selectCode(ACC_OFFSET)
    fireEvent.click(findButton())
    await summary()
    expect(rowTexts()).toEqual(ROW_TEXTS)
    selectCode(MAIN.indexOf('items)'))
    fireEvent.click(findButton())
    await waitFor(() =>
      expect(within(region()).getByTestId('occurrences-subject').textContent).toBe(
        'Occurrences of items (function scope)',
      ),
    )
    expect(rowTexts()).toEqual(['Line 1, column 11: parameter', 'Line 3, column 17: read'])
    expect(within(region()).getByTestId('occurrences-summary').textContent).toBe(
      '2 occurrences · 1 read · 0 writes · 0 deletes · 1 binding',
    )
    expect(seen).toEqual([
      { path: 'app/main.py', line: 5, column: 12 },
      { path: 'app/main.py', line: 1, column: 11 },
    ])
  })
})

// --- 9. Security ------------------------------------------------------------------------------

describe('Name occurrences security', () => {
  it('renders hostile names, scopes and kinds as literal text', async () => {
    await panelResult(
      found({
        reference: { path: 'app/main.py', line: 1, column: 1, name: HTML },
        scope: '<b>bold</b>' as OccurrenceScope,
        occurrences: [{ line: 1, column: 1, kind: '<script>x</script>' as OccurrenceKind }],
      }),
      { onSelectLine: vi.fn<Navigate>() },
    )
    const subject = within(region()).getByTestId('occurrences-subject')
    expect(subject.textContent).toBe(`Occurrences of ${HTML} (<b>bold</b> scope)`)
    expect(subject.innerHTML).toContain('&lt;img src=x onerror=alert(1)&gt;')
    expect(rowTexts()).toEqual(['Line 1, column 1: <script>x</script>'])
    expect(document.querySelector('img, b, script, iframe')).toBeNull()
    expect(document.querySelector('[src], [onerror]')).toBeNull()
  })

  it('never renders unknown response fields', async () => {
    const extra = { source: 'FAKE-SOURCE-MARKER', sha256: 'FAKE-SHA-MARKER', id: 'FAKE-ID-MARKER' }
    await panelResult(
      {
        ...found(),
        ...extra,
        reference: { ...found().reference, ...extra },
        summary: { ...SUMMARY, ...extra },
        occurrences: OCCURRENCES.map((o) => ({ ...o, ...extra })),
      },
      { onSelectLine: vi.fn<Navigate>() },
    )
    const text = document.body.textContent ?? ''
    for (const marker of Object.values(extra)) {
      expect(text).not.toContain(marker)
    }
    for (const button of screen.getAllByRole('button')) {
      expect(button.getAttribute('aria-label') ?? '').not.toMatch(/FAKE/)
    }
  })

  it('uses no browser storage, cookies or direct fetch, only the app API client', async () => {
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
      const { requests } = await panelResult(found(), { onSelectLine: vi.fn<Navigate>() })
      fireEvent.click(rowButtons()[0])
      expect(new Set(getItem.mock.calls.map((call) => call[0]))).toEqual(new Set(['ai_se_access_token']))
      for (const spy of writes) {
        expect(spy).not.toHaveBeenCalled()
      }
      expect([localStorage.length, sessionStorage.length]).toEqual(stored)
      expect(cookieGet).not.toHaveBeenCalled()
      expect(cookieSet).not.toHaveBeenCalled()
      expect(fetchSpy).not.toHaveBeenCalled()
      expect(requests.map((r) => `${r.method} ${r.url}`)).toEqual([GET_OCCURRENCES])
      expect(requests.every((r) => r.baseURL === '/api/v1')).toBe(true)
    } finally {
      globalThis.fetch = realFetch
      delete (document as { cookie?: unknown }).cookie
    }
  })

  it('renders repeated positions without React key warnings', async () => {
    const consoleError = vi.spyOn(console, 'error')
    const rows: Occurrence[] = [
      { line: 2, column: 1, kind: 'write' },
      { line: 2, column: 1, kind: 'write' },
      { line: 7, column: 1, kind: 'read' },
    ]
    const { getOffset } = await panelResult(found({ occurrences: rows }))
    fireEvent.click(findButton()) // a fresh lookup re-renders the same rows
    await waitFor(() => expect(getOffset).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(findButton().disabled).toBe(false))
    expect(rowTexts()).toEqual(['Line 2, column 1: write', 'Line 2, column 1: write', 'Line 7, column 1: read'])
    const messages = consoleError.mock.calls.map((call) => call.map(String).join(' '))
    expect(messages.filter((m) => /same key|unique "key"/.test(m))).toEqual([])
  })

  it('keeps the default labels clear of the frozen test patterns and of file paths', async () => {
    renderViewer('app/main.py', { [GET_CONTENT]: contentRoute })
    await waitFor(() => expect(codeText()).toBe(MAIN))
    const names = within(region())
      .getAllByRole('button')
      .map((b) => b.textContent ?? '')
    expect(names).toEqual(['Find occurrences'])
    for (const pattern of [/definition/i, /outline/i, /^Go to/, /^Open/, /^Find references/, /in the viewer/, /main\.py/]) {
      expect(names.some((name) => pattern.test(name))).toBe(false)
      expect(pattern.test('Name occurrences')).toBe(false)
    }
    // The frozen go-to-definition helper (/definition/i) still finds exactly one button.
    expect(screen.getAllByRole('button', { name: /definition/i }).map((b) => b.textContent)).toEqual([
      'Go to definition',
    ])
  })
})

// --- 10. FileViewer integration ----------------------------------------------------------------

describe('Name occurrences in the file viewer', () => {
  it('places the panel right after go-to-definition, keeping every other panel', async () => {
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
    const definition = screen.getByRole('region', { name: 'Go to definition' })
    expect(definition.nextElementSibling).toBe(region())
    expect(definition.contains(region())).toBe(false)
  })

  it('shares the caret with go-to-definition: both send the same position', async () => {
    const positions: Params[] = []
    const record = (config: InternalAxiosRequestConfig): MockResponse => {
      positions.push({ url: config.url, ...(config.params as Params) })
      return config.url?.endsWith('/definition')
        ? {
            status: 200,
            data: { status: 'not_found', reference: null, definitions: [], truncated: false },
          }
        : { status: 200, data: notFound('not_bound') }
    }
    renderViewer('app/main.py', {
      [GET_CONTENT]: contentRoute,
      [GET_DEFINITION]: record,
      [GET_OCCURRENCES]: record,
    })
    await waitFor(() => expect(codeText()).toBe(MAIN))
    selectCode(ACC_OFFSET)
    fireEvent.click(screen.getByRole('button', { name: 'Go to definition' }))
    fireEvent.click(findButton())
    await within(region()).findByText(MESSAGES.not_bound)
    await waitFor(() => expect(positions).toHaveLength(2))
    expect(positions).toEqual([
      { url: `${BASE}/files/definition`, path: 'app/main.py', line: 5, column: 12 },
      { url: OCCURRENCES_PATH, path: 'app/main.py', line: 5, column: 12 },
    ])
  })
})
