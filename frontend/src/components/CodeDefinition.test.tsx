import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { AxiosError, type InternalAxiosRequestConfig } from 'axios'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { RepositoryPage } from '../pages/RepositoryPage'
import { apiClient } from '../services/client'
import { mockApi, type MockResponse, PROJECT, type RecordedRequest, ZIP_REPOSITORY } from '../test/mockApi'
import { createTestQueryClient } from '../test/render'
import type {
  CodeDefinitionResult,
  DefinitionLocation,
  FileContent,
  TreeDirectory,
} from '../types/api'
import {
  CodeDefinition,
  NO_POSITION,
  positionAt,
  STATUS_MESSAGES,
  TRUNCATED_NOTICE,
  UNKNOWN_STATUS,
} from './CodeDefinition'
import { FileViewer, type LineSelection } from './FileViewer'

const P = PROJECT.id
const R = ZIP_REPOSITORY.id
const BASE = `/projects/${P}/repositories/${R}`
const DEFINITION_PATH = `${BASE}/files/definition`
const GET_DEFINITION = `GET ${DEFINITION_PATH}`
const GET_CONTENT = `GET ${BASE}/files/content`
const HTML = '<img src=x onerror=alert(1)>'

const MAIN = 'import tools.utils\n\nresult = compute(1)\nvalue = tools.utils.helper()\n'

const COMPUTE: DefinitionLocation = { path: 'app/calc.py', kind: 'function', name: 'compute', line: 7, end_line: 12 }
// Server order, deliberately not alphabetical by path, name or line.
const CANDIDATES: DefinitionLocation[] = [
  { path: 'zeta/impl.py', kind: 'class', name: 'Runner', line: 30, end_line: 40 },
  { path: 'alpha/impl.py', kind: 'function', name: 'Runner', line: 5, end_line: 9 },
  { path: 'alpha/impl.py', kind: 'method', name: 'Runner', line: 2, end_line: 3 },
]

function result(extra: Partial<CodeDefinitionResult> = {}): CodeDefinitionResult {
  return {
    status: 'resolved',
    reference: { path: 'app/main.py', line: 3, column: 10, name: 'compute' },
    definitions: [COMPUTE],
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
type Params = { path: string; line: number; column: number }

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))
const codeText = () => document.querySelector('pre code')?.textContent
const definitionButton = () =>
  within(screen.getByRole('region', { name: 'Go to definition' })).getByRole('button', {
    name: /definition/i,
  }) as HTMLButtonElement
const definitionList = () => screen.findByRole('list', { name: 'Definitions' })
const definitionRequests = (requests: RecordedRequest[]) => requests.filter((r) => r.url === DEFINITION_PATH)

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

/** A real browser selection (or caret) inside the displayed code. */
function selectCode(start: number, end = start) {
  const pre = document.querySelector('pre') as HTMLPreElement
  const [startNode, startOffset] = pointAt(pre, start)
  const [endNode, endOffset] = pointAt(pre, end)
  document.getSelection()?.setBaseAndExtent(startNode, startOffset, endNode, endOffset)
}

/** Caret on the n-th character (0-based) of the first `needle` in the displayed code. */
function caretOn(text: string, needle: string, nth = 0) {
  const index = text.indexOf(needle)
  expect(index).toBeGreaterThanOrEqual(0)
  selectCode(index + nth)
}

function renderViewer(
  path: string,
  routes: Record<string, Handler>,
  options: { selectedLine?: LineSelection | null } = {},
) {
  const requests = mockApi(routes)
  const client = createTestQueryClient()
  const onSelectLine = vi.fn<(path: string, line: number) => void>()
  const ui = (selectedPath: string) => (
    <QueryClientProvider client={client}>
      <FileViewer
        projectId={P}
        repositoryId={R}
        selectedPath={selectedPath}
        selectedLine={options.selectedLine}
        onSelectLine={onSelectLine}
        onSelectFile={vi.fn()}
      />
    </QueryClientProvider>
  )
  const view = render(ui(path))
  return { requests, onSelectLine, rerenderPath: (next: string) => view.rerender(ui(next)), ...view }
}

