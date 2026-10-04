import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { AxiosError, type AxiosRequestConfig, type InternalAxiosRequestConfig } from 'axios'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { RepositoryPage } from '../pages/RepositoryPage'
import { apiClient } from '../services/client'
import { mockApi, type MockResponse, PROJECT, type RecordedRequest, ZIP_REPOSITORY } from '../test/mockApi'
import { createTestQueryClient } from '../test/render'
import type {
  CallHierarchyResult,
  CallHierarchySummary,
  CalleeGroup,
  CallerGroup,
  CodeDefinitionResult,
  DefinitionLocation,
  FileContent,
  TreeDirectory,
} from '../types/api'
import {
  CALLEES_TRUNCATED,
  CALLERS_TRUNCATED,
  CALLERS_UNSUPPORTED,
  CodeCallHierarchy,
  NO_CALLEES,
  NO_CALLERS,
  UNKNOWN_STATUS,
} from './CodeCallHierarchy'
import { FileViewer } from './FileViewer'

const P = PROJECT.id
const R = ZIP_REPOSITORY.id
const BASE = `/projects/${P}/repositories/${R}`
const DEFINITION_PATH = `${BASE}/files/definition`
const GET_DEFINITION = `GET ${DEFINITION_PATH}`
const HIERARCHY_PATH = `${BASE}/files/call-hierarchy`
const GET_HIERARCHY = `GET ${HIERARCHY_PATH}`
const GET_CONTENT = `GET ${BASE}/files/content`
const HTML = '<img src=x onerror=alert(1)>'

const MAIN = 'import tools.utils\n\nresult = compute(1)\nvalue = tools.utils.helper()\n'
const OTHER = 'import json\n'

const COMPUTE: DefinitionLocation = { path: 'app/calc.py', kind: 'function', name: 'compute', line: 7, end_line: 12 }
const COMPUTE_LABEL = 'Call hierarchy for function compute (app/calc.py, line 7)'
const MODULE: DefinitionLocation = { path: 'pkg/tools.py', kind: 'module', name: 'tools', line: 1, end_line: 1 }
const METHOD: DefinitionLocation = { path: 'alpha/impl.py', kind: 'method', name: 'Runner', line: 2, end_line: 3 }
// Server order, deliberately not alphabetical by path, name or line.
const CANDIDATES: DefinitionLocation[] = [
  { path: 'zeta/impl.py', kind: 'class', name: 'Runner', line: 30, end_line: 40 },
  { path: 'alpha/impl.py', kind: 'function', name: 'Runner', line: 5, end_line: 9 },
  METHOD,
]
const CANDIDATE_LABELS = [
  'Call hierarchy for class Runner (zeta/impl.py, line 30)',
  'Call hierarchy for function Runner (alpha/impl.py, line 5)',
  'Call hierarchy for method Runner (alpha/impl.py, line 2)',
]

// Server order deliberately unsorted (groups and calls), with a duplicate group.
const MODULE_CALLER: CallerGroup = {
  caller: { path: 'app/main.py', kind: 'module', name: 'main', line: 1, end_line: 1 },
  total_calls: 1,
  calls: [{ line: 3, column: 10, name: 'compute', ambiguous: false }],
}
const CALLERS: CallerGroup[] = [
  {
    caller: { path: 'tests/test_calc.py', kind: 'function', name: 'test_compute', line: 20, end_line: 25 },
    total_calls: 3,
    calls: [
      { line: 22, column: 5, name: 'compute', ambiguous: true },
      { line: 21, column: 9, name: 'compute', ambiguous: false },
    ],
  },
  MODULE_CALLER,
  MODULE_CALLER,
]
const CALLER_TEXTS = [
  'test_compute function tests/test_calc.py lines 20–25 · 3 calls',
  'main module app/main.py lines 1–1 · 1 calls',
  'main module app/main.py lines 1–1 · 1 calls',
]
const CALLEES: CalleeGroup[] = [
  {
    callee: { path: 'app/util.py', kind: 'function', name: 'helper', line: 40, end_line: 44 },
    total_calls: 2,
    calls: [
      { line: 9, column: 12, name: 'helper', ambiguous: false },
      { line: 8, column: 5, name: 'h', ambiguous: true },
    ],
  },
  {
    callee: { path: 'app/models.py', kind: 'class', name: 'Model', line: 3, end_line: 30 },
    total_calls: 1,
    calls: [{ line: 10, column: 16, name: 'Model', ambiguous: false }],
  },
]
const CALLEE_TEXTS = [
  'helper function app/util.py lines 40–44 · 2 calls',
  'Model class app/models.py lines 3–30 · 1 calls',
]
// Every count distinct, so a swapped field is visible.
const SUMMARY: CallHierarchySummary = {
  files_scanned: 41,
  too_large: 1,
  binary: 2,
  syntax_error: 3,
  caller_calls: 4,
  caller_ambiguous: 5,
  caller_unsupported: 6,
  non_call_references: 7,
  callee_calls: 8,
  callee_ambiguous: 9,
  callee_unsupported: 10,
  callee_builtin: 11,
  callee_unresolved: 12,
  callee_not_callable: 13,
  callee_dynamic: 14,
}
const SUMMARY_TEXT =
  'Callers: 4 calls · 5 ambiguous · 6 unsupported. ' +
  'Callees: 8 calls · 9 ambiguous · 10 unsupported · 11 builtin · 12 unresolved · 14 dynamic.'
