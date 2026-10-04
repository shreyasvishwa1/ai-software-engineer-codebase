import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { AxiosError, type AxiosRequestConfig, type InternalAxiosRequestConfig } from 'axios'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { apiClient } from '../services/client'
import { mockApi, type MockResponse, PROJECT, type RecordedRequest, ZIP_REPOSITORY } from '../test/mockApi'
import { createTestQueryClient } from '../test/render'
import type {
  BaseKind,
  CodeDefinitionResult,
  DefinitionLocation,
  FileContent,
  MethodOverridesResult,
  MethodOverridesSummary,
  OverriddenEntry,
  OverridingEntry,
} from '../types/api'
import {
  CodeMethodOverrides,
  MEMBER_MESSAGES,
  NO_BASES,
  NO_OVERRIDES,
  OVERRIDDEN_TRUNCATED,
  OVERRIDES_UNSUPPORTED,
  OVERRIDING_TRUNCATED,
  SEVERAL_DEFINITIONS,
  STATUS_MESSAGES,
  UNKNOWN_STATUS,
} from './CodeMethodOverrides'
import { KIND_MESSAGES } from './CodeTypeHierarchy'
import { FileViewer } from './FileViewer'

const P = PROJECT.id
const R = ZIP_REPOSITORY.id
const BASE = `/projects/${P}/repositories/${R}`
const DEFINITION_PATH = `${BASE}/files/definition`
const GET_DEFINITION = `GET ${DEFINITION_PATH}`
const OVERRIDES_PATH = `${BASE}/files/method-overrides`
const GET_OVERRIDES = `GET ${OVERRIDES_PATH}`
const GET_CONTENT = `GET ${BASE}/files/content`
const HTML = '<img src=x onerror=alert(1)>'

const MAIN = 'from shapes.square import Square\n\nshape = Square()\nshape.area()\n'
const OTHER = 'import json\n'

// The method spans lines 5-6 of a class whose bases are written on line 3.
const AREA: DefinitionLocation = { path: 'shapes/square.py', kind: 'method', name: 'area', line: 5, end_line: 6 }
const AREA_LABEL = 'Method overrides for method area (shapes/square.py, line 5)'
const SQUARE: DefinitionLocation = { path: 'shapes/square.py', kind: 'class', name: 'Square', line: 3, end_line: 20 }
const ZETA: DefinitionLocation = { path: 'zeta/impl.py', kind: 'class', name: 'Runner', line: 30, end_line: 40 }
const FUNCTION: DefinitionLocation = { path: 'alpha/func.py', kind: 'function', name: 'Runner', line: 5, end_line: 9 }
const METHOD_A: DefinitionLocation = { path: 'alpha/impl.py', kind: 'method', name: 'Runner', line: 2, end_line: 3 }
const MODULE: DefinitionLocation = { path: 'pkg/runner.py', kind: 'module', name: 'runner', line: 1, end_line: 1 }
const METHOD_B: DefinitionLocation = { path: 'beta/impl.py', kind: 'method', name: 'Runner', line: 14, end_line: 16 }
// Server order, deliberately not sorted by path, kind, name or line.
const CANDIDATES: DefinitionLocation[] = [ZETA, FUNCTION, METHOD_A, MODULE, METHOD_B]
const CANDIDATE_LABELS = [
  'Method overrides for method Runner (alpha/impl.py, line 2)',
  'Method overrides for method Runner (beta/impl.py, line 14)',
]

const SHAPE: DefinitionLocation = { path: 'shapes/base.py', kind: 'class', name: 'Shape', line: 4, end_line: 26 }
const SHAPE_AREA: DefinitionLocation = { path: 'shapes/base.py', kind: 'method', name: 'area', line: 6, end_line: 7 }
const TILE_Z: DefinitionLocation = { path: 'zeta/tile.py', kind: 'class', name: 'Tile', line: 9, end_line: 30 }
const TILE_A: DefinitionLocation = { path: 'alpha/tile.py', kind: 'class', name: 'Tile', line: 1, end_line: 8 }
const Z_AREA_1: DefinitionLocation = { path: 'zeta/tile.py', kind: 'method', name: 'area', line: 12, end_line: 14 }
const Z_AREA_2: DefinitionLocation = { path: 'zeta/tile.py', kind: 'method', name: 'area', line: 20, end_line: 22 }
const NAMED: DefinitionLocation = { path: 'lib/named.py', kind: 'class', name: 'Named', line: 1, end_line: 5 }
const MIXIN: DefinitionLocation = { path: 'lib/mixin.py', kind: 'class', name: 'Mixin', line: 1, end_line: 9 }

function overridden(extra: Partial<OverriddenEntry>): OverriddenEntry {
  return {
    site: { line: 3, column: 14, name: 'Shape' },
    base_kind: 'resolved',
    class: SHAPE,
    ambiguous: false,
    status: 'defined',
    implementations: [SHAPE_AREA],
    implementations_ambiguous: false,
    ...extra,
  }
}