/** Opens `text` as app/main.py and records the params of every definition request. */
async function openFile(text: string, route: Handler = { status: 200, data: result() }, path = 'app/main.py') {
  const seen: Params[] = []
  const view = renderViewer(path, {
    [GET_CONTENT]: content(path, text),
    [GET_DEFINITION]: (config) => {
      seen.push({ ...(config.params as Params) })
      return typeof route === 'function' ? route(config) : route
    },
  })
  await waitFor(() => expect(codeText()).toBe(text))
  return { seen, ...view }
}

/** Places the caret, clicks Go to definition and returns the request's params. */
async function positionFor(text: string, needle: string, nth = 0) {
  const { seen } = await openFile(text)
  caretOn(text, needle, nth)
  fireEvent.click(definitionButton())
  await definitionList()
  expect(seen).toHaveLength(1)
  return seen[0]
}

afterEach(() => {
  document.getSelection()?.removeAllRanges()
})

// --- Position conversion --------------------------------------------------------------------

describe('positionAt', () => {
  it.each([
    ['ASCII first character', 'x = compute(1)', 4, { line: 1, column: 5 }],
    ['start of the file', 'compute', 0, { line: 1, column: 1 }],
    ['second line', 'a\nb = c', 6, { line: 2, column: 5 }],
    ['BMP characters count once', 's = "é中"; t', 10, { line: 1, column: 11 }],
    ['astral characters count once', 'x = "😀𝒳"; t', 12, { line: 1, column: 11 }],
    ['a tab counts as one', '\t\tvalue', 2, { line: 1, column: 3 }],
    ['CRLF is one line break', 'a = 1\r\nb = c\r\n', 11, { line: 2, column: 5 }],
    ['a lone CR is a line break', 'a = 1\rb = c', 10, { line: 2, column: 5 }],
    ['an empty line', 'a\n\nb', 2, { line: 2, column: 1 }],
    ['no NFKC normalization', 'ﬁ = 1; x', 7, { line: 1, column: 8 }],
  ])('%s', (_name, text, offset, expected) => {
    expect(positionAt(text, offset)).toEqual(expected)
  })
})

// --- Visibility and requests ----------------------------------------------------------------