const ZERO: CallHierarchySummary = Object.fromEntries(
  Object.keys(SUMMARY).map((k) => [k, 0]),
) as unknown as CallHierarchySummary

function definitionResult(extra: Partial<CodeDefinitionResult> = {}): CodeDefinitionResult {
  return {
    status: 'resolved',
    reference: { path: 'app/main.py', line: 3, column: 10, name: 'compute' },
    definitions: [COMPUTE],
    truncated: false,
    ...extra,
  }
}

const AMBIGUOUS = definitionResult({
  status: 'ambiguous',
  reference: { path: 'app/main.py', line: 3, column: 10, name: 'Runner' },
  definitions: CANDIDATES,
})

function hierarchyResult(extra: Partial<CallHierarchyResult> = {}): CallHierarchyResult {
  return {
    status: 'scanned',
    target: COMPUTE,
    callers_status: 'scanned',
    callers: CALLERS,
    callees: CALLEES,
    summary: SUMMARY,
    callers_truncated: false,
    callees_truncated: false,
    ...extra,
  }
}

/** A result for `target` whose only callee call is in `file`'s... named `marker`. */
function only(target: DefinitionLocation, marker: string): CallHierarchyResult {
  return hierarchyResult({
    target,
    callers: [],
    callees: [
      {
        callee: { path: `${marker}.py`, kind: 'function', name: marker, line: 1, end_line: 2 },
        total_calls: 1,
        calls: [{ line: 4, column: 2, name: marker, ambiguous: false }],
      },
    ],
  })
}

function content(path: string, text: string | null, extra: Partial<FileContent> = {}): MockResponse {
  const language = path.endsWith('.py') ? 'Python' : path.endsWith('.md') ? 'Markdown' : null
  return {
    status: 200,
    data: { path, language, size_bytes: text?.length ?? 0, content: text, reason: null, ...extra },
  }
}

type Handler = MockResponse | ((config: InternalAxiosRequestConfig) => MockResponse)
type Params = { path: string; line: number }

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))
const codeText = () => document.querySelector('pre code')?.textContent
const definitionButton = () =>
  within(screen.getByRole('region', { name: 'Go to definition' })).getByRole('button', {
    name: /definition/i,
  }) as HTMLButtonElement
const definitionList = () => screen.findByRole('list', { name: 'Definitions' })
const region = () => screen.getByRole('region', { name: 'Call hierarchy' })
const actionButtons = () =>
  within(region()).getAllByRole('button', { name: /hierarchy for/ }) as HTMLButtonElement[]
const action = (name: string | RegExp) =>
  within(region()).getByRole('button', { name }) as HTMLButtonElement
const callerTexts = () => screen.getAllByTestId('caller').map((g) => g.firstChild?.textContent)
const calleeTexts = () => screen.getAllByTestId('callee').map((g) => g.firstChild?.textContent)
const callerCalls = () => screen.getAllByTestId('caller-call').map((c) => c.textContent)
const calleeCalls = () => screen.getAllByTestId('callee-call').map((c) => c.textContent)
const targetText = () => screen.getByTestId('call-hierarchy-target').textContent
const summaryText = () => screen.getByTestId('call-hierarchy-summary').textContent
const byUrl = (requests: RecordedRequest[], url: string) => requests.filter((r) => r.url === url)
const loaded = () =>
  waitFor(() =>
    expect(screen.queryByTestId('call-hierarchy-target') ?? screen.queryByRole('alert')).not.toBeNull(),
  )

function caretOn(text: string, needle: string) {
  const index = text.indexOf(needle)
  expect(index).toBeGreaterThanOrEqual(0)
  const walker = document.createTreeWalker(document.querySelector('pre') as HTMLPreElement, NodeFilter.SHOW_TEXT)
  let remaining = index
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const length = (node as Text).data.length
    if (remaining <= length) {
      document.getSelection()?.setBaseAndExtent(node, remaining, node, remaining)
      return
    }
    remaining -= length
  }
  throw new Error('offset outside the code')
}

function renderViewer(path: string, routes: Record<string, Handler>) {
  const requests = mockApi(routes)
  const client = createTestQueryClient()
  const onSelectLine = vi.fn<(path: string, line: number) => void>()
  const ui = (selectedPath: string) => (
    <QueryClientProvider client={client}>
      <FileViewer
        projectId={P}
        repositoryId={R}
        selectedPath={selectedPath}
        onSelectLine={onSelectLine}
        onSelectFile={vi.fn()}
      />
    </QueryClientProvider>
  )
  const view = render(ui(path))
  return { requests, onSelectLine, rerenderPath: (next: string) => view.rerender(ui(next)), ...view }
}