// Server order (bases, then candidates), never sorted: a duplicate base and every status.
const OVERRIDDEN: OverriddenEntry[] = [
  overridden({}),
  overridden({
    site: { line: 3, column: 21, name: 'Tile' },
    base_kind: 'ambiguous',
    class: TILE_Z,
    ambiguous: true,
    implementations: [Z_AREA_1, Z_AREA_2],
    implementations_ambiguous: true,
  }),
  overridden({
    site: { line: 3, column: 21, name: 'Tile' },
    base_kind: 'ambiguous',
    class: TILE_A,
    ambiguous: true,
    status: 'not_defined',
    implementations: [],
  }),
  overridden({ site: { line: 3, column: 27, name: 'Shape' } }),
  overridden({ site: { line: 3, column: 34, name: 'Named' }, class: NAMED, status: 'not_a_method', implementations: [] }),
  overridden({ site: { line: 3, column: 41, name: 'Mixin' }, class: MIXIN, status: 'unsupported', implementations: [] }),
  overridden({
    site: { line: 3, column: 48, name: 'object' },
    base_kind: 'builtin',
    class: null,
    status: 'unavailable',
    implementations: [],
  }),
  overridden({ site: null, base_kind: 'dynamic', class: null, status: 'unavailable', implementations: [] }),
]
// (site, base class, member status, reason, multiple-definitions note, implementations)
const OVERRIDDEN_ROWS = [
  ['Shape line 3, column 14', 'Shape class shapes/base.py lines 4–26', MEMBER_MESSAGES.defined, null, null, ['area method shapes/base.py lines 6–7']],
  [
    'Tile line 3, column 21',
    'Tile class zeta/tile.py lines 9–30 · ambiguous',
    MEMBER_MESSAGES.defined,
    null,
    SEVERAL_DEFINITIONS,
    ['area method zeta/tile.py lines 12–14', 'area method zeta/tile.py lines 20–22'],
  ],
  ['Tile line 3, column 21', 'Tile class alpha/tile.py lines 1–8 · ambiguous', MEMBER_MESSAGES.not_defined, null, null, []],
  ['Shape line 3, column 27', 'Shape class shapes/base.py lines 4–26', MEMBER_MESSAGES.defined, null, null, ['area method shapes/base.py lines 6–7']],
  ['Named line 3, column 34', 'Named class lib/named.py lines 1–5', MEMBER_MESSAGES.not_a_method, null, null, []],
  ['Mixin line 3, column 41', 'Mixin class lib/mixin.py lines 1–9', MEMBER_MESSAGES.unsupported, null, null, []],
  ['object line 3, column 48', null, MEMBER_MESSAGES.unavailable, 'Built-in class', null, []],
  ['Dynamic base expression', null, MEMBER_MESSAGES.unavailable, 'Dynamic base: the base class could not be resolved statically.', null, []],
]

const CUBE: DefinitionLocation = { path: 'app/cube.py', kind: 'class', name: 'Cube', line: 10, end_line: 20 }
const CUBE_AREA: DefinitionLocation = { path: 'app/cube.py', kind: 'method', name: 'area', line: 15, end_line: 16 }
const ALPHA: DefinitionLocation = { path: 'app/alpha.py', kind: 'class', name: 'Alpha', line: 2, end_line: 9 }
const BOX: DefinitionLocation = { path: 'lib/box.py', kind: 'class', name: 'Box', line: 1, end_line: 4 }
const PROP: DefinitionLocation = { path: 'app/prop.py', kind: 'class', name: 'Prop', line: 1, end_line: 30 }
const PROP_AREA: DefinitionLocation = { path: 'app/prop.py', kind: 'method', name: 'area', line: 20, end_line: 22 }
// Server order deliberately unsorted, with a duplicate subclass (it inherits Square twice).
const OVERRIDING: OverridingEntry[] = [
  { subclass: CUBE, site: { line: 11, column: 5, name: 'Square', ambiguous: false }, status: 'defined', implementations: [CUBE_AREA], implementations_ambiguous: false },
  { subclass: ALPHA, site: { line: 3, column: 7, name: 'Sq', ambiguous: true }, status: 'not_a_method', implementations: [], implementations_ambiguous: false },
  { subclass: CUBE, site: { line: 12, column: 5, name: 'Square', ambiguous: false }, status: 'defined', implementations: [CUBE_AREA], implementations_ambiguous: false },
  { subclass: BOX, site: { line: 1, column: 11, name: 'Square', ambiguous: false }, status: 'unsupported', implementations: [], implementations_ambiguous: false },
  // The server counted several definitions before cutting its list to one.
  { subclass: PROP, site: { line: 1, column: 12, name: 'Square', ambiguous: false }, status: 'defined', implementations: [PROP_AREA], implementations_ambiguous: true },
]
// (subclass, site, member status, multiple-definitions note, implementations)
const OVERRIDING_ROWS = [
  ['Cube class app/cube.py lines 10–20', 'Inherits at line 11, column 5', MEMBER_MESSAGES.defined, null, ['area method app/cube.py lines 15–16']],
  ['Alpha class app/alpha.py lines 2–9', 'Inherits at line 3, column 7 · ambiguous', MEMBER_MESSAGES.not_a_method, null, []],
  ['Cube class app/cube.py lines 10–20', 'Inherits at line 12, column 5', MEMBER_MESSAGES.defined, null, ['area method app/cube.py lines 15–16']],
  ['Box class lib/box.py lines 1–4', 'Inherits at line 1, column 11', MEMBER_MESSAGES.unsupported, null, []],
  ['Prop class app/prop.py lines 1–30', 'Inherits at line 1, column 12', MEMBER_MESSAGES.defined, SEVERAL_DEFINITIONS, ['area method app/prop.py lines 20–22']],
]
// Every count distinct and none equal to a row count, so a swapped or recounted field shows.
const SUMMARY: MethodOverridesSummary = {
  files_scanned: 41,
  too_large: 1,
  binary: 2,
  syntax_error: 3,
  bases: 4,
  base_classes: 5,
  overridden_defined: 6,
  overridden_not_defined: 7,
  overridden_not_a_method: 8,
  overridden_unavailable: 9,
  overridden_unsupported: 10,
  subclasses: 11,
  overriding: 12,
  inheriting: 13,
  overriding_not_a_method: 14,
  overriding_unsupported: 15,
}
const SUMMARY_TEXT =
  'Bases: 4 · 6 defined · 9 unavailable · 10 unsupported. Subclasses: 11 · 12 overriding · 13 inheriting · 15 unsupported.'