describe('CodeDefinition visibility and requests', () => {
  it('requests nothing on mount, on selection or before the click, then one GET', async () => {
    const { seen, requests } = await openFile(MAIN)
    expect(definitionButton().textContent).toBe('Go to definition')
    caretOn(MAIN, 'compute')
    fireEvent.mouseUp(document.querySelector('pre') as HTMLPreElement)
    fireEvent.keyUp(document.querySelector('pre') as HTMLPreElement, { key: 'ArrowRight' })
    await sleep(50)
    expect(definitionRequests(requests)).toHaveLength(0)

    fireEvent.click(definitionButton())
    await definitionList()
    expect(definitionRequests(requests)).toHaveLength(1)
    expect(definitionRequests(requests)[0].method).toBe('GET')
    expect(definitionRequests(requests)[0].data).toBeUndefined()
    // Exactly the path and the position; no symbol name, source text or other parameter.
    expect(seen).toEqual([{ path: 'app/main.py', line: 3, column: 10 }])
  })

  it.each([
    ['README.md', '# readme\n', {}],
    ['app/blob.py', null, { reason: 'binary' as const }],
    ['app/huge.py', null, { reason: 'too_large' as const }],
    ['app/empty.py', '', {}],
  ])('shows nothing for %s', async (path, text, extra) => {
    const { requests } = renderViewer(path, { [GET_CONTENT]: content(path, text, extra) })
    await screen.findByTestId('file-header')
    expect(screen.queryByRole('region', { name: 'Go to definition' })).toBeNull()
    expect(definitionRequests(requests)).toHaveLength(0)
  })

  it('needs a selection inside the code and sends nothing without one', async () => {
    const { requests } = await openFile(MAIN)
    fireEvent.click(definitionButton())
    expect(screen.getByText(NO_POSITION)).toBeTruthy()
    // A selection outside the code (the file header) is not a position either.
    const header = screen.getByTestId('file-header').firstChild?.firstChild as Node
    document.getSelection()?.setBaseAndExtent(header, 1, header, 1)
    fireEvent.click(definitionButton())
    await sleep(50)
    expect(definitionRequests(requests)).toHaveLength(0)

    caretOn(MAIN, 'compute')
    fireEvent.click(definitionButton())
    await definitionList()
    expect(screen.queryByText(NO_POSITION)).toBeNull()
    expect(definitionRequests(requests)).toHaveLength(1)
  })

  it('keeps the code selection when the button is pressed', async () => {
    await openFile(MAIN)
    caretOn(MAIN, 'compute')
    const press = fireEvent.mouseDown(definitionButton())
    expect(press).toBe(false) // default prevented: focus and selection stay in the code
  })

  it('sends the server path exactly, without trimming or normalizing it', async () => {
    const seen: Params[] = []
    mockApi({
      [GET_DEFINITION]: (config) => {
        seen.push({ ...(config.params as Params) })
        return { status: 200, data: result() }
      },
    })
    const path = ' app/My Dir/../x%20y.py '
    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <CodeDefinition
          projectId={P}
          repositoryId={R}
          path={path}
          language="Python"
          content={'a = 1\nb = a\n'}
          getOffset={() => 10}
        />
      </QueryClientProvider>,
    )
    fireEvent.click(definitionButton())
    await definitionList()
    expect(seen).toEqual([{ path, line: 2, column: 5 }])
  })

  it('shows a pending label and disables the button while loading', async () => {
    let finish: () => void = () => {}
    const realGet = apiClient.get.bind(apiClient)
    const get = vi.spyOn(apiClient, 'get').mockImplementation(((url: string, config?: object) =>
      url === DEFINITION_PATH
        ? new Promise<unknown>((resolve) => (finish = () => resolve({ data: result() })))
        : realGet(url, config)) as typeof apiClient.get)
    try {
      await openFile(MAIN)
      caretOn(MAIN, 'compute')
      fireEvent.click(definitionButton())
      await waitFor(() => expect(definitionButton().textContent).toBe('Finding definition…'))
      expect(definitionButton().disabled).toBe(true)
      finish()
      await definitionList()
      expect(definitionButton().textContent).toBe('Go to definition')
    } finally {
      get.mockRestore()
    }
  })
})

// --- Position from the real selection --------------------------------------------------------

describe('CodeDefinition position', () => {
  it.each([
    ['first', 0, 10],
    ['middle', 3, 13],
    ['final', 6, 16],
  ])('ASCII: the %s character of the identifier', async (_name, nth, column) => {
    expect(await positionFor(MAIN, 'compute', nth)).toEqual({ path: 'app/main.py', line: 3, column })
  })

  it('uses the start of a selected range', async () => {
    const { seen } = await openFile(MAIN)
    const index = MAIN.indexOf('helper')
    selectCode(index + 1, index + 5)
    fireEvent.click(definitionButton())
    await definitionList()
    expect(seen).toEqual([{ path: 'app/main.py', line: 4, column: 22 }])
  })

  it('uses the start of a backwards selection', async () => {
    const { seen } = await openFile(MAIN)
    const index = MAIN.indexOf('helper')
    const pre = document.querySelector('pre') as HTMLPreElement
    const [node, offset] = pointAt(pre, index + 6)
    const [startNode, startOffset] = pointAt(pre, index)
    document.getSelection()?.setBaseAndExtent(node, offset, startNode, startOffset)
    fireEvent.click(definitionButton())
    await definitionList()
    expect(seen).toEqual([{ path: 'app/main.py', line: 4, column: 21 }])
  })

  it('counts BMP characters as code points', async () => {
    const text = 'label = "é中"; target()\n'
    expect(await positionFor(text, 'target')).toEqual({ path: 'app/main.py', line: 1, column: 15 })
  })

  it.each([
    ['first', 0, 11],
    ['final', 5, 16],
  ])('counts astral characters once (the %s character)', async (_name, nth, column) => {
    const text = '"𝒳"\nx = "😀𝒳"; target()\n'
    expect(await positionFor(text, 'target', nth)).toEqual({ path: 'app/main.py', line: 2, column })
  })

  it('counts a tab as one character', async () => {
    const text = 'def run():\n\tvalue = helper()\n'
    expect(await positionFor(text, 'helper')).toEqual({ path: 'app/main.py', line: 2, column: 10 })
  })

  it('treats CRLF as one line break', async () => {
    const text = 'a = 1\r\nb = 2\r\nc = helper()\r\n'
    expect(await positionFor(text, 'helper', 5)).toEqual({ path: 'app/main.py', line: 3, column: 10 })
  })

  it('uses the whole text while a line is highlighted', async () => {
    const seen: Params[] = []
    renderViewer(
      'app/main.py',
      {
        [GET_CONTENT]: content('app/main.py', MAIN),
        [GET_DEFINITION]: (config) => {
          seen.push({ ...(config.params as Params) })
          return { status: 200, data: result() }
        },
      },
      { selectedLine: { line: 3 } },
    )
    await screen.findByTestId('selected-line')
    caretOn(MAIN, 'helper', 2) // after the highlighted line
    fireEvent.click(definitionButton())
    await definitionList()
    caretOn(MAIN, 'compute', 1) // inside the highlighted line
    fireEvent.click(definitionButton())
    await waitFor(() => expect(seen).toHaveLength(2))
    expect(seen).toEqual([
      { path: 'app/main.py', line: 4, column: 23 },
      { path: 'app/main.py', line: 3, column: 11 },
    ])
  })
})

