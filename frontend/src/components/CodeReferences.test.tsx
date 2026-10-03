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
  CodeDefinitionResult,
  CodeReferencesResult,
  DefinitionLocation,
  FileContent,
  ReferenceLocation,
  ReferencesSummary,
  TreeDirectory,
} from '../types/api'
import { NO_POSITION } from './CodeDefinition'
import {
  CodeReferences,
  NO_REFERENCES,
  STATUS_MESSAGES,
  TRUNCATED_NOTICE,
  UNKNOWN_STATUS,
} from './CodeReferences'
import { FileViewer } from './FileViewer'

const P = PROJECT.id
const R = ZIP_REPOSITORY.id
const BASE = `/projects/${P}/repositories/${R}`
const DEFINITION_PATH = `${BASE}/files/definition`
const GET_DEFINITION = `GET ${DEFINITION_PATH}`
const REFERENCES_PATH = `${BASE}/files/references`
const GET_REFERENCES = `GET ${REFERENCES_PATH}`
const GET_CONTENT = `GET ${BASE}/files/content`
const HTML = '<img src=x onerror=alert(1)>'

const MAIN = 'import tools.utils\n\nresult = compute(1)\nvalue = tools.utils.helper()\n'
const OTHER = 'import json\n'

const COMPUTE: DefinitionLocation = { path: 'app/calc.py', kind: 'function', name: 'compute', line: 7, end_line: 12 }
const COMPUTE_LABEL = 'Find references to function compute (app/calc.py, line 7)'
const MODULE: DefinitionLocation = { path: 'pkg/tools.py', kind: 'module', name: 'tools', line: 1, end_line: 1 }
const METHOD: DefinitionLocation = { path: 'alpha/impl.py', kind: 'method', name: 'Runner', line: 2, end_line: 3 }
// Server order, deliberately not alphabetical by path, name or line.
const CANDIDATES: DefinitionLocation[] = [
  { path: 'zeta/impl.py', kind: 'class', name: 'Runner', line: 30, end_line: 40 },
  { path: 'alpha/impl.py', kind: 'function', name: 'Runner', line: 5, end_line: 9 },
  METHOD,
]
const CANDIDATE_LABELS = [
  'Find references to class Runner (zeta/impl.py, line 30)',
  'Find references to function Runner (alpha/impl.py, line 5)',
  'Find references to method Runner (alpha/impl.py, line 2)',
]

// Server order deliberately unsorted; every path differs from every name, every line from its column.
const USES: ReferenceLocation[] = [
  { path: 'tests/test_calc.py', line: 12, column: 5, name: 'run', context: 'name', ambiguous: true },
  { path: 'app/main.py', line: 3, column: 10, name: 'compute', context: 'name', ambiguous: false },
  { path: 'app/api.py', line: 1, column: 22, name: 'compute', context: 'import', ambiguous: false },
  { path: 'app/api.py', line: 9, column: 14, name: 'compute', context: 'attribute', ambiguous: false },
]
const USE_TEXTS = [
  'tests/test_calc.py line 12, column 5 run name ambiguous',
  'app/main.py line 3, column 10 compute name',
  'app/api.py line 1, column 22 compute import',
  'app/api.py line 9, column 14 compute attribute',
]
// Every count distinct, so a swapped field is visible.
const SUMMARY: ReferencesSummary = {
  files_scanned: 12,
  too_large: 6,
  binary: 2,
  syntax_error: 3,
  references: 4,
  ambiguous: 1,
  unsupported: 5,
}
const ZERO: ReferencesSummary = {
  files_scanned: 0,
  too_large: 0,
  binary: 0,
  syntax_error: 0,
  references: 0,
  ambiguous: 0,
  unsupported: 0,
}

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

function referencesResult(extra: Partial<CodeReferencesResult> = {}): CodeReferencesResult {
  return { status: 'scanned', target: COMPUTE, references: USES, summary: SUMMARY, truncated: false, ...extra }
}