/** Opens main.py, runs Go to definition and records the params of every hierarchy request. */
async function showDefinition(
  definition: Handler = { status: 200, data: definitionResult() },
  hierarchy: Handler = { status: 200, data: hierarchyResult() },
) {
  const seen: Params[] = []
  const view = renderViewer('app/main.py', {
    [GET_CONTENT]: content('app/main.py', MAIN),
    [GET_DEFINITION]: definition,
    [GET_HIERARCHY]: (config) => {
      seen.push({ ...(config.params as Params) })
      return typeof hierarchy === 'function' ? hierarchy(config) : hierarchy
    },
  })
  await waitFor(() => expect(codeText()).toBe(MAIN))
  caretOn(MAIN, 'compute')
  fireEvent.click(definitionButton())
  await definitionList()
  return { seen, ...view }
}

async function showHierarchy(hierarchy: Handler, label = COMPUTE_LABEL) {
  const view = await showDefinition(undefined, hierarchy)
  fireEvent.click(action(label))
  await loaded()
  return view
}

function renderPanel(
  definitions: DefinitionLocation[] | null,
  hierarchy: Handler = { status: 200, data: hierarchyResult() },
  onSelectLine?: (path: string, line: number) => void,
) {
  const seen: Params[] = []
  const requests = mockApi({
    [GET_HIERARCHY]: (config) => {
      seen.push({ ...(config.params as Params) })
      return typeof hierarchy === 'function' ? hierarchy(config) : hierarchy
    },
  })
  const view = render(
    <QueryClientProvider client={createTestQueryClient()}>
      <CodeCallHierarchy projectId={P} repositoryId={R} definitions={definitions} onSelectLine={onSelectLine} />
    </QueryClientProvider>,
  )
  return { seen, requests, ...view }
}

/** Holds every hierarchy request matching `when` until released; others go through. */
function holdHierarchy(when: (params: Params) => boolean = () => true) {
  const held: { params: Params; release: (data: CallHierarchyResult) => void }[] = []
  const realGet = apiClient.get.bind(apiClient)
  const spy = vi.spyOn(apiClient, 'get').mockImplementation(((url: string, config?: AxiosRequestConfig) => {
    const params = config?.params as Params | undefined
    if (url === HIERARCHY_PATH && params && when(params)) {
      return new Promise<unknown>((resolve) => {
        held.push({ params: { ...params }, release: (data) => resolve({ data }) })
      })
    }
    return realGet(url, config)
  }) as typeof apiClient.get)
  const calls = () => spy.mock.calls.filter(([url]) => url === HIERARCHY_PATH).length
  return { held, spy, calls }
}

afterEach(() => {
  document.getSelection()?.removeAllRanges()
})

// --- Visibility and requests ----------------------------------------------------------------

