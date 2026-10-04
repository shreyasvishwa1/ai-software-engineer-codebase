import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { AxiosError, type AxiosRequestConfig, type InternalAxiosRequestConfig } from 'axios'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { apiClient } from '../services/client'
import { mockApi, type MockResponse, PROJECT, type RecordedRequest, ZIP_REPOSITORY } from '../test/mockApi'
import { createTestQueryClient } from '../test/render'
import type {
  BaseEntry,
  CodeDefinitionResult,
  DefinitionLocation,
  FileContent,
  SubclassEntry,
  TypeHierarchyResult,
  TypeHierarchySummary,
} from '../types/api'
import {
  BASES_TRUNCATED,
  CodeTypeHierarchy,
  NO_BASES,
  NO_SUBCLASSES,
  STATUS_MESSAGES,
  SUBCLASSES_TRUNCATED,
  SUBCLASSES_UNSUPPORTED,
  UNKNOWN_STATUS,
} from './CodeTypeHierarchy'
import { FileViewer } from './FileViewer'

const P = PROJECT.id
const R = ZIP_REPOSITORY.id
const BASE = `/projects/${P}/repositories/${R}`
const DEFINITION_PATH = `${BASE}/files/definition`
const GET_DEFINITION = `GET ${DEFINITION_PATH}`
const HIERARCHY_PATH = `${BASE}/files/type-hierarchy`
const GET_HIERARCHY = `GET ${HIERARCHY_PATH}`
const GET_CALLS = `GET ${BASE}/files/call-hierarchy`
const GET_CONTENT = `GET ${BASE}/files/content`
const HTML = '<img src=x onerror=alert(1)>'

const MAIN = 'from pets.dog import Dog\n\npet = Dog()\nother = 1\n'
const OTHER = 'import json\n'

// The class statement spans lines 7-20; its bases are written on later lines.
const DOG: DefinitionLocation = { path: 'pets/dog.py', kind: 'class', name: 'Dog', line: 7, end_line: 20 }
const DOG_LABEL = 'Type hierarchy for class Dog (pets/dog.py, line 7)'
const FUNCTION: DefinitionLocation = { path: 'alpha/impl.py', kind: 'function', name: 'Runner', line: 5, end_line: 9 }
const METHOD: DefinitionLocation = { path: 'alpha/impl.py', kind: 'method', name: 'Runner', line: 2, end_line: 3 }
const MODULE: DefinitionLocation = { path: 'pkg/runner.py', kind: 'module', name: 'runner', line: 1, end_line: 1 }
const ZETA: DefinitionLocation = { path: 'zeta/impl.py', kind: 'class', name: 'Runner', line: 30, end_line: 40 }
const BETA: DefinitionLocation = { path: 'beta/impl.py', kind: 'class', name: 'Runner', line: 4, end_line: 6 }
// Server order, deliberately not sorted by path, kind, name or line.
const CANDIDATES: DefinitionLocation[] = [ZETA, FUNCTION, METHOD, MODULE, BETA]
const CANDIDATE_LABELS = [
  'Type hierarchy for class Runner (zeta/impl.py, line 30)',
  'Type hierarchy for class Runner (beta/impl.py, line 4)',
]