// --- Results ---------------------------------------------------------------------------------

async function showResult(route: Handler) {
  const view = await openFile(MAIN, route)
  caretOn(MAIN, 'compute')
  fireEvent.click(definitionButton())
  return view
}

const definitionTexts = () => screen.getAllByTestId('definition').map((row) => row.textContent)

describe('CodeDefinition results', () => {
  it('shows a resolved definition and navigates to its exact path and start line', async () => {
    const { onSelectLine } = await showResult({ status: 200, data: result() })
    await definitionList()
    expect(definitionTexts()).toEqual(['compute function app/calc.py lines 7–12'])
    expect(screen.getByText('compute', { selector: 'p > span' })).toBeTruthy()
    expect(onSelectLine).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Open app/calc.py at line 7 (function compute)' }))
    expect(onSelectLine).toHaveBeenCalledTimes(1)
    expect(onSelectLine).toHaveBeenCalledWith('app/calc.py', 7)
  })

  it('keeps an unusual server path exactly, in the text and the navigation', async () => {
    const odd: DefinitionLocation = { path: ' pkg/Weird Name .py', kind: 'module', name: 'Weird', line: 1, end_line: 4 }
    const { onSelectLine } = await showResult({ status: 200, data: result({ definitions: [odd] }) })
    await definitionList()
    expect(screen.getByText(' pkg/Weird Name .py', { normalizer: (s) => s })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /^Open/ }))
    expect(onSelectLine).toHaveBeenCalledWith(' pkg/Weird Name .py', 1)
  })

  it('shows every ambiguous definition in server order and chooses none', async () => {
    const { onSelectLine } = await showResult({
      status: 200,
      data: result({ status: 'ambiguous', definitions: CANDIDATES, reference: { ...result().reference!, name: 'Runner' } }),
    })
    await definitionList()
    expect(screen.getByText('3 possible definitions of', { exact: false })).toBeTruthy()
    expect(definitionTexts()).toEqual([
      'Runner class zeta/impl.py lines 30–40',
      'Runner function alpha/impl.py lines 5–9',
      'Runner method alpha/impl.py lines 2–3',
    ])
    expect(screen.queryByText(TRUNCATED_NOTICE)).toBeNull()
    expect(onSelectLine).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Open alpha/impl.py at line 2 (method Runner)' }))
    fireEvent.click(screen.getByRole('button', { name: 'Open zeta/impl.py at line 30 (class Runner)' }))
    expect(onSelectLine.mock.calls).toEqual([
      ['alpha/impl.py', 2],
      ['zeta/impl.py', 30],
    ])
  })

  it('keeps duplicate ambiguous definitions as returned', async () => {
    await showResult({
      status: 200,
      data: result({ status: 'ambiguous', definitions: [CANDIDATES[1], CANDIDATES[1]] }),
    })
    await definitionList()
    expect(screen.getAllByTestId('definition')).toHaveLength(2)
  })

  it('shows the truncated notice when the server says so', async () => {
    await showResult({ status: 200, data: result({ status: 'ambiguous', definitions: CANDIDATES, truncated: true }) })
    await definitionList()
    expect(screen.getByText(TRUNCATED_NOTICE)).toBeTruthy()
    expect(screen.getAllByTestId('definition')).toHaveLength(3)
  })

  it.each([
    ['not_found', 'Definition not found.'],
    ['unsupported', 'Go to definition is not supported for this reference.'],
    ['no_identifier', 'No identifier at this position.'],
    ['syntax_error', STATUS_MESSAGES.syntax_error],
    ['too_large', STATUS_MESSAGES.too_large],
    ['binary', STATUS_MESSAGES.binary],
    ['unsupported_language', STATUS_MESSAGES.unsupported_language],
    ['something_new', UNKNOWN_STATUS],
  ])('shows %s as plain text, with nothing to navigate to', async (status, message) => {
    const reference = status === 'no_identifier' || status === 'something_new' ? null : result().reference
    const { onSelectLine } = await showResult({
      status: 200,
      data: { status, reference, definitions: [], truncated: false },
    })
    expect(await screen.findByText(message)).toBeTruthy()
    expect(screen.queryByRole('list', { name: 'Definitions' })).toBeNull()
    expect(screen.queryByText('Definition of', { exact: false })).toBeNull()
    expect(screen.queryByRole('button', { name: /^Open/ })).toBeNull()
    expect(onSelectLine).not.toHaveBeenCalled()
  })

  it('shows the truncated notice with not_found too', async () => {
    await showResult({ status: 200, data: { ...result(), status: 'not_found', definitions: [], truncated: true } })
    expect(await screen.findByText('Definition not found.')).toBeTruthy()
    expect(screen.getByText(TRUNCATED_NOTICE)).toBeTruthy()
  })

  it('replaces the previous result on the next request', async () => {
    let call = 0
    await showResult(() => {
      call += 1
      return {
        status: 200,
        data: call === 1 ? result() : { ...result(), status: 'unsupported', definitions: [] },
      }
    })
    await definitionList()
    caretOn(MAIN, 'value')
    fireEvent.click(definitionButton())
    expect(await screen.findByText(STATUS_MESSAGES.unsupported)).toBeTruthy()
    expect(screen.queryByRole('list', { name: 'Definitions' })).toBeNull()
  })
})