describe('Call hierarchy visibility and requests', () => {
  it('requests nothing on file load, selection, a definition result or Open', async () => {
    const { requests, onSelectLine } = await showDefinition()
    caretOn(MAIN, 'value')
    fireEvent.mouseUp(document.querySelector('pre') as HTMLPreElement)
    fireEvent.keyUp(document.querySelector('pre') as HTMLPreElement, { key: 'ArrowRight' })
    expect(actionButtons().map((b) => b.textContent)).toEqual([COMPUTE_LABEL])
    fireEvent.click(screen.getByRole('button', { name: 'Open app/calc.py at line 7 (function compute)' }))
    expect(onSelectLine).toHaveBeenCalledTimes(1)
    await sleep(50)
    expect(byUrl(requests, HIERARCHY_PATH)).toHaveLength(0)
    expect(screen.queryByTestId('call-hierarchy-target')).toBeNull()
  })

  it('sends exactly the definition path and start line as one GET, once', async () => {
    const { seen, requests } = await showDefinition()
    fireEvent.click(action(COMPUTE_LABEL))
    await loaded()
    const sent = byUrl(requests, HIERARCHY_PATH)
    expect(sent).toHaveLength(1)
    expect(sent[0].method).toBe('GET')
    expect(sent[0].data).toBeUndefined()
    // The start line (7), never end_line (12), the cursor line (3), a column or a name.
    expect(seen).toEqual([{ path: 'app/calc.py', line: 7 }])
    await sleep(100)
    expect(byUrl(requests, HIERARCHY_PATH)).toHaveLength(1) // no polling
  })

  it('sends an unusual server path unchanged', async () => {
    const odd: DefinitionLocation = { path: ' pkg/My Dir/../x%20y.py ', kind: 'function', name: 'run', line: 4, end_line: 6 }
    const { seen } = renderPanel([odd])
    fireEvent.click(screen.getByRole('button', { name: /^Call hierarchy/ }))
    await screen.findByTestId('call-hierarchy-target')
    expect(seen).toEqual([{ path: ' pkg/My Dir/../x%20y.py ', line: 4 }])
  })

  it('offers no action for modules, one per other definition in server order', async () => {
    const { requests } = await showDefinition({ status: 200, data: definitionResult({ definitions: [MODULE] }) })
    expect(screen.queryByRole('region', { name: 'Call hierarchy' })).toBeNull()
    await sleep(50)
    expect(byUrl(requests, HIERARCHY_PATH)).toHaveLength(0)
    renderPanel([CANDIDATES[0], MODULE, ...CANDIDATES.slice(1)])
    expect(screen.getAllByRole('button', { name: /^Call hierarchy/ }).map((b) => b.textContent)).toEqual(
      CANDIDATE_LABELS,
    )
  })

  it('ambiguous: each action sends only its own definition', async () => {
    const { seen } = await showDefinition({ status: 200, data: AMBIGUOUS })
    expect(actionButtons().map((b) => b.textContent)).toEqual(CANDIDATE_LABELS)
    fireEvent.click(action(CANDIDATE_LABELS[2])) // a method: still a valid target
    await loaded()
    expect(seen).toEqual([{ path: 'alpha/impl.py', line: 2 }])
    fireEvent.click(action(CANDIDATE_LABELS[0]))
    await waitFor(() => expect(seen).toHaveLength(2))
    expect(seen[1]).toEqual({ path: 'zeta/impl.py', line: 30 })
  })

  it('labels only the exact path and line as loading and disables every action meanwhile', async () => {
    const beta: DefinitionLocation = { path: 'beta/impl.py', kind: 'function', name: 'Runner', line: 5, end_line: 6 }
    const hold = holdHierarchy()
    try {
      renderPanel([...CANDIDATES, beta])
      fireEvent.click(screen.getByRole('button', { name: CANDIDATE_LABELS[1] }))
      await screen.findByText('Loading call hierarchy for function Runner (alpha/impl.py, line 5)…')
      const buttons = screen.getAllByRole('button') as HTMLButtonElement[]
      expect(buttons.map((b) => b.textContent)).toEqual([
        CANDIDATE_LABELS[0],
        'Loading call hierarchy for function Runner (alpha/impl.py, line 5)…',
        CANDIDATE_LABELS[2],
        'Call hierarchy for function Runner (beta/impl.py, line 5)',
      ])
      expect(buttons.map((b) => b.disabled)).toEqual([true, true, true, true])
      fireEvent.click(buttons[0])
      await sleep(20)
      expect(hold.calls()).toBe(1)
      hold.held[0].release(only(CANDIDATES[1], 'b'))
      await screen.findByText('Calls of function Runner (alpha/impl.py, line 5)')
      expect(screen.getAllByRole('button', { name: /^Call hierarchy/ }).every((b) => !(b as HTMLButtonElement).disabled)).toBe(true)
    } finally {
      hold.spy.mockRestore()
    }
  })
})

// --- Results ---------------------------------------------------------------------------------