/** A result for `target` whose only reference lives in `file`. */
function onlyIn(target: DefinitionLocation, file: string): CodeReferencesResult {
  return referencesResult({
    target,
    references: [{ path: file, line: 4, column: 2, name: target.name, context: 'name', ambiguous: false }],
    summary: { ...SUMMARY, references: 1, ambiguous: 0 },
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
type RefParams = { path: string; line: number }

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))
const codeText = () => document.querySelector('pre code')?.textContent
const definitionButton = () =>
  within(screen.getByRole('region', { name: 'Go to definition' })).getByRole('button', {
    name: /definition/i,
  }) as HTMLButtonElement
const definitionList = () => screen.findByRole('list', { name: 'Definitions' })
const referencesRegion = () => screen.getByRole('region', { name: 'Find references' })
const findButtons = () => within(referencesRegion()).getAllByRole('button') as HTMLButtonElement[]
const findButton = (name: string | RegExp) =>
  within(referencesRegion()).getByRole('button', { name }) as HTMLButtonElement
const referenceList = () => screen.findByRole('list', { name: 'References' })
const referenceTexts = () => screen.getAllByTestId('reference').map((row) => row.textContent)
const targetText = () => screen.getByTestId('references-target').textContent
const summaryText = () => screen.getByTestId('references-summary').textContent
const byUrl = (requests: RecordedRequest[], url: string) => requests.filter((r) => r.url === url)

/** A real browser caret on the first character of `needle` in the displayed code. */
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

/** Opens main.py, runs Go to definition and records the params of every references request. */
async function showDefinition(
  definition: Handler = { status: 200, data: definitionResult() },
  references: Handler = { status: 200, data: referencesResult() },
) {
  const seen: RefParams[] = []
  const view = renderViewer('app/main.py', {
    [GET_CONTENT]: content('app/main.py', MAIN),
    [GET_DEFINITION]: definition,
    [GET_REFERENCES]: (config) => {
      seen.push({ ...(config.params as RefParams) })
      return typeof references === 'function' ? references(config) : references
    },
  })
  await waitFor(() => expect(codeText()).toBe(MAIN))
  caretOn(MAIN, 'compute')
  fireEvent.click(definitionButton())
  await definitionList()
  return { seen, ...view }
}

/** Shows a definition, clicks its Find references button and waits for the outcome. */
async function showReferences(references: Handler, label = COMPUTE_LABEL) {
  const view = await showDefinition(undefined, references)
  fireEvent.click(findButton(label))
  await waitFor(() =>
    expect(screen.queryByTestId('references-target') ?? screen.queryByRole('alert')).not.toBeNull(),
  )
  return view
}

/** Renders the panel alone (no viewer) and records the params of every references request. */
function renderPanel(
  definitions: DefinitionLocation[] | null,
  references: Handler = { status: 200, data: referencesResult() },
  onSelectLine?: (path: string, line: number) => void,
) {
  const seen: RefParams[] = []
  const requests = mockApi({
    [GET_REFERENCES]: (config) => {
      seen.push({ ...(config.params as RefParams) })
      return typeof references === 'function' ? references(config) : references
    },
  })
  const view = render(
    <QueryClientProvider client={createTestQueryClient()}>
      <CodeReferences projectId={P} repositoryId={R} definitions={definitions} onSelectLine={onSelectLine} />
    </QueryClientProvider>,
  )
  return { seen, requests, ...view }
}

/** Holds every references request matching `when` until released; others go through. */
function holdReferences(when: (params: RefParams) => boolean = () => true) {
  const held: { params: RefParams; release: (data: CodeReferencesResult) => void }[] = []
  const realGet = apiClient.get.bind(apiClient)
  const spy = vi.spyOn(apiClient, 'get').mockImplementation(((url: string, config?: AxiosRequestConfig) => {
    const params = config?.params as RefParams | undefined
    if (url === REFERENCES_PATH && params && when(params)) {
      return new Promise<unknown>((resolve) => {
        held.push({ params: { ...params }, release: (data) => resolve({ data }) })
      })
    }
    return realGet(url, config)
  }) as typeof apiClient.get)
  const calls = () => spy.mock.calls.filter(([url]) => url === REFERENCES_PATH).length
  return { held, spy, calls }
}

afterEach(() => {
  document.getSelection()?.removeAllRanges()
})

// --- Visibility and requests ----------------------------------------------------------------

describe('Find references visibility and requests', () => {
  it('requests nothing on file load, selection or before a definition result', async () => {
    const { requests } = renderViewer('app/main.py', {
      [GET_CONTENT]: content('app/main.py', MAIN),
      [GET_REFERENCES]: { status: 200, data: referencesResult() },
    })
    await waitFor(() => expect(codeText()).toBe(MAIN))
    caretOn(MAIN, 'compute')
    fireEvent.mouseUp(document.querySelector('pre') as HTMLPreElement)
    fireEvent.keyUp(document.querySelector('pre') as HTMLPreElement, { key: 'ArrowRight' })
    await sleep(50)
    expect(screen.queryByRole('region', { name: 'Find references' })).toBeNull()
    expect(byUrl(requests, REFERENCES_PATH)).toHaveLength(0)
  })

  it('offers one action after a resolved result and requests nothing until clicked', async () => {
    const { requests, onSelectLine } = await showDefinition()
    expect(findButtons().map((b) => b.textContent)).toEqual([COMPUTE_LABEL])
    expect(screen.queryByRole('list', { name: 'References' })).toBeNull()
    expect(screen.queryByTestId('references-target')).toBeNull()
    caretOn(MAIN, 'value') // a new selection, mouseup and keyup while the panel exists
    fireEvent.mouseUp(document.querySelector('pre') as HTMLPreElement)
    fireEvent.keyUp(document.querySelector('pre') as HTMLPreElement, { key: 'ArrowLeft' })
    fireEvent.click(screen.getByRole('button', { name: 'Open app/calc.py at line 7 (function compute)' }))
    expect(onSelectLine).toHaveBeenCalledWith('app/calc.py', 7)
    await sleep(50)
    expect(byUrl(requests, REFERENCES_PATH)).toHaveLength(0)
  })

  it('sends exactly the definition path and start line as one GET', async () => {
    const { seen, requests } = await showDefinition()
    fireEvent.click(findButton(COMPUTE_LABEL))
    await referenceList()
    const sent = byUrl(requests, REFERENCES_PATH)
    expect(sent).toHaveLength(1)
    expect(sent[0].method).toBe('GET')
    expect(sent[0].data).toBeUndefined()
    // Only the path and the start line: no column, kind, name, end_line or other parameter.
    expect(seen).toEqual([{ path: 'app/calc.py', line: 7 }])
    await sleep(100)
    expect(byUrl(requests, REFERENCES_PATH)).toHaveLength(1) // no polling
  })

  it('sends an unusual server path unchanged', async () => {
    const odd: DefinitionLocation = { path: ' pkg/My Dir/../x%20y.py ', kind: 'function', name: 'run', line: 4, end_line: 6 }
    const { seen } = renderPanel([odd])
    fireEvent.click(screen.getByRole('button', { name: /^Find references/ }))
    await referenceList()
    expect(seen).toEqual([{ path: ' pkg/My Dir/../x%20y.py ', line: 4 }])
  })

  it('offers no action for module definitions', async () => {
    const { requests } = await showDefinition({ status: 200, data: definitionResult({ definitions: [MODULE] }) })
    expect(screen.queryByRole('region', { name: 'Find references' })).toBeNull()
    expect(screen.queryByRole('button', { name: /^Find references/ })).toBeNull()
    expect(screen.getAllByTestId('definition')).toHaveLength(1)
    await sleep(50)
    expect(byUrl(requests, REFERENCES_PATH)).toHaveLength(0)
    renderPanel([CANDIDATES[0], MODULE, METHOD])
    expect(screen.getAllByRole('button', { name: /^Find references/ }).map((b) => b.textContent)).toEqual([
      CANDIDATE_LABELS[0],
      CANDIDATE_LABELS[2],
    ])
  })

  it('ambiguous: one action per definition in server order, each sent only by its own click', async () => {
    const { seen } = await showDefinition({ status: 200, data: AMBIGUOUS })
    expect(findButtons().map((b) => b.textContent)).toEqual(CANDIDATE_LABELS)
    await sleep(50)
    expect(seen).toEqual([])
    fireEvent.click(findButton(CANDIDATE_LABELS[1]))
    await referenceList()
    expect(seen).toEqual([{ path: 'alpha/impl.py', line: 5 }])
    fireEvent.click(findButton(CANDIDATE_LABELS[2]))
    await waitFor(() => expect(seen).toHaveLength(2))
    expect(seen[1]).toEqual({ path: 'alpha/impl.py', line: 2 })
  })

  it('labels only the exact path and line as pending and disables every button meanwhile', async () => {
    const beta: DefinitionLocation = { path: 'beta/impl.py', kind: 'function', name: 'Runner', line: 5, end_line: 6 }
    const hold = holdReferences()
    try {
      renderPanel([...CANDIDATES, beta])
      fireEvent.click(screen.getByRole('button', { name: CANDIDATE_LABELS[1] }))
      await screen.findByRole('button', { name: 'Finding references to function Runner (alpha/impl.py, line 5)…' })
      const buttons = screen.getAllByRole('button') as HTMLButtonElement[]
      expect(buttons.map((b) => b.textContent)).toEqual([
        CANDIDATE_LABELS[0],
        'Finding references to function Runner (alpha/impl.py, line 5)…',
        CANDIDATE_LABELS[2], // same path, another line
        'Find references to function Runner (beta/impl.py, line 5)', // same line, another path
      ])
      expect(buttons.map((b) => b.disabled)).toEqual([true, true, true, true])
      fireEvent.click(buttons[0])
      fireEvent.click(buttons[2])
      await sleep(20)
      expect(hold.calls()).toBe(1) // disabled: nothing else is sent
      expect(hold.held).toHaveLength(1)
      hold.held[0].release(onlyIn(CANDIDATES[1], 'uses/b.py'))
      await screen.findByRole('list', { name: 'References' })
      const after = screen.getAllByRole('button', { name: /^Find references/ }) as HTMLButtonElement[]
      expect(after.map((b) => b.disabled)).toEqual([false, false, false, false])
      expect(after[1].textContent).toBe(CANDIDATE_LABELS[1])
    } finally {
      hold.spy.mockRestore()
    }
  })
})

// --- Results ---------------------------------------------------------------------------------

describe('Find references results', () => {
  it('shows references in server order with every field, the server target and the summary', async () => {
    await showReferences({ status: 200, data: referencesResult() })
    expect(referenceTexts()).toEqual(USE_TEXTS)
    expect(targetText()).toBe('Results for function compute (app/calc.py, line 7)')
    expect(summaryText()).toBe('4 references · 1 ambiguous · 12 files scanned.')
    expect(screen.queryByText(TRUNCATED_NOTICE)).toBeNull()
    expect(screen.queryByText(NO_REFERENCES)).toBeNull()
    expect(screen.getByRole('list', { name: 'Definitions' })).toBeTruthy()
  })

  it('navigates to the exact reference path and line, never the column, only when clicked', async () => {
    const { onSelectLine } = await showReferences({ status: 200, data: referencesResult() })
    expect(onSelectLine).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Go to app/main.py at line 3 (name compute)' }))
    fireEvent.click(screen.getByRole('button', { name: 'Go to tests/test_calc.py at line 12 (name run, ambiguous)' }))
    fireEvent.click(screen.getByRole('button', { name: 'Go to app/api.py at line 9 (attribute compute)' }))
    expect(onSelectLine.mock.calls).toEqual([
      ['app/main.py', 3],
      ['tests/test_calc.py', 12],
      ['app/api.py', 9],
    ])
  })

  it('keeps duplicate references as returned', async () => {
    await showReferences({
      status: 200,
      data: referencesResult({ references: [USES[1], USES[1]], summary: { ...SUMMARY, references: 2, ambiguous: 0 } }),
    })
    expect(referenceTexts()).toEqual([USE_TEXTS[1], USE_TEXTS[1]])
  })

  it('shows "No references found." with the summary for an empty scan', async () => {
    await showReferences({
      status: 200,
      data: referencesResult({ references: [], summary: { ...ZERO, files_scanned: 12 } }),
    })
    expect(screen.getByText('No references found.')).toBeTruthy()
    expect(screen.queryByRole('list', { name: 'References' })).toBeNull()
    expect(summaryText()).toBe('0 references · 0 ambiguous · 12 files scanned.')
    expect(targetText()).toBe('Results for function compute (app/calc.py, line 7)')
  })

  it('shows the truncated notice and the server counts, not the rows shown', async () => {
    await showReferences({
      status: 200,
      data: referencesResult({ truncated: true, summary: { ...SUMMARY, references: 650, ambiguous: 7, files_scanned: 40 } }),
    })
    expect(screen.getByText(TRUNCATED_NOTICE)).toBeTruthy()
    expect(summaryText()).toBe('650 references · 7 ambiguous · 40 files scanned.')
    expect(referenceTexts()).toHaveLength(4)
  })

  it('shows the truncated notice with an unavailable status too', async () => {
    renderPanel([COMPUTE], {
      status: 200,
      data: { status: 'syntax_error', target: null, references: [], summary: ZERO, truncated: true },
    })
    fireEvent.click(screen.getByRole('button', { name: COMPUTE_LABEL }))
    expect(await screen.findByText(STATUS_MESSAGES.syntax_error)).toBeTruthy()
    expect(screen.getByText(TRUNCATED_NOTICE)).toBeTruthy()
  })

  it.each([
    ['not_a_definition', COMPUTE, null, 'Find references is unavailable: no class or function starts on this line.'],
    ['unsupported_target', METHOD, METHOD, 'Find references does not support methods or nested classes.'],
    ['syntax_error', COMPUTE, null, 'Find references is unavailable: the target file could not be parsed as Python.'],
    ['too_large', COMPUTE, null, 'Find references is unavailable: the target file is too large to analyze.'],
    ['binary', COMPUTE, null, 'Find references is unavailable: the target file is not text.'],
    ['unsupported_language', COMPUTE, null, 'Find references is only available for Python files.'],
    ['something_new', COMPUTE, COMPUTE, UNKNOWN_STATUS],
  ])('shows %s as plain text with nothing to navigate to', async (status, clicked, target, message) => {
    const onSelectLine = vi.fn()
    const payload = {
      status,
      target,
      // Even references in an unknown payload are never shown outside a scan.
      references: status === 'something_new' ? USES : [],
      summary: status === 'something_new' ? SUMMARY : ZERO,
      truncated: false,
    }
    renderPanel([clicked], { status: 200, data: payload }, onSelectLine)
    fireEvent.click(screen.getByRole('button', { name: /^Find references/ }))
    expect(await screen.findByText(message)).toBeTruthy()
    const kind = clicked === METHOD ? 'method Runner (alpha/impl.py, line 2)' : 'function compute (app/calc.py, line 7)'
    expect(targetText()).toBe(`Results for ${kind}`)
    expect(screen.queryByRole('list', { name: 'References' })).toBeNull()
    expect(screen.queryByTestId('reference')).toBeNull()
    expect(screen.queryByText(NO_REFERENCES)).toBeNull()
    expect(screen.queryByTestId('references-summary')).toBeNull()
    expect(screen.queryByRole('button', { name: /^Go to/ })).toBeNull()
    expect(onSelectLine).not.toHaveBeenCalled()
  })

  it('falls back to the clicked definition, not the first one, when the server reports no target', async () => {
    renderPanel(CANDIDATES, {
      status: 200,
      data: { status: 'not_a_definition', target: null, references: [], summary: ZERO, truncated: false },
    })
    fireEvent.click(screen.getByRole('button', { name: CANDIDATE_LABELS[1] }))
    expect(await screen.findByText(STATUS_MESSAGES.not_a_definition)).toBeTruthy()
    expect(targetText()).toBe('Results for function Runner (alpha/impl.py, line 5)')
  })

  it('requests again when the same definition is clicked again and shows the new result', async () => {
    let call = 0
    const { seen } = await showReferences(() => {
      call += 1
      return { status: 200, data: call === 1 ? referencesResult() : onlyIn(COMPUTE, 'uses/second.py') }
    })
    await referenceList()
    expect(referenceTexts()).toEqual(USE_TEXTS)
    fireEvent.click(findButton(COMPUTE_LABEL))
    await screen.findByText('uses/second.py')
    expect(seen).toEqual([
      { path: 'app/calc.py', line: 7 },
      { path: 'app/calc.py', line: 7 },
    ])
    expect(referenceTexts()).toEqual(['uses/second.py line 4, column 2 compute name'])
  })

  it('labels the results with the server target even when it differs from the clicked one', async () => {
    const changed: DefinitionLocation = { path: 'app/calc.py', kind: 'class', name: 'Calculator', line: 7, end_line: 9 }
    await showReferences({ status: 200, data: referencesResult({ target: changed }) })
    expect(targetText()).toBe('Results for class Calculator (app/calc.py, line 7)')
  })
})

// --- Errors ------------------------------------------------------------------------------------

describe('Find references errors', () => {
  it.each([
    [404, 'Repository not found'],
    [409, 'Repository changed during analysis; run it again'],
    [429, 'Too many attempts; please try again later'],
  ])('shows the server detail for %i exactly, without retrying', async (status, detail) => {
    const { requests } = await showReferences({ status, data: { detail } })
    const alerts = await screen.findAllByRole('alert')
    expect(alerts.map((a) => a.textContent)).toEqual([detail])
    await sleep(50)
    expect(byUrl(requests, REFERENCES_PATH)).toHaveLength(1)
    expect(screen.queryByRole('list', { name: 'References' })).toBeNull()
    expect(screen.getByRole('list', { name: 'Definitions' })).toBeTruthy()
  })

  it('joins the validation messages for 422', async () => {
    await showReferences({
      status: 422,
      data: { detail: [{ msg: 'Input should be greater than or equal to 1' }, { msg: 'Field required' }] },
    })
    expect((await screen.findByRole('alert')).textContent).toBe(
      'Input should be greater than or equal to 1; Field required',
    )
  })

  it('uses the existing network message without retrying', async () => {
    const { requests } = await showReferences(() => {
      throw new AxiosError('Network Error', AxiosError.ERR_NETWORK)
    })
    expect((await screen.findByRole('alert')).textContent).toBe('Could not reach the server')
    await sleep(50)
    expect(byUrl(requests, REFERENCES_PATH)).toHaveLength(1)
  })

  it('shows the fallback for an error without a usable detail', async () => {
    await showReferences({ status: 500, data: {} })
    expect((await screen.findByRole('alert')).textContent).toBe('Could not find the references')
  })

  it('clears an error after a successful request', async () => {
    let call = 0
    await showReferences(() => {
      call += 1
      return call === 1
        ? { status: 429, data: { detail: 'Too many attempts; please try again later' } }
        : { status: 200, data: referencesResult() }
    })
    await screen.findByRole('alert')
    fireEvent.click(findButton(COMPUTE_LABEL))
    await referenceList()
    expect(screen.queryByRole('alert')).toBeNull()
  })
})

// --- Stale results -------------------------------------------------------------------------

describe('Find references never shows a stale result', () => {
  it('switching definitions shows only the new target and its references', async () => {
    const hold = holdReferences((params) => params.path === 'alpha/impl.py')
    try {
      await showDefinition({ status: 200, data: AMBIGUOUS }, { status: 200, data: onlyIn(CANDIDATES[0], 'uses/a.py') })
      fireEvent.click(findButton(CANDIDATE_LABELS[0]))
      await screen.findByText('uses/a.py')
      expect(targetText()).toBe('Results for class Runner (zeta/impl.py, line 30)')
      fireEvent.click(findButton(CANDIDATE_LABELS[1]))
      await screen.findByRole('button', { name: 'Finding references to function Runner (alpha/impl.py, line 5)…' })
      await waitFor(() => expect(screen.queryByText('uses/a.py')).toBeNull())
      expect(screen.queryByTestId('references-target')).toBeNull()
      hold.held[0].release(onlyIn(CANDIDATES[1], 'uses/b.py'))
      await screen.findByText('uses/b.py')
      expect(targetText()).toBe('Results for function Runner (alpha/impl.py, line 5)')
      expect(screen.queryByText('uses/a.py', { exact: false })).toBeNull()
    } finally {
      hold.spy.mockRestore()
    }
  })

  it('a new go-to-definition lookup drops a references request that is still loading', async () => {
    const hold = holdReferences()
    try {
      let lookups = 0
      await showDefinition(() => {
        lookups += 1
        return { status: 200, data: lookups === 1 ? definitionResult() : AMBIGUOUS }
      })
      fireEvent.click(findButton(COMPUTE_LABEL))
      await screen.findByRole('button', { name: `Finding references to function compute (app/calc.py, line 7)…` })
      caretOn(MAIN, 'value')
      fireEvent.click(definitionButton())
      await screen.findByText('3 possible definitions of', { exact: false })
      expect(findButtons().map((b) => [b.textContent, b.disabled])).toEqual(
        CANDIDATE_LABELS.map((label) => [label, false]),
      )
      hold.held[0].release(onlyIn(COMPUTE, 'uses/a.py'))
      await sleep(50)
      expect(screen.queryByRole('list', { name: 'References' })).toBeNull()
      expect(screen.queryByTestId('references-target')).toBeNull()
      expect(screen.queryByText('uses/a.py', { exact: false })).toBeNull()
    } finally {
      hold.spy.mockRestore()
    }
  })

  it('a new lookup clears the shown references, even with an identical result', async () => {
    const { requests } = await showReferences({ status: 200, data: referencesResult() })
    await referenceList()
    fireEvent.click(definitionButton())
    await waitFor(() => expect(byUrl(requests, DEFINITION_PATH)).toHaveLength(2))
    await definitionList()
    await sleep(50)
    expect(findButtons().map((b) => [b.textContent, b.disabled])).toEqual([[COMPUTE_LABEL, false]])
    expect(screen.queryByRole('list', { name: 'References' })).toBeNull()
    expect(screen.queryByTestId('references-target')).toBeNull()
    expect(screen.queryByTestId('references-summary')).toBeNull()
    expect(byUrl(requests, REFERENCES_PATH)).toHaveLength(1)
  })

  it('a click without a position keeps the result and its references', async () => {
    const { requests } = await showReferences({ status: 200, data: referencesResult() })
    await referenceList()
    document.getSelection()?.removeAllRanges()
    fireEvent.click(definitionButton())
    expect(screen.getByText(NO_POSITION)).toBeTruthy()
    await sleep(50)
    expect(screen.getByRole('list', { name: 'Definitions' })).toBeTruthy()
    expect(referenceTexts()).toEqual(USE_TEXTS)
    expect(byUrl(requests, DEFINITION_PATH)).toHaveLength(1)
    expect(byUrl(requests, REFERENCES_PATH)).toHaveLength(1)
  })

  // other.py is opened first so its content is cached: switching back to it renders the
  // viewer immediately (no loading state), which is when the per-path key matters.
  const contentRoute: Handler = (config) => {
    const path = (config.params as { path: string }).path
    return content(path, path === 'app/main.py' ? MAIN : OTHER)
  }

  it('never shows references for a file that is no longer open, even a loaded one', async () => {
    const hold = holdReferences()
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
      fireEvent.click(findButton(COMPUTE_LABEL))
      await screen.findByRole('button', { name: /^Finding references/ })
      rerenderPath('app/other.py')
      await waitFor(() => expect(codeText()).toBe(OTHER))
      hold.held[0].release(referencesResult())
      await sleep(50)
      expect(screen.queryByRole('region', { name: 'Find references' })).toBeNull()
      expect(screen.queryByRole('list', { name: 'References' })).toBeNull()
      expect(screen.queryByText('tests/test_calc.py', { exact: false })).toBeNull()
      expect(definitionButton().textContent).toBe('Go to definition')
    } finally {
      hold.spy.mockRestore()
    }
  })

  it('starts fresh in another loaded file, with no request on selection and no cache', async () => {
    const { rerenderPath, requests } = renderViewer('app/other.py', {
      [GET_CONTENT]: contentRoute,
      [GET_DEFINITION]: { status: 200, data: definitionResult() },
      [GET_REFERENCES]: { status: 200, data: referencesResult() },
    })
    await waitFor(() => expect(codeText()).toBe(OTHER))
    rerenderPath('app/main.py')
    await waitFor(() => expect(codeText()).toBe(MAIN))
    caretOn(MAIN, 'compute')
    fireEvent.click(definitionButton())
    await definitionList()
    fireEvent.click(findButton(COMPUTE_LABEL))
    await referenceList()
    rerenderPath('app/other.py')
    await waitFor(() => expect(codeText()).toBe(OTHER))
    expect(screen.queryByRole('region', { name: 'Find references' })).toBeNull()
    expect(screen.queryByRole('list', { name: 'References' })).toBeNull()
    rerenderPath('app/main.py')
    await waitFor(() => expect(codeText()).toBe(MAIN))
    caretOn(MAIN, 'compute')
    fireEvent.click(definitionButton())
    await definitionList()
    await sleep(50)
    expect(findButtons().map((b) => b.textContent)).toEqual([COMPUTE_LABEL])
    expect(screen.queryByRole('list', { name: 'References' })).toBeNull()
    expect(byUrl(requests, REFERENCES_PATH)).toHaveLength(1)
  })
})