// --- Errors ------------------------------------------------------------------------------------

describe('CodeDefinition errors', () => {
  it.each([
    [404, 'Repository not found'],
    [409, 'Repository changed during analysis; run it again'],
    [429, 'Too many attempts; please try again later'],
  ])('shows the server message for %i without retrying', async (status, detail) => {
    const { requests } = await showResult({ status, data: { detail } })
    expect((await screen.findByRole('alert')).textContent).toBe(detail)
    await sleep(50)
    expect(definitionRequests(requests)).toHaveLength(1)
    expect(screen.queryByRole('list', { name: 'Definitions' })).toBeNull()
  })

  it('shows the validation messages for 422', async () => {
    await showResult({
      status: 422,
      data: { detail: [{ msg: 'Input should be greater than or equal to 1' }, { msg: 'Field required' }] },
    })
    expect((await screen.findByRole('alert')).textContent).toBe(
      'Input should be greater than or equal to 1; Field required',
    )
  })

  it('uses the existing network and fallback messages, without retrying', async () => {
    const { requests } = await showResult(() => {
      throw new AxiosError('Network Error', AxiosError.ERR_NETWORK)
    })
    expect((await screen.findByRole('alert')).textContent).toBe('Could not reach the server')
    await sleep(50)
    expect(definitionRequests(requests)).toHaveLength(1)
  })

  it('clears an error after a successful request', async () => {
    let call = 0
    await showResult(() => {
      call += 1
      return call === 1 ? { status: 429, data: { detail: 'Too many attempts; please try again later' } } : { status: 200, data: result() }
    })
    await screen.findByRole('alert')
    fireEvent.click(definitionButton())
    await definitionList()
    expect(screen.queryByRole('alert')).toBeNull()
  })
})