const ZERO = Object.fromEntries(Object.keys(SUMMARY).map((k) => [k, 0])) as unknown as MethodOverridesSummary

function definitionResult(extra: Partial<CodeDefinitionResult> = {}): CodeDefinitionResult {
  return {
    status: 'resolved',
    reference: { path: 'app/main.py', line: 4, column: 7, name: 'area' },
    definitions: [AREA],
    truncated: false,
    ...extra,
  }
}

const AMBIGUOUS = definitionResult({
  status: 'ambiguous',
  reference: { path: 'app/main.py', line: 4, column: 7, name: 'Runner' },
  definitions: CANDIDATES,
})

function overridesResult(extra: Partial<MethodOverridesResult> = {}): MethodOverridesResult {
  return {
    status: 'scanned',
    target: AREA,
    owner: SQUARE,
    overridden: OVERRIDDEN,
    overriding_status: 'scanned',
    overriding: OVERRIDING,
    summary: SUMMARY,
    overridden_truncated: false,
    overriding_truncated: false,
    ...extra,
  }
}

function notScanned(status: string): MethodOverridesResult {
  return {
    status: status as MethodOverridesResult['status'],
    target: null,
    owner: null,
    overridden: [],
    overriding_status: null,
    overriding: null,
    summary: ZERO,
    overridden_truncated: false,
    overriding_truncated: null,
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
type Params = Record<string, unknown>

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))
const codeText = () => document.querySelector('pre code')?.textContent
const definitionButton = () =>
  within(screen.getByRole('region', { name: 'Go to definition' })).getByRole('button', {
    name: /definition/i,
  }) as HTMLButtonElement
const definitionList = () => screen.findByRole('list', { name: 'Definitions' })
const region = () => screen.getByRole('region', { name: 'Method overrides' })
const actionButtons = () =>
  within(region()).getAllByRole('button', { name: /method overrides for/i }) as HTMLButtonElement[]
const action = (name: string | RegExp) => within(region()).getByRole('button', { name }) as HTMLButtonElement
const targetText = () => screen.getByTestId('method-overrides-target').textContent
const summaryText = () => screen.getByTestId('method-overrides-summary').textContent
const regionText = () => region().textContent ?? ''
const overriddenRows = () =>
  screen.getAllByTestId('overridden').map((row) => [
    row.firstChild?.textContent,
    within(row).queryByTestId('base-class')?.textContent ?? null,
    within(row).getByTestId('member-status').textContent,
    within(row).queryByTestId('base-kind')?.textContent ?? null,
    within(row).queryByTestId('several')?.textContent ?? null,
    within(row).queryAllByTestId('implementation').map((i) => i.textContent),
  ])
const overridingRows = () =>
  screen.getAllByTestId('overriding').map((row) => [
    row.firstChild?.textContent,
    within(row).getByTestId('overriding-site').textContent,
    within(row).getByTestId('member-status').textContent,
    within(row).queryByTestId('several')?.textContent ?? null,
    within(row).queryAllByTestId('implementation').map((i) => i.textContent),
  ])