describe('Call hierarchy results', () => {
  it('shows callers and callees in server order with duplicates, the target and the summary', async () => {
    await showHierarchy({ status: 200, data: hierarchyResult() })
    expect(targetText()).toBe('Calls of function compute (app/calc.py, line 7)')
    expect(callerTexts()).toEqual(CALLER_TEXTS)
    expect(callerCalls()).toEqual([
      'compute line 22, column 5 · ambiguous',
      'compute line 21, column 9',
      'compute line 3, column 10',
      'compute line 3, column 10',
    ])
    expect(calleeTexts()).toEqual(CALLEE_TEXTS)
    expect(calleeCalls()).toEqual([
      'helper line 9, column 12',
      'h line 8, column 5 · ambiguous',
      'Model line 10, column 16',
    ])
    expect(summaryText()).toBe(SUMMARY_TEXT)
    for (const notice of [CALLERS_TRUNCATED, CALLEES_TRUNCATED, CALLERS_UNSUPPORTED, NO_CALLERS, NO_CALLEES]) {
      expect(screen.queryByText(notice)).toBeNull()
    }
    expect(screen.getByRole('list', { name: 'Definitions' })).toBeTruthy()
  })

  it('navigates callers to caller.path and callees to the target file, at the call line', async () => {
    const { onSelectLine } = await showHierarchy({ status: 200, data: hierarchyResult() })
    expect(onSelectLine).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Jump to tests/test_calc.py at line 22 (compute, ambiguous)' }))
    fireEvent.click(screen.getAllByRole('button', { name: 'Jump to app/main.py at line 3 (compute)' })[1])
    // Callee calls are in the target's file (app/calc.py), never callee.path or the open file.
    fireEvent.click(screen.getByRole('button', { name: 'Jump to app/calc.py at line 9 (helper)' }))
    fireEvent.click(screen.getByRole('button', { name: 'Jump to app/calc.py at line 8 (h, ambiguous)' }))
    fireEvent.click(screen.getByRole('button', { name: 'Open caller function test_compute in tests/test_calc.py at line 20' }))
    fireEvent.click(screen.getByRole('button', { name: 'Open callee function helper in app/util.py at line 40' }))
    expect(onSelectLine.mock.calls).toEqual([
      ['tests/test_calc.py', 22],
      ['app/main.py', 3],
      ['app/calc.py', 9],
      ['app/calc.py', 8],
      ['tests/test_calc.py', 20],
      ['app/util.py', 40], // the definition's start line, never end_line (44)
    ])
  })

  it('navigates callee calls to the server target, not the clicked definition', async () => {
    const moved: DefinitionLocation = { ...COMPUTE, path: 'app/calc2.py' }
    const { onSelectLine } = await showHierarchy({ status: 200, data: hierarchyResult({ target: moved }) })
    fireEvent.click(screen.getByRole('button', { name: 'Jump to app/calc2.py at line 9 (helper)' }))
    expect(onSelectLine.mock.calls).toEqual([['app/calc2.py', 9]])
  })

  it('shows empty callers and callees', async () => {
    await showHierarchy({ status: 200, data: hierarchyResult({ callers: [], callees: [], summary: ZERO }) })
    expect(screen.getByText('No callers found.')).toBeTruthy()
    expect(screen.getByText('No callees found.')).toBeTruthy()
    expect(screen.queryByRole('list', { name: 'Callers' })).toBeNull()
    expect(screen.queryByRole('list', { name: 'Callees' })).toBeNull()
  })

  it('a method: callers unsupported, callees still shown', async () => {
    renderPanel(
      [METHOD],
      {
        status: 200,
        data: hierarchyResult({ target: METHOD, callers_status: 'unsupported_target', callers: null, callers_truncated: null }),
      },
      vi.fn(),
    )
    fireEvent.click(screen.getByRole('button', { name: /^Call hierarchy/ }))
    expect(await screen.findByText('Caller analysis is not available for this target.')).toBeTruthy()
    expect(screen.queryByRole('list', { name: 'Callers' })).toBeNull()
    expect(screen.queryByText(NO_CALLERS)).toBeNull()
    expect(calleeTexts()).toEqual(CALLEE_TEXTS)
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByText(CALLERS_TRUNCATED)).toBeNull()
  })

  it.each([
    [true, false],
    [false, true],
    [true, true],
    [false, false],
  ])('truncation notices are separate (callers %s, callees %s)', async (callers, callees) => {
    renderPanel([COMPUTE], {
      status: 200,
      data: hierarchyResult({ callers_truncated: callers, callees_truncated: callees }),
    })
    fireEvent.click(screen.getByRole('button', { name: COMPUTE_LABEL }))
    await screen.findByTestId('call-hierarchy-target')
    expect(screen.queryByText('Caller results are truncated.') !== null).toBe(callers)
    expect(screen.queryByText('Callee results are truncated.') !== null).toBe(callees)
  })

  it.each([
    ['not_a_definition', 'Call hierarchy is unavailable: no class or function starts on this line.'],
    ['syntax_error', 'Call hierarchy is unavailable: the target file could not be parsed as Python.'],
    ['too_large', 'Call hierarchy is unavailable: the target file is too large to analyze.'],
    ['binary', 'Call hierarchy is unavailable: the target file is not text.'],
    ['unsupported_language', 'Call hierarchy is only available for Python files.'],
    ['something_new', UNKNOWN_STATUS],
  ])('shows %s as plain text with nothing to navigate to', async (status, message) => {
    const onSelectLine = vi.fn()
    renderPanel(
      [COMPUTE],
      {
        status: 200,
        data: {
          status,
          target: null,
          callers_status: null,
          // Even groups in an unknown payload are never shown outside a scan.
          callers: status === 'something_new' ? CALLERS : null,
          callees: status === 'something_new' ? CALLEES : [],
          summary: ZERO,
          callers_truncated: null,
          callees_truncated: false,
        },
      },
      onSelectLine,
    )
    fireEvent.click(screen.getByRole('button', { name: COMPUTE_LABEL }))
    expect(await screen.findByText(message)).toBeTruthy()
    expect(targetText()).toBe('Calls of function compute (app/calc.py, line 7)')
    expect(screen.queryByTestId('caller')).toBeNull()
    expect(screen.queryByTestId('callee')).toBeNull()
    expect(screen.queryByTestId('call-hierarchy-summary')).toBeNull()
    expect(screen.queryByText(CALLERS_UNSUPPORTED)).toBeNull()
    expect(screen.queryByRole('button', { name: /^(Jump|Open) / })).toBeNull()
    expect(onSelectLine).not.toHaveBeenCalled()
  })
})

// --- Errors ------------------------------------------------------------------------------------