const ANIMAL: DefinitionLocation = { path: 'zoo/base.py', kind: 'class', name: 'Animal', line: 1, end_line: 2 }
const MIXINS: DefinitionLocation[] = [
  { path: 'zeta/mix.py', kind: 'class', name: 'Mixin', line: 9, end_line: 12 },
  { path: 'alpha/mix.py', kind: 'class', name: 'Mixin', line: 3, end_line: 4 },
  { path: 'alpha/mix.py', kind: 'class', name: 'Mixin', line: 3, end_line: 4 },
  { path: 'lib/mix.py', kind: 'module', name: 'mix', line: 1, end_line: 1 },
]
const FACTORY: DefinitionLocation = { path: 'zoo/base.py', kind: 'function', name: 'factory', line: 7, end_line: 8 }
// Server (source) order, never sorted, with a duplicate base and every kind.
const BASES: BaseEntry[] = [
  { site: { line: 8, column: 5, name: 'Animal' }, kind: 'resolved', definitions: [ANIMAL] },
  { site: { line: 9, column: 5, name: 'Mixin' }, kind: 'ambiguous', definitions: MIXINS },
  { site: { line: 10, column: 5, name: 'Animal' }, kind: 'resolved', definitions: [ANIMAL] },
  { site: { line: 11, column: 5, name: 'factory' }, kind: 'not_a_class', definitions: [FACTORY] },
  { site: { line: 12, column: 5, name: 'object' }, kind: 'builtin', definitions: [] },
  { site: { line: 13, column: 10, name: 'Animal' }, kind: 'unsupported', definitions: [] },
  { site: { line: 14, column: 5, name: 'Generic' }, kind: 'unresolved', definitions: [] },
  { site: null, kind: 'dynamic', definitions: [] },
]
const BASE_SITES = [
  'Animal line 8, column 5',
  'Mixin line 9, column 5',
  'Animal line 10, column 5',
  'factory line 11, column 5',
  'object line 12, column 5',
  'Animal line 13, column 10',
  'Generic line 14, column 5',
  'Dynamic base expression',
]
const BASE_KINDS = [
  'Resolved class',
  'Ambiguous: 4 candidates',
  'Resolved class',
  'Not a class',
  'Built-in class',
  'Static resolution is not supported for this base.',
  'Base class could not be resolved.',
  'Dynamic base: the base class could not be resolved statically.',
]
const BASE_DEFINITIONS = [
  'Animal class zoo/base.py lines 1–2',
  'Mixin class zeta/mix.py lines 9–12',
  'Mixin class alpha/mix.py lines 3–4',
  'Mixin class alpha/mix.py lines 3–4',
  'mix module lib/mix.py lines 1–1',
  'Animal class zoo/base.py lines 1–2',
  'factory function zoo/base.py lines 7–8',
]
const PUPPY: DefinitionLocation = { path: 'pets/puppy.py', kind: 'class', name: 'Puppy', line: 2, end_line: 5 }
const ALPHA: DefinitionLocation = { path: 'app/alpha.py', kind: 'class', name: 'Alpha', line: 10, end_line: 12 }
// Server order deliberately unsorted, with a duplicate subclass (it inherits Dog twice).
const SUBCLASSES: SubclassEntry[] = [
  { subclass: PUPPY, site: { line: 3, column: 5, name: 'Dog', ambiguous: false } },
  { subclass: ALPHA, site: { line: 11, column: 7, name: 'D', ambiguous: true } },
  { subclass: PUPPY, site: { line: 4, column: 5, name: 'Dog', ambiguous: false } },
]
const SUBCLASS_TEXTS = [
  'Puppy class pets/puppy.py lines 2–5',
  'Alpha class app/alpha.py lines 10–12',
  'Puppy class pets/puppy.py lines 2–5',
]
const SITE_TEXTS = [
  'Inherits at line 3, column 5',
  'Inherits at line 11, column 7 · ambiguous',
  'Inherits at line 4, column 5',
]
// Every count distinct, so a swapped field is visible.
const SUMMARY: TypeHierarchySummary = {
  files_scanned: 41,
  too_large: 1,
  binary: 2,
  syntax_error: 3,
  bases: 4,
  bases_resolved: 5,
  bases_ambiguous: 6,
  bases_builtin: 7,
  bases_unresolved: 8,
  bases_unsupported: 9,
  bases_dynamic: 10,
  bases_not_a_class: 11,
  keyword_arguments: 12,
  subclasses: 13,
  subclasses_ambiguous: 14,
  subclass_unsupported: 15,
  non_base_references: 16,
}
const SUMMARY_TEXT =
  'Bases: 4 · 5 resolved · 6 ambiguous · 8 unresolved · 10 dynamic. Subclasses: 13 · 14 ambiguous.'
const ZERO = Object.fromEntries(Object.keys(SUMMARY).map((k) => [k, 0])) as unknown as TypeHierarchySummary

function definitionResult(extra: Partial<CodeDefinitionResult> = {}): CodeDefinitionResult {
  return {
    status: 'resolved',
    reference: { path: 'app/main.py', line: 3, column: 7, name: 'Dog' },
    definitions: [DOG],
    truncated: false,
    ...extra,
  }
}

const AMBIGUOUS = definitionResult({
  status: 'ambiguous',
  reference: { path: 'app/main.py', line: 3, column: 7, name: 'Runner' },
  definitions: CANDIDATES,
})