const byUrl = (requests: RecordedRequest[], url: string) => requests.filter((r) => r.url === url)
const loaded = () =>
  waitFor(() =>
    expect(screen.queryByTestId('method-overrides-target') ?? screen.queryByRole('alert')).not.toBeNull(),
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

/** Opens main.py, runs Go to definition and records the params of every overrides request. */
async function showDefinition(
  definition: Handler = { status: 200, data: definitionResult() },
  overrides: Handler = { status: 200, data: overridesResult() },
) {
  const seen: Params[] = []
  const view = renderViewer('app/main.py', {
    [GET_CONTENT]: content('app/main.py', MAIN),
    [GET_DEFINITION]: definition,
    [GET_OVERRIDES]: (config) => {
      seen.push({ ...(config.params as Params) })
      return typeof overrides === 'function' ? overrides(config) : overrides
    },
  })
  await waitFor(() => expect(codeText()).toBe(MAIN))
  caretOn(MAIN, 'area()')
  fireEvent.click(definitionButton())
  await definitionList()
  return { seen, ...view }
}

async function showOverrides(overrides: Handler, label = AREA_LABEL) {
  const view = await showDefinition(undefined, overrides)
  fireEvent.click(action(label))
  await loaded()
  return view
}

function renderPanel(
  definitions: DefinitionLocation[] | null,
  overrides: Handler = { status: 200, data: overridesResult() },
  onSelectLine?: (path: string, line: number) => void,
) {
  const seen: Params[] = []
  const requests = mockApi({
    [GET_OVERRIDES]: (config) => {
      seen.push({ ...(config.params as Params) })
      return typeof overrides === 'function' ? overrides(config) : overrides
    },
  })
  const view = render(
    <QueryClientProvider client={createTestQueryClient()}>
      <CodeMethodOverrides projectId={P} repositoryId={R} definitions={definitions} onSelectLine={onSelectLine} />
    </QueryClientProvider>,
  )
  return { seen, requests, ...view }
}

/** Shows `data` (any JSON) for AREA in a bare panel with navigation. */
async function panelResult(data: unknown) {
  const onSelectLine = vi.fn<(path: string, line: number) => void>()
  const view = renderPanel([AREA], { status: 200, data }, onSelectLine)
  fireEvent.click(screen.getByRole('button', { name: AREA_LABEL }))
  await screen.findByTestId('method-overrides-target')
  return { onSelectLine, ...view }
}

/** Holds every overrides request until released; others go through. */
function holdOverrides() {
  const held: { params: Params; release: (data: MethodOverridesResult) => void }[] = []
  const realGet = apiClient.get.bind(apiClient)
  const spy = vi.spyOn(apiClient, 'get').mockImplementation(((url: string, config?: AxiosRequestConfig) => {
    if (url === OVERRIDES_PATH) {
      return new Promise<unknown>((resolve) => {
        held.push({ params: { ...(config?.params as Params) }, release: (data) => resolve({ data }) })
      })
    }
    return realGet(url, config)
  }) as typeof apiClient.get)
  return { held, spy }
}

afterEach(() => {
  document.getSelection()?.removeAllRanges()
})

// --- Visibility and requests ----------------------------------------------------------------

describe('Method overrides visibility and requests', () => {
  it('requests nothing on file load, selection, a definition result or Open (O)', async () => {
    const { requests, onSelectLine } = await showDefinition()
    caretOn(MAIN, 'shape =')
    fireEvent.mouseUp(document.querySelector('pre') as HTMLPreElement)
    expect(actionButtons().map((b) => b.textContent)).toEqual([AREA_LABEL])
    fireEvent.click(screen.getByRole('button', { name: 'Open shapes/square.py at line 5 (method area)' }))
    expect(onSelectLine).toHaveBeenCalledTimes(1)
    await sleep(50)
    expect(byUrl(requests, OVERRIDES_PATH)).toHaveLength(0)
    expect(screen.queryByTestId('method-overrides-target')).toBeNull()
  })

  it('sends exactly the method path and its start line as one GET, once (H-N)', async () => {
    const { seen, requests } = await showDefinition()
    fireEvent.click(action(AREA_LABEL))
    await loaded()
    // Not end_line (6), not the cursor line (4) or column (7), nothing else.
    expect(seen).toEqual([{ path: 'shapes/square.py', line: 5 }])
    expect(byUrl(requests, OVERRIDES_PATH).map((r) => [r.method, r.data])).toEqual([['GET', undefined]])
    await sleep(50)
    expect(byUrl(requests, OVERRIDES_PATH)).toHaveLength(1)
  })

  it('offers an action only for methods, in server order, and requests the clicked one (A-D)', async () => {
    const { seen } = await showDefinition({ status: 200, data: AMBIGUOUS })
    expect(actionButtons().map((b) => b.textContent)).toEqual(CANDIDATE_LABELS)
    for (const other of ['zeta/impl.py', 'alpha/func.py', 'pkg/runner.py']) {
      expect(regionText()).not.toContain(other) // the class, the function, the module
    }
    fireEvent.click(action(CANDIDATE_LABELS[1]))
    await loaded()
    expect(seen).toEqual([{ path: 'beta/impl.py', line: 14 }]) // the clicked method, not the first
  })

  it.each([
    ['a class', [ZETA]],
    ['a function', [FUNCTION]],
    ['a module', [MODULE]],
    ['no result', null],
    ['no definitions', []],
  ])('renders nothing and requests nothing for %s (B-F)', async (_name, definitions) => {
    const { container, requests } = renderPanel(definitions)
    expect(container.childElementCount).toBe(0)
    await sleep(30)
    expect(requests).toHaveLength(0)
  })

  it('a non-Python file has no go-to-definition and so no method overrides (G)', async () => {
    const { requests } = renderViewer('docs/notes.md', {
      [GET_CONTENT]: content('docs/notes.md', 'class A:\n    def area(self): pass\n'),
    })
    await waitFor(() => expect(codeText()).toBe('class A:\n    def area(self): pass\n'))
    expect(screen.queryByRole('region', { name: 'Go to definition' })).toBeNull()
    expect(screen.queryByRole('region', { name: 'Method overrides' })).toBeNull()
    expect(byUrl(requests, OVERRIDES_PATH)).toHaveLength(0)
  })

  it('labels only the clicked method as loading and disables every action meanwhile (P-R)', async () => {
    const hold = holdOverrides()
    try {
      await showDefinition({ status: 200, data: AMBIGUOUS })
      fireEvent.click(action(CANDIDATE_LABELS[0]))
      await screen.findByText('Loading method overrides for method Runner (alpha/impl.py, line 2)…')
      expect(actionButtons().map((b) => [b.textContent, b.disabled])).toEqual([
        ['Loading method overrides for method Runner (alpha/impl.py, line 2)…', true],
        [CANDIDATE_LABELS[1], true],
      ])
      fireEvent.click(actionButtons()[0])
      fireEvent.click(actionButtons()[1])
      await sleep(20)
      expect(hold.held).toHaveLength(1) // disabled: no duplicate or concurrent request
      hold.held[0].release(overridesResult({ target: METHOD_A }))
      await screen.findByTestId('method-overrides-target')
      expect(actionButtons().map((b) => [b.textContent, b.disabled])).toEqual(
        CANDIDATE_LABELS.map((label) => [label, false]),
      )
    } finally {
      hold.spy.mockRestore()
    }
  })

  it('sends an unusual server path unchanged', async () => {
    const odd: DefinitionLocation = { ...AREA, path: 'a b/ü&x=1#.py' }
    const { seen } = renderPanel([odd])
    fireEvent.click(screen.getByRole('button', { name: /^Method overrides for/ }))
    await screen.findByTestId('method-overrides-target')
    expect(seen).toEqual([{ path: 'a b/ü&x=1#.py', line: 5 }])
  })
})

// --- Results -----------------------------------------------------------------------------

describe('Method overrides results', () => {
  it('shows the target, its class and the server summary (AS)', async () => {
    await showOverrides({ status: 200, data: overridesResult() })
    expect(targetText()).toBe('Method overrides of method area (shapes/square.py, line 5) in class Square')
    expect(summaryText()).toBe(SUMMARY_TEXT)
  })

  it('never recounts: the summary is the server counts, whatever the rows (AT)', async () => {
    await panelResult(overridesResult({ summary: ZERO }))
    expect(summaryText()).toBe(
      'Bases: 0 · 0 defined · 0 unavailable · 0 unsupported. Subclasses: 0 · 0 overriding · 0 inheriting · 0 unsupported.',
    )
    expect([screen.getAllByTestId('overridden').length, screen.getAllByTestId('overriding').length]).toEqual([8, 5])
  })

  it('shows every overridden entry in server order with duplicates, statuses and candidates (S-AC)', async () => {
    await showOverrides({ status: 200, data: overridesResult() })
    expect(screen.getByRole('heading', { name: 'Overridden in direct base classes' })).toBeTruthy()
    expect(overriddenRows()).toEqual(OVERRIDDEN_ROWS)
  })

  it('shows direct overrides in server order with duplicates and the ambiguity marker (AG-AI, AO)', async () => {
    await showOverrides({ status: 200, data: overridesResult() })
    expect(screen.getByRole('heading', { name: 'Direct overrides' })).toBeTruthy()
    expect(overridingRows()).toEqual(OVERRIDING_ROWS)
  })

  it('navigates implementations and base classes to their path and start line (AD, AE)', async () => {
    const { onSelectLine } = await panelResult(overridesResult())
    const rows = screen.getAllByTestId('overridden')
    fireEvent.click(within(rows[0]).getByRole('button', { name: 'Open base class Shape in shapes/base.py at line 4' }))
    within(rows[1])
      .getAllByRole('button', { name: /^Open implementation/ })
      .forEach((button) => fireEvent.click(button))
    fireEvent.click(
      within(rows[2]).getByRole('button', { name: 'Open base class Tile in alpha/tile.py at line 1, ambiguous' }),
    )
    fireEvent.click(within(rows[0]).getByRole('button', { name: /^Open implementation/ }))
    const subclassRows = screen.getAllByTestId('overriding')
    fireEvent.click(within(subclassRows[0]).getByRole('button', { name: 'Open implementation method area in app/cube.py at line 15' }))
    expect(onSelectLine.mock.calls).toEqual([
      ['shapes/base.py', 4],
      ['zeta/tile.py', 12],
      ['zeta/tile.py', 20],
      ['alpha/tile.py', 1],
      ['shapes/base.py', 6],
      ['app/cube.py', 15],
    ])
  })

  it("navigates base sites to the server target's file at the site line (AF)", async () => {
    // The server's target is authoritative, even when it differs from the clicked definition.
    const moved: DefinitionLocation = { ...AREA, path: 'moved/square.py' }
    const { onSelectLine } = await panelResult(overridesResult({ target: moved }))
    const rows = screen.getAllByTestId('overridden')
    fireEvent.click(within(rows[1]).getByRole('button', { name: 'Jump to moved/square.py at line 3 (base Tile)' }))
    fireEvent.click(within(rows[6]).getByRole('button', { name: 'Jump to moved/square.py at line 3 (base object)' }))
    expect(onSelectLine.mock.calls).toEqual([
      ['moved/square.py', 3],
      ['moved/square.py', 3],
    ])
    expect(within(rows[7]).queryByRole('button')).toBeNull() // a dynamic base: nothing to open
  })

  it('navigates subclasses and their sites within subclass.path (AM, AN)', async () => {
    const { onSelectLine } = await panelResult(overridesResult())
    const rows = screen.getAllByTestId('overriding')
    fireEvent.click(within(rows[0]).getByRole('button', { name: 'Open subclass class Cube in app/cube.py at line 10' }))
    fireEvent.click(within(rows[0]).getByRole('button', { name: 'Jump to app/cube.py at line 11 (inherits Square)' }))
    fireEvent.click(within(rows[2]).getByRole('button', { name: 'Jump to app/cube.py at line 12 (inherits Square)' }))
    fireEvent.click(
      within(rows[1]).getByRole('button', { name: 'Jump to app/alpha.py at line 3 (inherits Sq, ambiguous)' }),
    )
    expect(onSelectLine.mock.calls).toEqual([
      ['app/cube.py', 10],
      ['app/cube.py', 11],
      ['app/cube.py', 12],
      ['app/alpha.py', 3],
    ])
  })

  it('unsupported override analysis is never shown as an empty list; bases still shown (AJ)', async () => {
    await panelResult(overridesResult({ overriding_status: 'unsupported_target', overriding: null, overriding_truncated: null }))
    expect(regionText()).toContain(OVERRIDES_UNSUPPORTED)
    expect(regionText()).not.toContain(NO_OVERRIDES)
    expect(overriddenRows()).toEqual(OVERRIDDEN_ROWS)
    expect(screen.queryAllByTestId('overriding')).toHaveLength(0)
  })

  it('null overriding is unsupported, not empty (AK)', async () => {
    await panelResult(overridesResult({ overriding: null, overriding_truncated: null }))
    expect(regionText()).toContain(OVERRIDES_UNSUPPORTED)
    expect(regionText()).not.toContain(NO_OVERRIDES)
  })

  it('shows empty direct overrides and an empty base list (AL)', async () => {
    await panelResult(overridesResult({ overridden: [], overriding: [] }))
    expect(regionText()).toContain(NO_OVERRIDES)
    expect(regionText()).toContain(NO_BASES)
    expect(regionText()).not.toContain(OVERRIDES_UNSUPPORTED)
  })

  it.each([
    [true, false],
    [false, true],
    [true, true],
    [false, false],
  ])('shows each truncation notice only for its own flag: overridden %s, overriding %s (AP-AR)', async (up, down) => {
    await panelResult(overridesResult({ overridden_truncated: up, overriding_truncated: down }))
    const text = regionText()
    expect(text.includes(OVERRIDDEN_TRUNCATED)).toBe(up)
    expect(text.includes(OVERRIDING_TRUNCATED)).toBe(down)
    const heading = text.indexOf('Direct overrides')
    if (up) {
      expect(text.indexOf(OVERRIDDEN_TRUNCATED)).toBeLessThan(heading) // under the overridden section
    }
    if (down) {
      expect(text.indexOf(OVERRIDING_TRUNCATED)).toBeGreaterThan(heading)
    }
  })

  it.each(Object.entries(STATUS_MESSAGES))('status %s shows only its message (AU)', async (status, message) => {
    renderPanel([AREA], { status: 200, data: notScanned(status) })
    fireEvent.click(screen.getByRole('button', { name: AREA_LABEL }))
    await screen.findByText(message)
    expect(targetText()).toBe('Method overrides of method area (shapes/square.py, line 5)') // the requested method
    expect(screen.queryByTestId('method-overrides-summary')).toBeNull()
    expect(screen.queryByText(NO_BASES)).toBeNull()
    expect(screen.queryByText(NO_OVERRIDES)).toBeNull()
  })

  it('shows a safe fallback for an unknown status (AV)', async () => {
    renderPanel([AREA], { status: 200, data: notScanned('something_new') })
    fireEvent.click(screen.getByRole('button', { name: AREA_LABEL }))
    await screen.findByText(UNKNOWN_STATUS)
    expect(screen.queryByTestId('method-overrides-summary')).toBeNull()
  })

  it('has the approved wording', () => {
    expect(STATUS_MESSAGES).toEqual({
      not_a_definition: 'Method overrides are unavailable: no method starts on this line.',
      not_a_method: 'Method overrides are only available for methods.',
      syntax_error: 'Method overrides are unavailable: the target file could not be parsed as Python.',
      too_large: 'Method overrides are unavailable: the target file is too large to analyze.',
      binary: 'Method overrides are unavailable: the target file is not text.',
      unsupported_language: 'Method overrides are only available for Python files.',
    })
    expect(MEMBER_MESSAGES).toEqual({
      defined: 'Defined in this class',
      not_defined: 'Not defined in this class',
      not_a_method: 'Bound in this class, but not as a method',
      unsupported: 'Static analysis could not determine whether this class defines the method.',
      unavailable: 'The base class could not be resolved to a repository class.',
    })
    expect([NO_BASES, NO_OVERRIDES, OVERRIDES_UNSUPPORTED, OVERRIDDEN_TRUNCATED, OVERRIDING_TRUNCATED]).toEqual([
      "This method's class does not directly inherit from another class.",
      'No direct overrides found.',
      "Override analysis is not available for this method's class.",
      'Overridden-method results are truncated.',
      'Override results are truncated.',
    ])
    expect([SEVERAL_DEFINITIONS, UNKNOWN_STATUS]).toEqual([
      'Defined more than once in this class',
      'Method overrides are unavailable.',
    ])
  })

  it('reads the base class from the JSON key "class", never "class_"', async () => {
    const decoy = { path: 'decoy/wrong.py', kind: 'class', name: 'Decoy', line: 99, end_line: 99 }
    const raw = {
      ...overridesResult(),
      overridden: [{ ...OVERRIDDEN[0], class: SHAPE, class_: decoy }],
    }
    const { onSelectLine } = await panelResult(raw)
    expect(screen.getByTestId('base-class').textContent).toBe('Shape class shapes/base.py lines 4–26')
    expect(regionText()).not.toContain('decoy')
    fireEvent.click(screen.getByRole('button', { name: 'Open base class Shape in shapes/base.py at line 4' }))
    expect(onSelectLine).toHaveBeenCalledWith('shapes/base.py', 4)
  })

  it('shows the multiple-definitions note from the server flag only, never the list length', async () => {
    await panelResult(
      overridesResult({
        overridden: [
          overridden({ implementations: [SHAPE_AREA], implementations_ambiguous: true }), // cut to one
          overridden({ implementations: [SHAPE_AREA, SHAPE_AREA], implementations_ambiguous: false }),
        ],
        overriding: [],
      }),
    )
    const rows = screen.getAllByTestId('overridden')
    expect(within(rows[0]).queryByTestId('several')?.textContent).toBe(SEVERAL_DEFINITIONS)
    expect(within(rows[0]).getAllByTestId('implementation')).toHaveLength(1)
    expect(within(rows[1]).queryByTestId('several')).toBeNull()
    expect(within(rows[1]).getAllByTestId('implementation')).toHaveLength(2)
  })

  it.each(['builtin', 'unresolved', 'dynamic', 'unsupported', 'not_a_class'] as BaseKind[])(
    "explains an unavailable %s base with Step 52's wording (X)",
    async (kind) => {
      await panelResult(
        overridesResult({
          overridden: [overridden({ base_kind: kind, class: null, status: 'unavailable', implementations: [] })],
        }),
      )
      expect(overriddenRows()).toEqual([
        [
          'Shape line 3, column 14',
          null,
          MEMBER_MESSAGES.unavailable,
          KIND_MESSAGES[kind as keyof typeof KIND_MESSAGES],
          null,
          [],
        ],
      ])
    },
  )

  it('without onSelectLine, every row is plain text', async () => {
    renderPanel([AREA])
    fireEvent.click(screen.getByRole('button', { name: AREA_LABEL }))
    await screen.findByTestId('method-overrides-target')
    expect(overriddenRows()).toEqual(OVERRIDDEN_ROWS)
    expect(overridingRows()).toEqual(OVERRIDING_ROWS)
    expect(screen.queryAllByRole('button', { name: /^(Jump|Open) / })).toHaveLength(0)
  })
})

// --- Errors --------------------------------------------------------------------------------

describe('Method overrides errors', () => {
  it.each([
    [404, 'Repository not found'],
    [409, 'Repository changed during analysis; run it again'],
    [429, 'Too many attempts; please try again later'],
  ])('shows the server detail for %i exactly, without retrying (AW-AY, BD)', async (status, detail) => {
    const { requests } = await showOverrides({ status, data: { detail } })
    const alerts = await screen.findAllByRole('alert')
    expect(alerts.map((a) => a.textContent)).toEqual([detail])
    await sleep(50)
    expect(byUrl(requests, OVERRIDES_PATH)).toHaveLength(1)
    expect(screen.queryByTestId('overridden')).toBeNull()
  })

  it('shows the fallback for a 500 without a usable detail, without retrying (AZ, BD)', async () => {
    const { requests } = await showOverrides({ status: 500, data: {} })
    expect((await screen.findByRole('alert')).textContent).toBe('Could not load the method overrides')
    await sleep(50)
    expect(byUrl(requests, OVERRIDES_PATH)).toHaveLength(1)
  })

  it('uses the existing network message without retrying (BA)', async () => {
    const { requests } = await showOverrides(() => {
      throw new AxiosError('Network Error', AxiosError.ERR_NETWORK)
    })
    expect((await screen.findByRole('alert')).textContent).toBe('Could not reach the server')
    await sleep(50)
    expect(byUrl(requests, OVERRIDES_PATH)).toHaveLength(1)
  })
})

// --- Stale results -------------------------------------------------------------------------

describe('Method overrides never show a stale result', () => {
  it('a new go-to-definition lookup drops a request that is still loading (BC)', async () => {
    const hold = holdOverrides()
    try {
      let lookups = 0
      await showDefinition(() => {
        lookups += 1
        return { status: 200, data: lookups === 1 ? definitionResult() : AMBIGUOUS }
      })
      fireEvent.click(action(AREA_LABEL))
      await screen.findByText('Loading method overrides for method area (shapes/square.py, line 5)…')
      caretOn(MAIN, 'shape =')
      fireEvent.click(definitionButton())
      await screen.findByText('5 possible definitions of', { exact: false })
      expect(actionButtons().map((b) => [b.textContent, b.disabled])).toEqual(
        CANDIDATE_LABELS.map((label) => [label, false]),
      )
      hold.held[0].release(overridesResult())
      await sleep(50)
      expect(screen.queryByTestId('method-overrides-target')).toBeNull()
      expect(screen.queryByText('Cube', { exact: false })).toBeNull()
    } finally {
      hold.spy.mockRestore()
    }
  })

  it('a new lookup clears shown results, even with an identical result', async () => {
    const { requests } = await showOverrides({ status: 200, data: overridesResult() })
    fireEvent.click(definitionButton())
    await waitFor(() => expect(byUrl(requests, DEFINITION_PATH)).toHaveLength(2))
    await definitionList()
    await sleep(50)
    expect(actionButtons().map((b) => b.textContent)).toEqual([AREA_LABEL])
    expect(screen.queryByTestId('method-overrides-target')).toBeNull()
    expect(screen.queryByTestId('overridden')).toBeNull()
    expect(byUrl(requests, OVERRIDES_PATH)).toHaveLength(1)
  })

  const contentRoute: Handler = (config) => {
    const path = (config.params as { path: string }).path
    return content(path, path === 'app/main.py' ? MAIN : OTHER)
  }

  it('never shows a result for a file that is no longer open (BB)', async () => {
    const hold = holdOverrides()
    try {
      const { rerenderPath } = renderViewer('app/main.py', {
        [GET_CONTENT]: contentRoute,
        [GET_DEFINITION]: { status: 200, data: definitionResult() },
      })
      await waitFor(() => expect(codeText()).toBe(MAIN))
      caretOn(MAIN, 'area()')
      fireEvent.click(definitionButton())
      await definitionList()
      fireEvent.click(action(AREA_LABEL))
      await screen.findByRole('button', { name: /^Loading method overrides/ })
      rerenderPath('app/other.py')
      await waitFor(() => expect(codeText()).toBe(OTHER))
      hold.held[0].release(overridesResult())
      await sleep(50)
      expect(screen.queryByRole('region', { name: 'Method overrides' })).toBeNull()
      expect(screen.queryByText('Cube', { exact: false })).toBeNull()
    } finally {
      hold.spy.mockRestore()
    }
  })

  it('starts fresh after returning to a file, with no request and no cache', async () => {
    const { rerenderPath, requests } = renderViewer('app/main.py', {
      [GET_CONTENT]: contentRoute,
      [GET_DEFINITION]: { status: 200, data: definitionResult() },
      [GET_OVERRIDES]: { status: 200, data: overridesResult() },
    })
    await waitFor(() => expect(codeText()).toBe(MAIN))
    caretOn(MAIN, 'area()')
    fireEvent.click(definitionButton())
    await definitionList()
    fireEvent.click(action(AREA_LABEL))
    await loaded()
    rerenderPath('app/other.py')
    await waitFor(() => expect(codeText()).toBe(OTHER))
    rerenderPath('app/main.py')
    await waitFor(() => expect(codeText()).toBe(MAIN))
    caretOn(MAIN, 'area()')
    fireEvent.click(definitionButton())
    await definitionList()
    await sleep(50)
    expect(screen.queryByTestId('method-overrides-target')).toBeNull()
    expect(byUrl(requests, OVERRIDES_PATH)).toHaveLength(1)
  })
})

// --- Security ------------------------------------------------------------------------------

describe('Method overrides security', () => {
  it('renders server text as plain text, never as HTML (BG)', async () => {
    const method: DefinitionLocation = { path: `${HTML}.py`, kind: 'method', name: HTML, line: 3, end_line: 4 }
    const cls: DefinitionLocation = { path: `${HTML}.py`, kind: 'class', name: HTML, line: 1, end_line: 9 }
    renderPanel([method], {
      status: 200,
      data: overridesResult({
        target: method,
        owner: cls,
        overridden: [
          overridden({ site: { line: 1, column: 9, name: HTML }, class: cls, implementations: [method, method] }),
        ],
        overriding: [
          { subclass: cls, site: { line: 5, column: 6, name: HTML, ambiguous: true }, status: 'defined', implementations: [method], implementations_ambiguous: false },
        ],
      }),
    })
    fireEvent.click(screen.getByRole('button', { name: /^Method overrides for/ }))
    await screen.findByTestId('method-overrides-target')
    expect(overriddenRows()[0][0]).toBe(`${HTML} line 1, column 9`)
    expect(overridingRows()[0][0]).toBe(`${HTML} class ${HTML}.py lines 1–9`)
    expect(document.querySelector('img, script, a, iframe')).toBeNull()
    expect(document.querySelector('[href], [src], [onerror]')).toBeNull()
  })

  it('shows only the named fields of the response (BH)', async () => {
    const extra = { snippet: 'FAKE-SNIPPET-MARKER', sha256: 'FAKE-SHA-MARKER', id: 'FAKE-ID-MARKER' }
    const payload = {
      ...overridesResult({
        overridden: OVERRIDDEN.map((e) => ({
          ...e,
          ...extra,
          site: e.site && { ...e.site, ...extra },
          implementations: e.implementations.map((d) => ({ ...d, ...extra })),
        })),
        overriding: OVERRIDING.map((e) => ({ ...e, ...extra, site: { ...e.site, ...extra } })),
      }),
      target: { ...AREA, ...extra },
      owner: { ...SQUARE, ...extra },
      summary: { ...SUMMARY, ...extra },
      ...extra,
    }
    await panelResult(payload)
    const labels = Array.from(region().querySelectorAll('[aria-label]')).map((e) => e.getAttribute('aria-label'))
    for (const marker of Object.values(extra)) {
      expect(regionText()).not.toContain(marker)
      expect(labels.join(' ')).not.toContain(marker)
    }
    expect(regionText()).not.toContain('{')
    expect(regionText()).not.toContain('overriding_status')
    expect(regionText()).not.toContain('base_kind')
  })

  it('uses no browser storage or cookies and calls only the app API (BE, BF)', async () => {
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
      const { requests } = await showOverrides({ status: 200, data: overridesResult() })
      fireEvent.click(screen.getByRole('button', { name: 'Jump to app/cube.py at line 11 (inherits Square)' }))
      expect(new Set(getItem.mock.calls.map((call) => call[0]))).toEqual(new Set(['ai_se_access_token']))
      for (const spy of writes) {
        expect(spy).not.toHaveBeenCalled()
      }
      expect([localStorage.length, sessionStorage.length]).toEqual(stored)
      expect(cookieGet).not.toHaveBeenCalled()
      expect(cookieSet).not.toHaveBeenCalled()
      expect(fetchSpy).not.toHaveBeenCalled()
      expect(requests.map((r) => `${r.method} ${r.url}`)).toEqual([GET_CONTENT, GET_DEFINITION, GET_OVERRIDES])
      expect(requests.every((r) => r.baseURL === '/api/v1')).toBe(true)
    } finally {
      for (const spy of [getItem, ...writes]) {
        spy.mockRestore()
      }
      globalThis.fetch = realFetch
      delete (document as { cookie?: unknown }).cookie
    }
  })
})