describe('Call hierarchy errors', () => {
  it.each([
    [404, 'Repository not found'],
    [409, 'Repository changed during analysis; run it again'],
    [429, 'Too many attempts; please try again later'],
  ])('shows the server detail for %i exactly, without retrying', async (status, detail) => {
    const { requests } = await showHierarchy({ status, data: { detail } })
    const alerts = await screen.findAllByRole('alert')
    expect(alerts.map((a) => a.textContent)).toEqual([detail])
    await sleep(50)
    expect(byUrl(requests, HIERARCHY_PATH)).toHaveLength(1)
    expect(screen.queryByTestId('caller')).toBeNull()
  })

  it('joins 422 messages, uses the network message and the fallback', async () => {
    await showHierarchy({ status: 422, data: { detail: [{ msg: 'Input should be greater than or equal to 1' }, { msg: 'Field required' }] } })
    expect((await screen.findByRole('alert')).textContent).toBe('Input should be greater than or equal to 1; Field required')
  })

  it('uses the existing network message without retrying', async () => {
    const { requests } = await showHierarchy(() => {
      throw new AxiosError('Network Error', AxiosError.ERR_NETWORK)
    })
    expect((await screen.findByRole('alert')).textContent).toBe('Could not reach the server')
    await sleep(50)
    expect(byUrl(requests, HIERARCHY_PATH)).toHaveLength(1)
  })

  it('shows the fallback for an error without a usable detail', async () => {
    await showHierarchy({ status: 500, data: {} })
    expect((await screen.findByRole('alert')).textContent).toBe('Could not load the call hierarchy')
  })
})

// --- Stale results -------------------------------------------------------------------------

describe('Call hierarchy never shows a stale result', () => {
  it('a new go-to-definition lookup drops a request that is still loading', async () => {
    const hold = holdHierarchy()
    try {
      let lookups = 0
      await showDefinition(() => {
        lookups += 1
        return { status: 200, data: lookups === 1 ? definitionResult() : AMBIGUOUS }
      })
      fireEvent.click(action(COMPUTE_LABEL))
      await screen.findByText('Loading call hierarchy for function compute (app/calc.py, line 7)…')
      caretOn(MAIN, 'value')
      fireEvent.click(definitionButton())
      await screen.findByText('3 possible definitions of', { exact: false })
      expect(actionButtons().map((b) => [b.textContent, b.disabled])).toEqual(
        CANDIDATE_LABELS.map((label) => [label, false]),
      )
      hold.held[0].release(only(COMPUTE, 'stale'))
      await sleep(50)
      expect(screen.queryByTestId('call-hierarchy-target')).toBeNull()
      expect(screen.queryByText('stale.py', { exact: false })).toBeNull()
    } finally {
      hold.spy.mockRestore()
    }
  })

  it('a new lookup clears shown results, even with an identical result', async () => {
    const { requests } = await showHierarchy({ status: 200, data: hierarchyResult() })
    fireEvent.click(definitionButton())
    await waitFor(() => expect(byUrl(requests, DEFINITION_PATH)).toHaveLength(2))
    await definitionList()
    await sleep(50)
    expect(actionButtons().map((b) => b.textContent)).toEqual([COMPUTE_LABEL])
    expect(screen.queryByTestId('call-hierarchy-target')).toBeNull()
    expect(screen.queryByTestId('caller')).toBeNull()
    expect(byUrl(requests, HIERARCHY_PATH)).toHaveLength(1)
  })

  const contentRoute: Handler = (config) => {
    const path = (config.params as { path: string }).path
    return content(path, path === 'app/main.py' ? MAIN : OTHER)
  }

  it('never shows a result for a file that is no longer open, even a loaded one', async () => {
    const hold = holdHierarchy()
    try {
      const { rerenderPath } = renderViewer('app/other.py', {
        [GET_CONTENT]: contentRoute,
        [GET_DEFINITION]: { status: 200, data: definitionResult() },
      })
      await waitFor(() => expect(codeText()).toBe(OTHER))
      rerenderPath('app/main.py')
      await waitFor(() => expect(codeText()).toBe(MAIN))
      caretOn(MAIN, 'compute')
      fireEvent.click(definitionButton())
      await definitionList()
      fireEvent.click(action(COMPUTE_LABEL))
      await screen.findByRole('button', { name: /^Loading call hierarchy/ })
      rerenderPath('app/other.py')
      await waitFor(() => expect(codeText()).toBe(OTHER))
      hold.held[0].release(hierarchyResult())
      await sleep(50)
      expect(screen.queryByRole('region', { name: 'Call hierarchy' })).toBeNull()
      expect(screen.queryByText('test_compute', { exact: false })).toBeNull()
    } finally {
      hold.spy.mockRestore()
    }
  })

  it('starts fresh in another loaded file, with no request on selection and no cache', async () => {
    const { rerenderPath, requests } = renderViewer('app/other.py', {
      [GET_CONTENT]: contentRoute,
      [GET_DEFINITION]: { status: 200, data: definitionResult() },
      [GET_HIERARCHY]: { status: 200, data: hierarchyResult() },
    })
    await waitFor(() => expect(codeText()).toBe(OTHER))
    rerenderPath('app/main.py')
    await waitFor(() => expect(codeText()).toBe(MAIN))
    caretOn(MAIN, 'compute')
    fireEvent.click(definitionButton())
    await definitionList()
    fireEvent.click(action(COMPUTE_LABEL))
    await loaded()
    rerenderPath('app/other.py')
    await waitFor(() => expect(codeText()).toBe(OTHER))
    expect(screen.queryByRole('region', { name: 'Call hierarchy' })).toBeNull()
    rerenderPath('app/main.py')
    await waitFor(() => expect(codeText()).toBe(MAIN))
    caretOn(MAIN, 'compute')
    fireEvent.click(definitionButton())
    await definitionList()
    await sleep(50)
    expect(screen.queryByTestId('call-hierarchy-target')).toBeNull()
    expect(byUrl(requests, HIERARCHY_PATH)).toHaveLength(1)
  })
})