function hierarchyResult(extra: Partial<TypeHierarchyResult> = {}): TypeHierarchyResult {
  return {
    status: 'scanned',
    target: DOG,
    bases: BASES,
    subclasses_status: 'scanned',
    subclasses: SUBCLASSES,
    summary: SUMMARY,
    bases_truncated: false,
    subclasses_truncated: false,
    ...extra,
  }
}

function notScanned(status: string): TypeHierarchyResult {
  return {
    status: status as TypeHierarchyResult['status'],
    target: null,
    bases: [],
    subclasses_status: null,
    subclasses: null,
    summary: ZERO,
    bases_truncated: false,
    subclasses_truncated: null,
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
const region = () => screen.getByRole('region', { name: 'Type hierarchy' })
const actionButtons = () =>
  within(region()).getAllByRole('button', { name: /type hierarchy for/i }) as HTMLButtonElement[]
const action = (name: string | RegExp) => within(region()).getByRole('button', { name }) as HTMLButtonElement
const baseSites = () => screen.getAllByTestId('base').map((b) => b.firstChild?.textContent)
const baseKinds = () => screen.getAllByTestId('base-kind').map((k) => k.textContent)
const baseDefinitions = () => screen.getAllByTestId('base-definition').map((d) => d.textContent)
const subclassTexts = () => screen.getAllByTestId('subclass').map((s) => s.firstChild?.textContent)
const siteTexts = () => screen.getAllByTestId('subclass-site').map((s) => s.textContent)
const targetText = () => screen.getByTestId('type-hierarchy-target').textContent
const summaryText = () => screen.getByTestId('type-hierarchy-summary').textContent
const regionText = () => region().textContent ?? ''
const byUrl = (requests: RecordedRequest[], url: string) => requests.filter((r) => r.url === url)
const loaded = () =>
  waitFor(() =>
    expect(screen.queryByTestId('type-hierarchy-target') ?? screen.queryByRole('alert')).not.toBeNull(),
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
  caretOn(MAIN, 'Dog()')
  fireEvent.click(definitionButton())
  await definitionList()
  return { seen, ...view }
}

async function showHierarchy(hierarchy: Handler, label = DOG_LABEL) {
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
      <CodeTypeHierarchy projectId={P} repositoryId={R} definitions={definitions} onSelectLine={onSelectLine} />
    </QueryClientProvider>,
  )
  return { seen, requests, ...view }
}

/** Shows `data` for DOG in a bare panel with navigation. */
async function panelResult(data: TypeHierarchyResult) {
  const onSelectLine = vi.fn<(path: string, line: number) => void>()
  const view = renderPanel([DOG], { status: 200, data }, onSelectLine)
  fireEvent.click(screen.getByRole('button', { name: DOG_LABEL }))
  await screen.findByTestId('type-hierarchy-target')
  return { onSelectLine, ...view }
}

/** Holds every hierarchy request until released; others go through. */
function holdHierarchy() {
  const held: { params: Params; release: (data: TypeHierarchyResult) => void }[] = []
  const realGet = apiClient.get.bind(apiClient)
  const spy = vi.spyOn(apiClient, 'get').mockImplementation(((url: string, config?: AxiosRequestConfig) => {
    if (url === HIERARCHY_PATH) {
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

describe('Type hierarchy visibility and requests', () => {
  it('requests nothing on file load, selection, a definition result or Open (I)', async () => {
    const { requests, onSelectLine } = await showDefinition()
    caretOn(MAIN, 'other')
    fireEvent.mouseUp(document.querySelector('pre') as HTMLPreElement)
    expect(actionButtons().map((b) => b.textContent)).toEqual([DOG_LABEL])
    fireEvent.click(screen.getByRole('button', { name: 'Open pets/dog.py at line 7 (class Dog)' }))
    expect(onSelectLine).toHaveBeenCalledTimes(1)
    await sleep(50)
    expect(byUrl(requests, HIERARCHY_PATH)).toHaveLength(0)
    expect(screen.queryByTestId('type-hierarchy-target')).toBeNull()
  })

  it('sends exactly the class path and its start line as one GET, once (D-H)', async () => {
    const { seen, requests } = await showDefinition()
    fireEvent.click(action(DOG_LABEL))
    await loaded()
    // Not end_line (20), not the cursor line (3) or column (7), nothing else.
    expect(seen).toEqual([{ path: 'pets/dog.py', line: 7 }])
    const hierarchyRequests = byUrl(requests, HIERARCHY_PATH)
    expect(hierarchyRequests.map((r) => [r.method, r.data])).toEqual([['GET', undefined]])
    await sleep(50)
    expect(byUrl(requests, HIERARCHY_PATH)).toHaveLength(1)
  })

  it('offers an action only for classes, in server order; never functions, methods or modules (A, B)', async () => {
    const { seen } = await showDefinition({ status: 200, data: AMBIGUOUS })
    expect(actionButtons().map((b) => b.textContent)).toEqual(CANDIDATE_LABELS)
    expect(regionText()).not.toContain('function')
    expect(regionText()).not.toContain('method')
    expect(regionText()).not.toContain('module')
    fireEvent.click(action(CANDIDATE_LABELS[1]))
    await loaded()
    expect(seen).toEqual([{ path: 'beta/impl.py', line: 4 }]) // the clicked class, not the first
  })

  it.each([
    ['a function', [FUNCTION]],
    ['a method', [METHOD]],
    ['a module', [MODULE]],
    ['no result', null],
    ['no definitions', []],
  ])('renders nothing and requests nothing for %s (B)', async (_name, definitions) => {
    const { container, requests } = renderPanel(definitions)
    expect(container.childElementCount).toBe(0)
    await sleep(30)
    expect(requests).toHaveLength(0)
  })

  it('a non-Python file has no go-to-definition and so no type hierarchy (C)', async () => {
    const { requests } = renderViewer('docs/notes.md', {
      [GET_CONTENT]: content('docs/notes.md', 'class Dog: pass\n'),
    })
    await waitFor(() => expect(codeText()).toBe('class Dog: pass\n'))
    expect(screen.queryByRole('region', { name: 'Go to definition' })).toBeNull()
    expect(screen.queryByRole('region', { name: 'Type hierarchy' })).toBeNull()
    expect(byUrl(requests, HIERARCHY_PATH)).toHaveLength(0)
  })

  it('labels only the clicked class as loading and disables every action meanwhile (J)', async () => {
    const hold = holdHierarchy()
    try {
      await showDefinition({ status: 200, data: AMBIGUOUS })
      fireEvent.click(action(CANDIDATE_LABELS[0]))
      await screen.findByText('Loading type hierarchy for class Runner (zeta/impl.py, line 30)…')
      expect(actionButtons().map((b) => [b.textContent, b.disabled])).toEqual([
        ['Loading type hierarchy for class Runner (zeta/impl.py, line 30)…', true],
        [CANDIDATE_LABELS[1], true],
      ])
      fireEvent.click(actionButtons()[1])
      expect(hold.held).toHaveLength(1)
      hold.held[0].release(hierarchyResult({ target: ZETA }))
      await screen.findByTestId('type-hierarchy-target')
      expect(actionButtons().map((b) => [b.textContent, b.disabled])).toEqual(
        CANDIDATE_LABELS.map((label) => [label, false]),
      )
    } finally {
      hold.spy.mockRestore()
    }
  })

  it('sends an unusual server path unchanged', async () => {
    const odd: DefinitionLocation = { ...DOG, path: 'a b/ü&x=1#.py' }
    const { seen } = renderPanel([odd])
    fireEvent.click(screen.getByRole('button', { name: /^Type hierarchy for/ }))
    await screen.findByTestId('type-hierarchy-target')
    expect(seen).toEqual([{ path: 'a b/ü&x=1#.py', line: 7 }])
  })
})

// --- Results -----------------------------------------------------------------------------

describe('Type hierarchy results', () => {
  it('shows bases in server order with duplicates, every kind and every candidate (K-P, R-U)', async () => {
    await showHierarchy({ status: 200, data: hierarchyResult() })
    expect(targetText()).toBe('Type hierarchy of class Dog (pets/dog.py, line 7)')
    expect(baseSites()).toEqual(BASE_SITES)
    expect(baseKinds()).toEqual(BASE_KINDS)
    expect(baseDefinitions()).toEqual(BASE_DEFINITIONS)
    expect(within(screen.getAllByTestId('base')[1]).getAllByTestId('base-definition')).toHaveLength(4)
  })

  it('shows direct subclasses in server order with duplicates and the ambiguity marker (W, X, AA)', async () => {
    await showHierarchy({ status: 200, data: hierarchyResult() })
    expect(screen.getByRole('heading', { name: 'Direct subclasses' })).toBeTruthy()
    expect(subclassTexts()).toEqual(SUBCLASS_TEXTS)
    expect(siteTexts()).toEqual(SITE_TEXTS)
  })

  it('shows the server summary counts, never recounted (AG)', async () => {
    await showHierarchy({ status: 200, data: hierarchyResult() })
    expect(summaryText()).toBe(SUMMARY_TEXT)
  })

  it('navigates base definitions and every ambiguous candidate to their path and start line (Q)', async () => {
    const { onSelectLine } = await panelResult(hierarchyResult())
    const candidates = within(screen.getAllByTestId('base')[1]).getAllByRole('button', { name: /^Open base/ })
    expect(candidates).toHaveLength(4)
    candidates.forEach((button) => fireEvent.click(button))
    fireEvent.click(within(screen.getAllByTestId('base')[0]).getByRole('button', { name: /^Open base/ }))
    fireEvent.click(within(screen.getAllByTestId('base')[3]).getByRole('button', { name: /^Open base/ }))
    expect(onSelectLine.mock.calls).toEqual([
      ['zeta/mix.py', 9],
      ['alpha/mix.py', 3],
      ['alpha/mix.py', 3],
      ['lib/mix.py', 1],
      ['zoo/base.py', 1],
      ['zoo/base.py', 7], // not_a_class definitions stay navigable
    ])
  })

  it('navigates base sites to the target file (Step 51 BaseSite: a position in target.path) (D5)', async () => {
    // The server's target is authoritative, even when it differs from the clicked definition.
    const moved: DefinitionLocation = { ...DOG, path: 'moved/dog.py', line: 3, end_line: 16 }
    const { onSelectLine } = await panelResult(hierarchyResult({ target: moved }))
    fireEvent.click(screen.getByRole('button', { name: 'Jump to moved/dog.py at line 9 (base Mixin)' }))
    fireEvent.click(screen.getByRole('button', { name: 'Jump to moved/dog.py at line 13 (base Animal)' }))
    expect(onSelectLine.mock.calls).toEqual([
      ['moved/dog.py', 9],
      ['moved/dog.py', 13],
    ])
    // A dynamic base has no site, so nothing to open.
    expect(within(screen.getAllByTestId('base')[7]).queryByRole('button')).toBeNull()
  })

  it('navigates subclasses to subclass.path and their sites to subclass.path at site.line (Y, Z)', async () => {
    const { onSelectLine } = await panelResult(hierarchyResult())
    const entries = screen.getAllByTestId('subclass')
    fireEvent.click(within(entries[1]).getByRole('button', { name: 'Open subclass class Alpha in app/alpha.py at line 10' }))
    fireEvent.click(
      within(entries[1]).getByRole('button', { name: 'Jump to app/alpha.py at line 11 (inherits D, ambiguous)' }),
    )
    fireEvent.click(within(entries[2]).getByRole('button', { name: 'Jump to pets/puppy.py at line 4 (inherits Dog)' }))
    fireEvent.click(within(entries[0]).getByRole('button', { name: /^Open subclass/ }))
    expect(onSelectLine.mock.calls).toEqual([
      ['app/alpha.py', 10],
      ['app/alpha.py', 11],
      ['pets/puppy.py', 4],
      ['pets/puppy.py', 2],
    ])
  })

  it('shows empty bases and empty subclasses (V, AB)', async () => {
    await panelResult(hierarchyResult({ bases: [], subclasses: [] }))
    expect(regionText()).toContain(NO_BASES)
    expect(regionText()).toContain(NO_SUBCLASSES)
    expect(regionText()).not.toContain(SUBCLASSES_UNSUPPORTED)
    expect(screen.queryAllByTestId('base')).toHaveLength(0)
  })

  it('unsupported subclass analysis is never shown as an empty list; bases still shown (AC)', async () => {
    await panelResult(
      hierarchyResult({ subclasses_status: 'unsupported_target', subclasses: null, subclasses_truncated: null }),
    )
    expect(regionText()).toContain(SUBCLASSES_UNSUPPORTED)
    expect(regionText()).not.toContain(NO_SUBCLASSES)
    expect(baseSites()).toEqual(BASE_SITES)
    expect(screen.queryAllByTestId('subclass')).toHaveLength(0)
  })

  it('null subclasses are unsupported, not empty', async () => {
    await panelResult(hierarchyResult({ subclasses: null, subclasses_truncated: null }))
    expect(regionText()).toContain(SUBCLASSES_UNSUPPORTED)
    expect(regionText()).not.toContain(NO_SUBCLASSES)
  })

  it.each([
    [true, false],
    [false, true],
    [true, true],
    [false, false],
  ])('shows each truncation notice only for its own flag: bases %s, subclasses %s (AD-AF)', async (bases, subclasses) => {
    await panelResult(hierarchyResult({ bases_truncated: bases, subclasses_truncated: subclasses }))
    expect(regionText().includes(BASES_TRUNCATED)).toBe(bases)
    expect(regionText().includes(SUBCLASSES_TRUNCATED)).toBe(subclasses)
    if (bases) {
      // Under the Bases section, before the subclasses heading.
      const text = regionText()
      expect(text.indexOf(BASES_TRUNCATED)).toBeLessThan(text.indexOf('Direct subclasses'))
    }
    if (subclasses) {
      const text = regionText()
      expect(text.indexOf(SUBCLASSES_TRUNCATED)).toBeGreaterThan(text.indexOf('Direct subclasses'))
    }
  })

  it.each(Object.entries(STATUS_MESSAGES))('status %s shows only its message (AH-AL)', async (status, message) => {
    renderPanel([DOG], { status: 200, data: notScanned(status) })
    fireEvent.click(screen.getByRole('button', { name: DOG_LABEL }))
    await screen.findByText(message)
    expect(targetText()).toBe('Type hierarchy of class Dog (pets/dog.py, line 7)') // the requested class
    expect(screen.queryByTestId('type-hierarchy-summary')).toBeNull()
    expect(screen.queryByText(NO_BASES)).toBeNull()
    expect(screen.queryByText(NO_SUBCLASSES)).toBeNull()
  })

  it('has the approved status wording', () => {
    expect(STATUS_MESSAGES).toEqual({
      not_a_definition: 'Type hierarchy is unavailable: no class starts on this line.',
      not_a_class: 'Type hierarchy is only available for classes.',
      syntax_error: 'Type hierarchy is unavailable: the target file could not be parsed as Python.',
      too_large: 'Type hierarchy is unavailable: the target file is too large to analyze.',
      binary: 'Type hierarchy is unavailable: the target file is not text.',
      unsupported_language: 'Type hierarchy is only available for Python files.',
    })
  })

  it('shows a safe fallback for an unknown status', async () => {
    renderPanel([DOG], { status: 200, data: notScanned('something_new') })
    fireEvent.click(screen.getByRole('button', { name: DOG_LABEL }))
    await screen.findByText(UNKNOWN_STATUS)
    expect(screen.queryByTestId('type-hierarchy-summary')).toBeNull()
  })

  it('without onSelectLine, definitions and sites are plain text', async () => {
    renderPanel([DOG])
    fireEvent.click(screen.getByRole('button', { name: DOG_LABEL }))
    await screen.findByTestId('type-hierarchy-target')
    expect(baseSites()).toEqual(BASE_SITES)
    expect(subclassTexts()).toEqual(SUBCLASS_TEXTS)
    expect(screen.queryAllByRole('button', { name: /^(Jump|Open) / })).toHaveLength(0)
  })
})

// --- Errors --------------------------------------------------------------------------------

describe('Type hierarchy errors', () => {
  it.each([
    [404, 'Repository not found'],
    [409, 'Repository changed during analysis; run it again'],
    [429, 'Too many attempts; please try again later'],
  ])('shows the server detail for %i exactly, without retrying (AM)', async (status, detail) => {
    const { requests } = await showHierarchy({ status, data: { detail } })
    const alerts = await screen.findAllByRole('alert')
    expect(alerts.map((a) => a.textContent)).toEqual([detail])
    await sleep(50)
    expect(byUrl(requests, HIERARCHY_PATH)).toHaveLength(1)
    expect(screen.queryByTestId('base')).toBeNull()
  })

  it('shows the fallback for an error without a usable detail, without retrying a 500 (AN, AQ)', async () => {
    const { requests } = await showHierarchy({ status: 500, data: {} })
    expect((await screen.findByRole('alert')).textContent).toBe('Could not load the type hierarchy')
    await sleep(50)
    expect(byUrl(requests, HIERARCHY_PATH)).toHaveLength(1)
  })

  it('uses the existing network message without retrying', async () => {
    const { requests } = await showHierarchy(() => {
      throw new AxiosError('Network Error', AxiosError.ERR_NETWORK)
    })
    expect((await screen.findByRole('alert')).textContent).toBe('Could not reach the server')
    await sleep(50)
    expect(byUrl(requests, HIERARCHY_PATH)).toHaveLength(1)
  })
})

// --- Stale results -------------------------------------------------------------------------

describe('Type hierarchy never shows a stale result', () => {
  it('a new go-to-definition lookup drops a request that is still loading (AP)', async () => {
    const hold = holdHierarchy()
    try {
      let lookups = 0
      await showDefinition(() => {
        lookups += 1
        return { status: 200, data: lookups === 1 ? definitionResult() : AMBIGUOUS }
      })
      fireEvent.click(action(DOG_LABEL))
      await screen.findByText('Loading type hierarchy for class Dog (pets/dog.py, line 7)…')
      caretOn(MAIN, 'other')
      fireEvent.click(definitionButton())
      await screen.findByText('5 possible definitions of', { exact: false })
      expect(actionButtons().map((b) => [b.textContent, b.disabled])).toEqual(
        CANDIDATE_LABELS.map((label) => [label, false]),
      )
      hold.held[0].release(hierarchyResult())
      await sleep(50)
      expect(screen.queryByTestId('type-hierarchy-target')).toBeNull()
      expect(screen.queryByText('Puppy', { exact: false })).toBeNull()
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
    expect(actionButtons().map((b) => b.textContent)).toEqual([DOG_LABEL])
    expect(screen.queryByTestId('type-hierarchy-target')).toBeNull()
    expect(screen.queryByTestId('base')).toBeNull()
    expect(byUrl(requests, HIERARCHY_PATH)).toHaveLength(1)
  })

  const contentRoute: Handler = (config) => {
    const path = (config.params as { path: string }).path
    return content(path, path === 'app/main.py' ? MAIN : OTHER)
  }

  it('never shows a result for a file that is no longer open (AO)', async () => {
    const hold = holdHierarchy()
    try {
      const { rerenderPath } = renderViewer('app/main.py', {
        [GET_CONTENT]: contentRoute,
        [GET_DEFINITION]: { status: 200, data: definitionResult() },
      })
      await waitFor(() => expect(codeText()).toBe(MAIN))
      caretOn(MAIN, 'Dog()')
      fireEvent.click(definitionButton())
      await definitionList()
      fireEvent.click(action(DOG_LABEL))
      await screen.findByRole('button', { name: /^Loading type hierarchy/ })
      rerenderPath('app/other.py')
      await waitFor(() => expect(codeText()).toBe(OTHER))
      hold.held[0].release(hierarchyResult())
      await sleep(50)
      expect(screen.queryByRole('region', { name: 'Type hierarchy' })).toBeNull()
      expect(screen.queryByText('Puppy', { exact: false })).toBeNull()
    } finally {
      hold.spy.mockRestore()
    }
  })

  it('starts fresh after returning to a file, with no request and no cache', async () => {
    const { rerenderPath, requests } = renderViewer('app/main.py', {
      [GET_CONTENT]: contentRoute,
      [GET_DEFINITION]: { status: 200, data: definitionResult() },
      [GET_HIERARCHY]: { status: 200, data: hierarchyResult() },
    })
    await waitFor(() => expect(codeText()).toBe(MAIN))
    caretOn(MAIN, 'Dog()')
    fireEvent.click(definitionButton())
    await definitionList()
    fireEvent.click(action(DOG_LABEL))
    await loaded()
    rerenderPath('app/other.py')
    await waitFor(() => expect(codeText()).toBe(OTHER))
    rerenderPath('app/main.py')
    await waitFor(() => expect(codeText()).toBe(MAIN))
    caretOn(MAIN, 'Dog()')
    fireEvent.click(definitionButton())
    await definitionList()
    await sleep(50)
    expect(screen.queryByTestId('type-hierarchy-target')).toBeNull()
    expect(byUrl(requests, HIERARCHY_PATH)).toHaveLength(1)
  })
})

// --- Security ------------------------------------------------------------------------------

describe('Type hierarchy security', () => {
  it('renders server text as plain text, never as HTML (AS)', async () => {
    const hostile: DefinitionLocation = { path: `${HTML}.py`, kind: 'class', name: HTML, line: 3, end_line: 4 }
    renderPanel([hostile], {
      status: 200,
      data: hierarchyResult({
        target: hostile,
        bases: [{ site: { line: 3, column: 9, name: HTML }, kind: 'ambiguous', definitions: [hostile, hostile] }],
        subclasses: [{ subclass: hostile, site: { line: 5, column: 6, name: HTML, ambiguous: true } }],
      }),
    })
    fireEvent.click(screen.getByRole('button', { name: /^Type hierarchy for/ }))
    await screen.findByTestId('type-hierarchy-target')
    expect(baseSites()).toEqual([`${HTML} line 3, column 9`])
    expect(subclassTexts()).toEqual([`${HTML} class ${HTML}.py lines 3–4`])
    expect(document.querySelector('img, script, a, iframe')).toBeNull()
    expect(document.querySelector('[href], [src], [onerror]')).toBeNull()
  })

  it('shows only the named fields of the response', async () => {
    const extra = { snippet: 'FAKE-SNIPPET-MARKER', sha256: 'FAKE-SHA-MARKER', id: 'FAKE-ID-MARKER' }
    const payload = {
      ...hierarchyResult({
        bases: BASES.map((b) => ({ ...b, ...extra, definitions: b.definitions.map((d) => ({ ...d, ...extra })) })),
        subclasses: SUBCLASSES.map((s) => ({ ...s, ...extra, site: { ...s.site, ...extra } })),
      }),
      target: { ...DOG, ...extra },
      summary: { ...SUMMARY, ...extra },
      ...extra,
    }
    renderPanel([DOG], { status: 200, data: payload }, vi.fn())
    fireEvent.click(screen.getByRole('button', { name: DOG_LABEL }))
    await screen.findByTestId('type-hierarchy-target')
    const labels = Array.from(region().querySelectorAll('[aria-label]')).map((e) => e.getAttribute('aria-label'))
    for (const marker of Object.values(extra)) {
      expect(regionText()).not.toContain(marker)
      expect(labels.join(' ')).not.toContain(marker)
    }
    expect(regionText()).not.toContain('{')
    expect(regionText()).not.toContain('subclasses_status')
    expect(regionText()).not.toContain('"kind"')
  })

  it('uses no browser storage or cookies and calls only the app API (AR)', async () => {
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
      fireEvent.click(screen.getByRole('button', { name: 'Jump to pets/puppy.py at line 3 (inherits Dog)' }))
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
})

// --- Existing go-to-definition behaviour ----------------------------------------------------

describe('Go to definition with the type hierarchy panel', () => {
  it('keeps definitions, references and call hierarchy, with type hierarchy after them (AT)', async () => {
    const { requests, onSelectLine } = renderViewer('app/main.py', {
      [GET_CONTENT]: content('app/main.py', MAIN),
      [GET_DEFINITION]: { status: 200, data: definitionResult() },
      [GET_CALLS]: { status: 200, data: { detail: 'unused' } },
    })
    await waitFor(() => expect(codeText()).toBe(MAIN))
    caretOn(MAIN, 'Dog()')
    fireEvent.click(definitionButton())
    const list = await definitionList()
    expect(within(list).getAllByTestId('definition').map((d) => d.textContent)).toEqual([
      'Dog class pets/dog.py lines 7–20',
    ])
    const definition = screen.getByRole('region', { name: 'Go to definition' })
    const panels = within(definition)
      .getAllByRole('region')
      .map((r) => r.getAttribute('aria-label'))
    expect(panels).toEqual(['Find references', 'Call hierarchy', 'Type hierarchy'])
    fireEvent.click(screen.getByRole('button', { name: 'Open pets/dog.py at line 7 (class Dog)' }))
    expect(onSelectLine).toHaveBeenCalledWith('pets/dog.py', 7)
    await sleep(30)
    expect(requests.map((r) => `${r.method} ${r.url}`)).toEqual([GET_CONTENT, GET_DEFINITION])
  })
})