// --- Stale results -------------------------------------------------------------------------

const OTHER = 'import json\n'
const contentRoute: Handler = (config) => {
  const path = (config.params as { path: string }).path
  return content(path, path === 'app/main.py' ? MAIN : OTHER)
}

// other.py is opened first so its content is cached: switching back to it renders the
// viewer immediately (no loading state), which is when the per-path key matters.
describe('CodeDefinition per file', () => {
  it('starts fresh when another, already loaded file is opened', async () => {
    const { rerenderPath, requests } = renderViewer('app/other.py', {
      [GET_CONTENT]: contentRoute,
      [GET_DEFINITION]: { status: 200, data: result() },
    })
    await waitFor(() => expect(codeText()).toBe(OTHER))
    rerenderPath('app/main.py')
    await waitFor(() => expect(codeText()).toBe(MAIN))
    caretOn(MAIN, 'compute')
    fireEvent.click(definitionButton())
    await definitionList()
    rerenderPath('app/other.py')
    await waitFor(() => expect(codeText()).toBe(OTHER))
    expect(screen.queryByRole('list', { name: 'Definitions' })).toBeNull()
    expect(screen.queryByText('app/calc.py', { exact: false })).toBeNull()
    expect(definitionButton().textContent).toBe('Go to definition')
    await sleep(50)
    expect(definitionRequests(requests)).toHaveLength(1) // none on selection
  })

  it('never shows a slow response for a file that is no longer open, even a loaded one', async () => {
    let resolveMain: (value: { data: CodeDefinitionResult }) => void = () => {}
    const realGet = apiClient.get.bind(apiClient)
    const get = vi.spyOn(apiClient, 'get').mockImplementation(((url: string, config?: { params?: Params }) =>
      url === DEFINITION_PATH && config?.params?.path === 'app/main.py'
        ? new Promise((r) => (resolveMain = r as typeof resolveMain))
        : realGet(url, config)) as typeof apiClient.get)
    try {
      const { rerenderPath } = renderViewer('app/other.py', { [GET_CONTENT]: contentRoute })
      await waitFor(() => expect(codeText()).toBe(OTHER))
      rerenderPath('app/main.py')
      await waitFor(() => expect(codeText()).toBe(MAIN))
      caretOn(MAIN, 'compute')
      fireEvent.click(definitionButton())
      await waitFor(() => expect(definitionButton().textContent).toBe('Finding definition…'))
      rerenderPath('app/other.py')
      await waitFor(() => expect(codeText()).toBe(OTHER))
      resolveMain({ data: result() })
      await sleep(50)
      expect(screen.queryByRole('list', { name: 'Definitions' })).toBeNull()
      expect(screen.queryByText('app/calc.py', { exact: false })).toBeNull()
      expect(definitionButton().textContent).toBe('Go to definition')
    } finally {
      get.mockRestore()
    }
  })
})

// --- Security ------------------------------------------------------------------------------