// --- Security ------------------------------------------------------------------------------

describe('Call hierarchy security', () => {
  it('renders server text as plain text, never as HTML', async () => {
    const hostile: DefinitionLocation = { path: `${HTML}.py`, kind: 'function', name: HTML, line: 3, end_line: 4 }
    renderPanel([hostile], {
      status: 200,
      data: hierarchyResult({
        target: hostile,
        callers: [{ caller: hostile, total_calls: 1, calls: [{ line: 5, column: 6, name: HTML, ambiguous: true }] }],
        callees: [],
      }),
    })
    fireEvent.click(screen.getByRole('button', { name: /^Call hierarchy/ }))
    await screen.findByTestId('call-hierarchy-target')
    expect(callerCalls()).toEqual([`${HTML} line 5, column 6 · ambiguous`])
    expect(document.querySelector('img, script, a, iframe')).toBeNull()
    expect(document.querySelector('[href], [src], [onerror]')).toBeNull()
  })

  it('shows only the named fields of the response', async () => {
    const extra = { snippet: 'FAKE-SNIPPET-MARKER', sha256: 'FAKE-SHA-MARKER', id: 'FAKE-ID-MARKER' }
    const payload = {
      ...hierarchyResult({
        callers: CALLERS.map((g) => ({ ...g, ...extra, calls: g.calls.map((c) => ({ ...c, ...extra })) })),
        callees: CALLEES.map((g) => ({ ...g, ...extra, callee: { ...g.callee, ...extra } })),
      }),
      target: { ...COMPUTE, ...extra },
      summary: { ...SUMMARY, ...extra },
      ...extra,
    }
    renderPanel([COMPUTE], { status: 200, data: payload }, vi.fn())
    fireEvent.click(screen.getByRole('button', { name: COMPUTE_LABEL }))
    await screen.findByTestId('call-hierarchy-target')
    const text = region().textContent ?? ''
    const labels = Array.from(region().querySelectorAll('[aria-label]')).map((e) => e.getAttribute('aria-label'))
    for (const marker of Object.values(extra)) {
      expect(text).not.toContain(marker)
      expect(labels.join(' ')).not.toContain(marker)
    }
    expect(text).not.toContain('{')
    expect(text).not.toContain('"status"')
    expect(text).not.toContain('callers_status')
  })

  it('uses no browser storage or cookies and calls only the app API', async () => {
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
      const { requests } = await showHierarchy({ status: 200, data: hierarchyResult() })
      fireEvent.click(screen.getByRole('button', { name: 'Jump to app/calc.py at line 9 (helper)' }))
      expect(new Set(getItem.mock.calls.map((call) => call[0]))).toEqual(new Set(['ai_se_access_token']))
      for (const spy of writes) {
        expect(spy).not.toHaveBeenCalled()
      }
      expect([localStorage.length, sessionStorage.length]).toEqual(stored)
      expect(cookieGet).not.toHaveBeenCalled()
      expect(cookieSet).not.toHaveBeenCalled()
      expect(fetchSpy).not.toHaveBeenCalled()
      expect(requests.map((r) => `${r.method} ${r.url}`)).toEqual([GET_CONTENT, GET_DEFINITION, GET_HIERARCHY])
      expect(requests.every((r) => r.baseURL === '/api/v1')).toBe(true)
    } finally {
      for (const spy of [getItem, ...writes]) {
        spy.mockRestore()
      }
      globalThis.fetch = realFetch
      delete (document as { cookie?: unknown }).cookie
    }
  })

  it.each([
    ['no result', null],
    ['no definitions', []],
  ])('renders nothing and requests nothing with %s', async (_name, definitions) => {
    const { container, requests } = renderPanel(definitions)
    expect(container.childElementCount).toBe(0)
    await sleep(30)
    expect(requests).toHaveLength(0)
  })

  it('without onSelectLine, groups and calls are plain text', async () => {
    renderPanel([COMPUTE])
    fireEvent.click(screen.getByRole('button', { name: COMPUTE_LABEL }))
    await screen.findByTestId('call-hierarchy-target')
    expect(callerTexts()).toEqual(CALLER_TEXTS)
    expect(screen.queryAllByRole('button', { name: /^(Jump|Open) / })).toHaveLength(0)
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

const CALC = 'import math\n\n\n\n\n\ndef compute(x):\n    return x\n\n\n\nanswer = compute(2)\n'
const PAGE_HIERARCHY = hierarchyResult({
  callers: [
    {
      caller: { path: 'app/calc.py', kind: 'module', name: 'calc', line: 1, end_line: 1 },
      total_calls: 1,
      calls: [{ line: 12, column: 10, name: 'compute', ambiguous: false }],
    },
  ],
  callees: [],
})

const scrollTopSpy = vi.fn()
const scrollIntoViewSpy = vi.fn()

describe('RepositoryPage call hierarchy', () => {
  beforeEach(() => {
    Object.defineProperty(HTMLSpanElement.prototype, 'offsetTop', {
      configurable: true,
      get(this: HTMLElement) {
        return this.dataset.testid === 'selected-line' ? 400 : 0
      },
    })
    Object.defineProperty(HTMLPreElement.prototype, 'clientHeight', { configurable: true, get: () => 300 })
    Object.defineProperty(HTMLPreElement.prototype, 'scrollTop', { configurable: true, get: () => 0, set: scrollTopSpy })
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, writable: true, value: scrollIntoViewSpy })
  })

  afterEach(() => {
    scrollTopSpy.mockReset()
    scrollIntoViewSpy.mockReset()
    delete (HTMLSpanElement.prototype as { offsetTop?: unknown }).offsetTop
    delete (HTMLPreElement.prototype as { clientHeight?: unknown }).clientHeight
    delete (HTMLPreElement.prototype as { scrollTop?: unknown }).scrollTop
    delete (HTMLElement.prototype as { scrollIntoView?: unknown }).scrollIntoView
  })

  it('opens a caller call in another file at its line through the existing line selection', async () => {
    const contentPaths: string[] = []
    const requests = mockApi({
      [`GET ${BASE}`]: { status: 200, data: { ...ZIP_REPOSITORY, status: 'ready' } },
      [`GET ${BASE}/tree`]: { status: 200, data: { repository_id: R, status: 'ready', file_count: 2, total_bytes: 140, tree: TREE } },
      [`GET ${BASE}/languages`]: { status: 200, data: { repository_id: R, status: 'ready', total_files: 2, total_bytes: 140, languages: [] } },
      [`GET ${BASE}/jobs`]: { status: 200, data: [] },
      [`GET ${BASE}/analysis/code-metrics`]: { status: 200, data: { job: null, summary: null, files: [] } },
      [`GET ${BASE}/analysis/dependencies`]: {
        status: 200,
        data: { manifests: [], summary: { manifests: 0, parsed: 0, invalid: 0, too_large: 0, unavailable: 0, dependencies: 0 }, truncated: false },
      },
      [`GET ${BASE}/analysis/secrets`]: {
        status: 200,
        data: { findings: [], summary: { files_scanned: 2, binary: 0, too_large: 0, unavailable: 0, findings: 0 }, truncated: false },
      },
      [GET_CONTENT]: (config) => {
        const path = (config.params as { path: string }).path
        contentPaths.push(path)
        return content(path, path === 'app/main.py' ? MAIN : CALC)
      },
      [GET_DEFINITION]: { status: 200, data: definitionResult() },
      [GET_HIERARCHY]: { status: 200, data: PAGE_HIERARCHY },
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
    await screen.findByRole('table', { name: 'Secret scan summary' })
    const tree = within(await screen.findByRole('navigation', { name: 'File tree' }))
    fireEvent.click(await tree.findByRole('button', { name: /main\.py/ }))
    await waitFor(() => expect(codeText()).toBe(MAIN))
    await sleep(50)
    expect(byUrl(requests, HIERARCHY_PATH)).toHaveLength(0)
    caretOn(MAIN, 'compute')
    fireEvent.click(definitionButton())
    await definitionList()
    fireEvent.click(action(COMPUTE_LABEL))
    fireEvent.click(await screen.findByRole('button', { name: 'Jump to app/calc.py at line 12 (compute)' }))
    await waitFor(() => expect(codeText()).toBe(CALC))
    expect(contentPaths).toEqual(['app/main.py', 'app/calc.py'])
    expect(screen.getByTestId('selected-line').textContent).toBe('answer = compute(2)')
    expect(scrollTopSpy).toHaveBeenCalled()
    expect(scrollIntoViewSpy).not.toHaveBeenCalled()
    await sleep(50)
    expect(byUrl(requests, HIERARCHY_PATH)).toHaveLength(1)
  })
})