// --- Existing go-to-definition behaviour ----------------------------------------------------

describe('Go to definition with the method overrides panel', () => {
  it('keeps the existing panels, in order, with method overrides last (BI)', async () => {
    const { requests, onSelectLine } = renderViewer('app/main.py', {
      [GET_CONTENT]: content('app/main.py', MAIN),
      [GET_DEFINITION]: { status: 200, data: AMBIGUOUS },
    })
    await waitFor(() => expect(codeText()).toBe(MAIN))
    caretOn(MAIN, 'area()')
    fireEvent.click(definitionButton())
    const list = await definitionList()
    expect(within(list).getAllByTestId('definition')).toHaveLength(5)
    const definition = screen.getByRole('region', { name: 'Go to definition' })
    const panels = within(definition)
      .getAllByRole('region')
      .map((r) => r.getAttribute('aria-label'))
    expect(panels).toEqual(['Find references', 'Call hierarchy', 'Type hierarchy', 'Method overrides'])
    fireEvent.click(screen.getByRole('button', { name: 'Open beta/impl.py at line 14 (method Runner)' }))
    expect(onSelectLine).toHaveBeenCalledWith('beta/impl.py', 14)
    await sleep(30)
    expect(requests.map((r) => `${r.method} ${r.url}`)).toEqual([GET_CONTENT, GET_DEFINITION])
  })
})