describe('CodeDefinition security', () => {
  it('renders server text as plain text, never as HTML', async () => {
    const hostile: DefinitionLocation = { path: `${HTML}.py`, kind: 'class', name: HTML, line: 3, end_line: 4 }
    await showResult({
      status: 200,
      data: result({ definitions: [hostile], reference: { ...result().reference!, name: HTML } }),
    })
    await definitionList()
    expect(document.querySelector('img')).toBeNull()
    expect(definitionTexts()).toEqual([`${HTML} class ${HTML}.py lines 3–4`])
    expect(screen.getByText(HTML, { selector: 'p > span' })).toBeTruthy()
  })

  it('shows only the named fields of the response', async () => {
    const extra = { source: 'FAKE-SOURCE-MARKER', sha256: 'FAKE-SHA-MARKER', id: 'FAKE-ID-MARKER' }
    await showResult({
      status: 200,
      data: {
        ...result({ definitions: [{ ...COMPUTE, ...extra }] as DefinitionLocation[] }),
        reference: { ...result().reference, ...extra },
        ...extra,
      },
    })
    await definitionList()
    const text = document.querySelector('section[aria-label="Go to definition"]')?.textContent ?? ''
    for (const marker of Object.values(extra)) {
      expect(text).not.toContain(marker)
    }
    expect(text).not.toContain('{')
    expect(text).not.toContain('"status"')
    expect(text).not.toContain('end_line')
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
      const { requests } = await showResult({ status: 200, data: result() })
      await definitionList()
      fireEvent.click(screen.getByRole('button', { name: /^Open/ }))
      // The only reads are the existing apiClient's access token lookups (services/session.ts).
      expect(new Set(getItem.mock.calls.map((call) => call[0]))).toEqual(new Set(['ai_se_access_token']))
      for (const spy of writes) {
        expect(spy).not.toHaveBeenCalled()
      }
      expect(cookieGet).not.toHaveBeenCalled()
      expect(cookieSet).not.toHaveBeenCalled()
      expect(requests.map((r) => `${r.method} ${r.url}`)).toEqual([GET_CONTENT, GET_DEFINITION])
    } finally {
      for (const spy of [getItem, ...writes]) {
        spy.mockRestore()
      }
      delete (document as { cookie?: unknown }).cookie // back to the prototype accessor
    }
  })
})

// --- Repository page -----------------------------------------------------------------------

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
        { type: 'file', name: 'calc.py', path: 'app/calc.py', extension: 'py', language: 'Python', size_bytes: 80 },
        { type: 'file', name: 'main.py', path: 'app/main.py', extension: 'py', language: 'Python', size_bytes: 60 },
      ],
    },
  ],
}

const CALC = 'import math\n\n\n\n\n\ndef compute(x):\n    return x\n\n\n\n    pass\n'

const scrollTopSpy = vi.fn()
const scrollIntoViewSpy = vi.fn()

describe('RepositoryPage go-to-definition', () => {
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
        data: { repository_id: R, status: 'ready', file_count: 2, total_bytes: 140, tree: TREE },
      },
      [`GET ${BASE}/languages`]: {
        status: 200,
        data: { repository_id: R, status: 'ready', total_files: 2, total_bytes: 140, languages: [] },
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
        return content(path, path === 'app/main.py' ? MAIN : CALC)
      },
      [GET_DEFINITION]: { status: 200, data: result() },
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

  it('opens the definition file at its start line through the existing line selection', async () => {
    const { contentPaths, requests } = renderPage()
    await screen.findByRole('table', { name: 'Secret scan summary' }) // page settled
    const tree = within(await screen.findByRole('navigation', { name: 'File tree' }))
    fireEvent.click(await tree.findByRole('button', { name: /main\.py/ }))
    await waitFor(() => expect(codeText()).toBe(MAIN))
    await sleep(50)
    expect(definitionRequests(requests)).toHaveLength(0) // not on page mount or file selection

    caretOn(MAIN, 'compute')
    fireEvent.click(definitionButton())
    fireEvent.click(await screen.findByRole('button', { name: 'Open app/calc.py at line 7 (function compute)' }))
    await waitFor(() => expect(codeText()).toBe(CALC))
    expect(contentPaths).toEqual(['app/main.py', 'app/calc.py'])
    expect(screen.getByTestId('selected-line').textContent).toBe('def compute(x):')
    expect(screen.getByText('Showing line 7')).toBeTruthy()
    expect(scrollTopSpy).toHaveBeenCalled()
    expect(scrollIntoViewSpy).not.toHaveBeenCalled() // the viewer's own line selection
    // The newly opened file starts fresh: no automatic definition request for it.
    await sleep(50)
    expect(definitionRequests(requests)).toHaveLength(1)
    expect(screen.queryByRole('list', { name: 'Definitions' })).toBeNull()
  })
})