// --- Security ------------------------------------------------------------------------------

describe('Find references security', () => {
  it('renders server text as plain text, never as HTML', async () => {
    const hostile: DefinitionLocation = { path: `${HTML}.py`, kind: 'class', name: HTML, line: 3, end_line: 4 }
    renderPanel([hostile], {
      status: 200,
      data: referencesResult({
        target: hostile,
        references: [{ path: `${HTML}.py`, line: 5, column: 6, name: HTML, context: 'name', ambiguous: true }],
      }),
    })
    fireEvent.click(screen.getByRole('button', { name: /^Find references/ }))
    await referenceList()
    expect(referenceTexts()).toEqual([`${HTML}.py line 5, column 6 ${HTML} name ambiguous`])
    expect(targetText()).toBe(`Results for class ${HTML} (${HTML}.py, line 3)`)
    expect(document.querySelector('img, script, a, iframe')).toBeNull()
    expect(document.querySelector('[href], [src], [onerror]')).toBeNull()
  })

  it('shows only the named fields of the response', async () => {
    const extra = { snippet: 'FAKE-SNIPPET-MARKER', sha256: 'FAKE-SHA-MARKER', id: 'FAKE-ID-MARKER' }
    const payload = {
      ...referencesResult({ references: USES.map((use) => ({ ...use, ...extra })) as ReferenceLocation[] }),
      target: { ...COMPUTE, ...extra },
      summary: { ...SUMMARY, ...extra },
      ...extra,
    }
    renderPanel([COMPUTE], { status: 200, data: payload }, vi.fn())
    fireEvent.click(screen.getByRole('button', { name: COMPUTE_LABEL }))
    await referenceList()
    const region = referencesRegion()
    const text = region.textContent ?? ''
    const labels = Array.from(region.querySelectorAll('[aria-label]')).map((e) => e.getAttribute('aria-label'))
    for (const marker of Object.values(extra)) {
      expect(text).not.toContain(marker)
      expect(labels.join(' ')).not.toContain(marker)
    }
    expect(text).not.toContain('{')
    expect(text).not.toContain('"status"')
    expect(text).not.toContain('end_line')
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
    const xhrOpen = vi.spyOn(XMLHttpRequest.prototype, 'open')
    const realFetch = globalThis.fetch
    const fetchSpy = vi.fn()
    globalThis.fetch = fetchSpy as unknown as typeof fetch
    try {
      const { requests } = await showReferences({ status: 200, data: referencesResult() })
      fireEvent.click(screen.getByRole('button', { name: 'Go to app/main.py at line 3 (name compute)' }))
      expect([localStorage.length, sessionStorage.length]).toEqual(stored)
      expect(xhrOpen).not.toHaveBeenCalled() // every request went through the apiClient
      expect(fetchSpy).not.toHaveBeenCalled()
      // The only reads are the existing apiClient's access token lookups (services/session.ts).
      expect(new Set(getItem.mock.calls.map((call) => call[0]))).toEqual(new Set(['ai_se_access_token']))
      for (const spy of writes) {
        expect(spy).not.toHaveBeenCalled()
      }
      expect(cookieGet).not.toHaveBeenCalled()
      expect(cookieSet).not.toHaveBeenCalled()
      expect(requests.map((r) => `${r.method} ${r.url}`)).toEqual([GET_CONTENT, GET_DEFINITION, GET_REFERENCES])
      expect(requests.every((r) => r.baseURL === '/api/v1')).toBe(true)
    } finally {
      for (const spy of [getItem, ...writes, xhrOpen]) {
        spy.mockRestore()
      }
      globalThis.fetch = realFetch
      delete (document as { cookie?: unknown }).cookie // back to the prototype accessor
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

  it('without onSelectLine, references are plain text', async () => {
    renderPanel([COMPUTE])
    fireEvent.click(screen.getByRole('button', { name: COMPUTE_LABEL }))
    await referenceList()
    expect(referenceTexts()).toEqual(USE_TEXTS)
    expect(screen.queryAllByRole('button', { name: /^Go to/ })).toHaveLength(0)
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
const PAGE_REFERENCES = referencesResult({
  references: [
    { path: 'app/calc.py', line: 12, column: 10, name: 'compute', context: 'name', ambiguous: false },
    { path: 'app/main.py', line: 3, column: 10, name: 'compute', context: 'name', ambiguous: false },
  ],
  summary: { ...SUMMARY, references: 2, ambiguous: 0 },
})

const scrollTopSpy = vi.fn()
const scrollIntoViewSpy = vi.fn()
const windowScrollSpy = vi.fn()

describe('RepositoryPage find references', () => {
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
    vi.spyOn(window, 'scrollTo').mockImplementation(windowScrollSpy)
    vi.spyOn(window, 'scrollBy').mockImplementation(windowScrollSpy)
  })

  afterEach(() => {
    vi.mocked(window.scrollTo).mockRestore()
    vi.mocked(window.scrollBy).mockRestore()
    windowScrollSpy.mockReset()
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
      [GET_DEFINITION]: { status: 200, data: definitionResult() },
      [GET_REFERENCES]: { status: 200, data: PAGE_REFERENCES },
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

  async function openMainAndFindReferences(requests: RecordedRequest[]) {
    await screen.findByRole('table', { name: 'Secret scan summary' }) // page settled
    const tree = within(await screen.findByRole('navigation', { name: 'File tree' }))
    fireEvent.click(await tree.findByRole('button', { name: /main\.py/ }))
    await waitFor(() => expect(codeText()).toBe(MAIN))
    await sleep(50)
    expect(byUrl(requests, REFERENCES_PATH)).toHaveLength(0) // not on page mount or file selection
    caretOn(MAIN, 'compute')
    fireEvent.click(definitionButton())
    await definitionList()
    fireEvent.click(findButton(COMPUTE_LABEL))
    await referenceList()
  }

  it('opens a reference in another file at its line through the existing line selection', async () => {
    const { contentPaths, requests } = renderPage()
    await openMainAndFindReferences(requests)
    fireEvent.click(screen.getByRole('button', { name: 'Go to app/calc.py at line 12 (name compute)' }))
    await waitFor(() => expect(codeText()).toBe(CALC))
    expect(contentPaths).toEqual(['app/main.py', 'app/calc.py'])
    expect(screen.getByTestId('selected-line').textContent).toBe('answer = compute(2)')
    expect(screen.getByText('Showing line 12')).toBeTruthy()
    expect(scrollTopSpy).toHaveBeenCalled()
    expect(scrollIntoViewSpy).not.toHaveBeenCalled() // the viewer's own line selection
    expect(windowScrollSpy).not.toHaveBeenCalled()
    await sleep(50)
    expect(byUrl(requests, DEFINITION_PATH)).toHaveLength(1)
    expect(byUrl(requests, REFERENCES_PATH)).toHaveLength(1)
    expect(screen.queryByRole('list', { name: 'References' })).toBeNull() // the new file starts fresh
  })

  it('keeps the references when a reference in the open file is opened', async () => {
    const { contentPaths, requests } = renderPage()
    await openMainAndFindReferences(requests)
    fireEvent.click(screen.getByRole('button', { name: 'Go to app/main.py at line 3 (name compute)' }))
    await waitFor(() => expect(screen.getByTestId('selected-line').textContent).toBe('result = compute(1)'))
    expect(screen.getByText('Showing line 3')).toBeTruthy()
    expect(screen.getByRole('list', { name: 'References' })).toBeTruthy()
    expect(contentPaths).toEqual(['app/main.py'])
    expect(scrollIntoViewSpy).not.toHaveBeenCalled()
    expect(windowScrollSpy).not.toHaveBeenCalled()
    await sleep(50)
    expect(byUrl(requests, DEFINITION_PATH)).toHaveLength(1)
    expect(byUrl(requests, REFERENCES_PATH)).toHaveLength(1)
  })
})
