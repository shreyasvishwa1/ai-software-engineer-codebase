import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { AxiosRequestConfig, InternalAxiosRequestConfig } from 'axios'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { apiClient } from '../services/client'
import { mockApi, type MockResponse, PROJECT, ZIP_REPOSITORY } from '../test/mockApi'
import { createTestQueryClient } from '../test/render'
import type {
  CallHierarchyResult,
  CodeOutline,
  CodeReferencesResult,
  DefinitionLocation,
  FileContent,
  MethodOverridesResult,
  OutlineSymbol,
  TypeHierarchyResult,
} from '../types/api'
import { FileOutline, STATUS_MESSAGES, UNKNOWN_STATUS } from './FileOutline'
import { FileViewer } from './FileViewer'

const P = PROJECT.id
const R = ZIP_REPOSITORY.id
const BASE = `/projects/${P}/repositories/${R}`
const OUTLINE_PATH = `${BASE}/files/outline`
const REFERENCES_PATH = `${BASE}/files/references`
const CALLS_PATH = `${BASE}/files/call-hierarchy`
const TYPES_PATH = `${BASE}/files/type-hierarchy`
const OVERRIDES_PATH = `${BASE}/files/method-overrides`
const CONTENT_PATH = `${BASE}/files/content`

// The outline is requested for REQUESTED and answered for CANONICAL: analyses must use CANONICAL.
const REQUESTED = 'requested/models.py'
const CANONICAL = 'pkg/models.py'
const HTML = '<img src=x onerror=alert(1)>'
const CHANGED = 'Repository changed during analysis; run it again'

const SYMBOLS: OutlineSymbol[] = [
  { kind: 'class', async: false, name: 'Widget', line: 3, end_line: 12, depth: 0 },
  { kind: 'method', async: false, name: 'render', line: 4, end_line: 5, depth: 1 },
  { kind: 'method', async: true, name: 'fetch', line: 7, end_line: 8, depth: 1 },
  { kind: 'class', async: false, name: 'Meta', line: 10, end_line: 11, depth: 1 },
  { kind: 'function', async: false, name: 'main', line: 14, end_line: 17, depth: 0 },
  { kind: 'function', async: false, name: 'inner', line: 15, end_line: 16, depth: 1 },
]
const [WIDGET, RENDER, FETCH, META, MAIN, INNER] = SYMBOLS
const ROW_TEXTS = [
  'classWidgetline 3–12',
  'methodrenderline 4–5',
  'methodasyncfetchline 7–8',
  'classMetaline 10–11',
  'functionmainline 14–17',
  'functioninnerline 15–16',
]
const ROW_LABELS = [
  'Go to class Widget (line 3)',
  'Go to method render (line 4)',
  'Go to method fetch (line 7)',
  'Go to class Meta (line 10)',
  'Go to function main (line 14)',
  'Go to function inner (line 15)',
]

type PanelKey = 'references' | 'calls' | 'types' | 'overrides'
// Each existing panel: its region, endpoint, button wording and result marker (all unchanged).
const PANELS: Record<PanelKey, { region: string; url: string; verb: string; testId: string }> = {
  references: { region: 'Find references', url: REFERENCES_PATH, verb: 'Find references to', testId: 'references-target' },
  calls: { region: 'Call hierarchy', url: CALLS_PATH, verb: 'Call hierarchy for', testId: 'call-hierarchy-target' },
  types: { region: 'Type hierarchy', url: TYPES_PATH, verb: 'Type hierarchy for', testId: 'type-hierarchy-target' },
  overrides: { region: 'Method overrides', url: OVERRIDES_PATH, verb: 'Method overrides for', testId: 'method-overrides-target' },
}
const CLASS_PANELS: PanelKey[] = ['references', 'calls', 'types']
const FUNCTION_PANELS: PanelKey[] = ['references', 'calls']
const METHOD_PANELS: PanelKey[] = ['references', 'calls', 'overrides']

const buttonLabel = (key: PanelKey, symbol: OutlineSymbol) =>
  `${PANELS[key].verb} ${symbol.kind} ${symbol.name} (${CANONICAL}, line ${symbol.line})`
const symbolLabel = (symbol: OutlineSymbol) => `Go to ${symbol.kind} ${symbol.name} (line ${symbol.line})`

function outline(extra: Partial<CodeOutline> = {}): CodeOutline {
  return { path: CANONICAL, status: 'parsed', symbols: SYMBOLS, truncated: false, ...extra }
}

/** The server's own view of a target: the canonical file and the symbol's statement. */
function definition(symbol: OutlineSymbol): DefinitionLocation {
  return { path: CANONICAL, kind: symbol.kind, name: symbol.name, line: symbol.line, end_line: symbol.end_line }
}

function referencesResult(symbol: OutlineSymbol, from = 'app/use.py'): CodeReferencesResult {
  return {
    status: 'scanned',
    target: definition(symbol),
    references: [{ path: from, line: 9, column: 5, name: symbol.name, context: 'name', ambiguous: false }],
    summary: { files_scanned: 2, too_large: 0, binary: 0, syntax_error: 0, references: 1, ambiguous: 0, unsupported: 0 },
    truncated: false,
  }
}

function callsResult(symbol: OutlineSymbol): CallHierarchyResult {
  const zero = { files_scanned: 0, too_large: 0, binary: 0, syntax_error: 0, caller_calls: 0, caller_ambiguous: 0 }
  return {
    status: 'scanned',
    target: definition(symbol),
    callers_status: 'scanned',
    callers: [],
    callees: [],
    summary: {
      ...zero,
      caller_unsupported: 0,
      non_call_references: 0,
      callee_calls: 0,
      callee_ambiguous: 0,
      callee_unsupported: 0,
      callee_builtin: 0,
      callee_unresolved: 0,
      callee_not_callable: 0,
      callee_dynamic: 0,
    },
    callers_truncated: false,
    callees_truncated: false,
  }
}

function typesResult(symbol: OutlineSymbol): TypeHierarchyResult {
  return {
    status: 'scanned',
    target: definition(symbol),
    bases: [],
    subclasses_status: 'scanned',
    subclasses: [],
    summary: {
      files_scanned: 0,
      too_large: 0,
      binary: 0,
      syntax_error: 0,
      bases: 0,
      bases_resolved: 0,
      bases_ambiguous: 0,
      bases_builtin: 0,
      bases_unresolved: 0,
      bases_unsupported: 0,
      bases_dynamic: 0,
      bases_not_a_class: 0,
      keyword_arguments: 0,
      subclasses: 0,
      subclasses_ambiguous: 0,
      subclass_unsupported: 0,
      non_base_references: 0,
    },
    bases_truncated: false,
    subclasses_truncated: false,
  }
}

function overridesResult(symbol: OutlineSymbol): MethodOverridesResult {
  return {
    status: 'scanned',
    target: definition(symbol),
    owner: definition(WIDGET),
    overridden: [],
    overriding_status: 'scanned',
    overriding: [],
    summary: {
      files_scanned: 0,
      too_large: 0,
      binary: 0,
      syntax_error: 0,
      bases: 0,
      base_classes: 0,
      overridden_defined: 0,
      overridden_not_defined: 0,
      overridden_not_a_method: 0,
      overridden_unavailable: 0,
      overridden_unsupported: 0,
      subclasses: 0,
      overriding: 0,
      inheriting: 0,
      overriding_not_a_method: 0,
      overriding_unsupported: 0,
    },
    overridden_truncated: false,
    overriding_truncated: false,
  }
}

type Handler = MockResponse | ((config: InternalAxiosRequestConfig) => MockResponse)
type Navigate = (path: string, line: number) => void
type Params = Record<string, unknown>
interface Seen {
  url: string
  params: Params
}

/** Answers an analysis for the symbol on the requested line (404 for any other line). */
function analysisRoute(respond: (symbol: OutlineSymbol) => unknown): Handler {
  return (config) => {
    const symbol = SYMBOLS.find((s) => s.line === (config.params as Params).line)
    return symbol ? { status: 200, data: respond(symbol) } : { status: 404, data: { detail: 'Not found' } }
  }
}

function defaultRoutes(): Record<string, Handler> {
  return {
    [OUTLINE_PATH]: { status: 200, data: outline() },
    [REFERENCES_PATH]: analysisRoute((symbol) => referencesResult(symbol)),
    [CALLS_PATH]: analysisRoute(callsResult),
    [TYPES_PATH]: analysisRoute(typesResult),
    [OVERRIDES_PATH]: analysisRoute(overridesResult),
  }
}

/** GET routes by URL; every request's URL and exact query parameters are recorded. */
function serve(routes: Record<string, Handler>) {
  const seen: Seen[] = []
  const requests = mockApi(
    Object.fromEntries(
      Object.entries(routes).map(([url, handler]) => [
        `GET ${url}`,
        (config: InternalAxiosRequestConfig) => {
          seen.push({ url, params: { ...(config.params as Params) } })
          return typeof handler === 'function' ? handler(config) : handler
        },
      ]),
    ),
  )
  return { seen, requests }
}

function renderOutline(
  options: {
    outlineRoute?: Handler
    routes?: Record<string, Handler>
    language?: string | null
    navigable?: boolean
  } = {},
) {
  const { outlineRoute, routes = {}, language = 'Python', navigable = true } = options
  const served = serve({ ...defaultRoutes(), ...(outlineRoute ? { [OUTLINE_PATH]: outlineRoute } : {}), ...routes })
  const onSelectLine = vi.fn<Navigate>()
  render(
    <QueryClientProvider client={createTestQueryClient()}>
      <FileOutline
        projectId={P}
        repositoryId={R}
        path={REQUESTED}
        language={language}
        onSelectLine={navigable ? onSelectLine : undefined}
      />
    </QueryClientProvider>,
  )
  return { ...served, onSelectLine }
}

/** Holds every request to `url` until released; other requests reach the mocked transport. */
function hold(url: string) {
  const held: { params: Params; release: (data: unknown) => void }[] = []
  const realGet = apiClient.get.bind(apiClient)
  vi.spyOn(apiClient, 'get').mockImplementation(((requested: string, config?: AxiosRequestConfig) => {
    if (requested === url) {
      return new Promise<unknown>((resolve) => {
        held.push({ params: { ...(config?.params as Params) }, release: (data) => resolve({ data }) })
      })
    }
    return realGet(requested, config)
  }) as typeof apiClient.get)
  return held
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))
const outlineRegion = () => screen.getByRole('region', { name: 'File outline' })
const outlineList = () => screen.getByRole('list', { name: 'Outline' })
const analyses = () => screen.queryByRole('region', { name: 'Symbol analyses' })
const subject = () => screen.queryByTestId('symbol-analyses-subject')?.textContent ?? null
const panelNames = () => {
  const section = analyses()
  return section ? within(section).queryAllByRole('region').map((region) => region.getAttribute('aria-label')) : []
}
const panelRegion = (key: PanelKey) =>
  within(analyses() as HTMLElement).getByRole('region', { name: PANELS[key].region })
/** A panel's own action button (it comes before any result row). */
const actionButton = (key: PanelKey) => within(panelRegion(key)).getAllByRole('button')[0] as HTMLButtonElement
const analysisSeen = (seen: Seen[]) => seen.filter((entry) => entry.url !== OUTLINE_PATH)
const choose = (symbol: OutlineSymbol) => fireEvent.click(screen.getByRole('button', { name: symbolLabel(symbol) }))

async function showOutline() {
  fireEvent.click(screen.getByRole('button', { name: 'Show outline' }))
  await screen.findByRole('list', { name: 'Outline' })
}

/** Clicks a panel's button for `symbol` and waits for that panel's result. */
async function analyze(key: PanelKey, symbol: OutlineSymbol) {
  fireEvent.click(within(panelRegion(key)).getByRole('button', { name: buttonLabel(key, symbol) }))
  await within(panelRegion(key)).findByTestId(PANELS[key].testId)
}

afterEach(() => {
  vi.restoreAllMocks()
})

// --- A. The existing outline -------------------------------------------------------------------

describe('Outline symbol analyses: the existing outline', () => {
  it('keeps rows, text, buttons, order and indentation, with no analyses or extra request before a selection', async () => {
    const { requests, seen } = renderOutline()
    await showOutline()
    const rows = within(outlineList()).getAllByTestId('outline-symbol')
    expect(rows.map((row) => row.textContent)).toEqual(ROW_TEXTS)
    expect(rows.map((row) => row.style.paddingLeft)).toEqual(['0rem', '1rem', '1rem', '1rem', '0rem', '1rem'])
    expect(within(outlineList()).getAllByRole('button').map((b) => b.getAttribute('aria-label'))).toEqual(ROW_LABELS)
    await sleep(30)
    expect(analyses()).toBeNull()
    expect(requests.map((r) => `${r.method} ${r.url}`)).toEqual([`GET ${OUTLINE_PATH}`])
    expect(seen).toEqual([{ url: OUTLINE_PATH, params: { path: REQUESTED } }])
  })

  it('leaves every row untouched after a selection and puts the analyses after the list, in no row', async () => {
    renderOutline()
    await showOutline()
    choose(MAIN)
    const section = analyses() as HTMLElement
    expect(section).not.toBeNull()
    const list = outlineList()
    expect(list.contains(section)).toBe(false)
    expect(outlineRegion().contains(section)).toBe(true)
    expect(list.compareDocumentPosition(section) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(within(list).getAllByTestId('outline-symbol').map((row) => row.textContent)).toEqual(ROW_TEXTS)
    expect(within(list).getAllByRole('button').map((b) => b.getAttribute('aria-label'))).toEqual(ROW_LABELS)
  })

  it('still navigates with the existing path and line on every click', async () => {
    const { onSelectLine } = renderOutline()
    await showOutline()
    choose(WIDGET)
    choose(MAIN)
    choose(MAIN)
    expect(onSelectLine.mock.calls).toEqual([
      [REQUESTED, 3],
      [REQUESTED, 14],
      [REQUESTED, 14],
    ])
  })

  it.each([['Markdown'], [null], ['JavaScript']])('renders and requests nothing for language %s', async (language) => {
    const { requests } = renderOutline({ language })
    await sleep(30)
    expect(screen.queryByRole('region', { name: 'File outline' })).toBeNull()
    expect(analyses()).toBeNull()
    expect(requests).toHaveLength(0)
  })

  it('offers no analyses without navigation, where symbols stay plain text', async () => {
    const { requests } = renderOutline({ navigable: false })
    await showOutline()
    expect(within(outlineList()).queryAllByRole('button')).toHaveLength(0)
    expect(analyses()).toBeNull()
    expect(requests.map((r) => r.url)).toEqual([OUTLINE_PATH])
  })
})

// --- B. Selection and C. kind filtering ----------------------------------------------------------

describe('Outline symbol analyses: selection and kinds', () => {
  it.each([
    ['class', WIDGET, CLASS_PANELS],
    ['method', RENDER, METHOD_PANELS],
    ['async method', FETCH, METHOD_PANELS],
    ['nested class', META, CLASS_PANELS],
    ['function', MAIN, FUNCTION_PANELS],
    ['nested function', INNER, FUNCTION_PANELS],
  ])('selects the %s, navigates, and shows exactly its panels for its canonical target', async (_label, symbol, panels) => {
    const { onSelectLine } = renderOutline()
    await showOutline()
    choose(symbol)
    expect(onSelectLine.mock.calls).toEqual([[REQUESTED, symbol.line]])
    expect(subject()).toBe(`Analyses for ${symbol.kind} ${symbol.name} (line ${symbol.line})`)
    expect(panelNames()).toEqual(panels.map((key) => PANELS[key].region))
    const buttons = within(analyses() as HTMLElement).getAllByRole('button') as HTMLButtonElement[]
    expect(buttons.map((button) => button.textContent)).toEqual(panels.map((key) => buttonLabel(key, symbol)))
    expect(buttons.map((button) => button.disabled)).toEqual(panels.map(() => false))
  })

  it('never shows method overrides for a class, nor a type hierarchy for a function or method', async () => {
    renderOutline()
    await showOutline()
    choose(WIDGET)
    expect(panelNames()).not.toContain('Method overrides')
    choose(MAIN)
    expect(panelNames()).not.toContain('Type hierarchy')
    expect(panelNames()).not.toContain('Method overrides')
    choose(RENDER)
    expect(panelNames()).not.toContain('Type hierarchy')
  })

  it('follows the latest selection', async () => {
    renderOutline()
    await showOutline()
    choose(WIDGET)
    expect(subject()).toBe('Analyses for class Widget (line 3)')
    choose(RENDER)
    expect(subject()).toBe('Analyses for method render (line 4)')
    expect(panelNames()).toEqual(['Find references', 'Call hierarchy', 'Method overrides'])
    choose(MAIN)
    expect(subject()).toBe('Analyses for function main (line 14)')
  })

  it.each([['module'], ['variable'], ['<script>x</script>']])(
    'navigates but creates no analyses for a %s symbol, and drops an earlier selection',
    async (kind) => {
      const odd = { kind, async: false, name: 'thing', line: 20, end_line: 20, depth: 0 } as unknown as OutlineSymbol
      const { onSelectLine, requests } = renderOutline({
        outlineRoute: { status: 200, data: outline({ symbols: [...SYMBOLS, odd] }) },
      })
      await showOutline()
      choose(MAIN)
      expect(analyses()).not.toBeNull()
      fireEvent.click(screen.getByRole('button', { name: `Go to ${kind} thing (line 20)` }))
      expect(onSelectLine.mock.lastCall).toEqual([REQUESTED, 20])
      expect(analyses()).toBeNull()
      expect(subject()).toBeNull()
      await sleep(30)
      expect(requests.map((r) => r.url)).toEqual([OUTLINE_PATH])
    },
  )

  it('names only the kind, name and line in the subject', async () => {
    renderOutline()
    await showOutline()
    choose(RENDER)
    const text = subject() ?? ''
    expect(text).toBe('Analyses for method render (line 4)')
    for (const hidden of [CANONICAL, REQUESTED, P, R]) {
      expect(text).not.toContain(hidden)
    }
    expect(text).not.toMatch(/error|diagnostic/i)
  })
})

// --- D. Explicit requests, E. exact targets and F. the canonical path ---------------------------------

describe('Outline symbol analyses: requests', () => {
  it('sends nothing when selecting symbols and rendering their analyses', async () => {
    const { requests } = renderOutline()
    await showOutline()
    for (const symbol of [WIDGET, RENDER, MAIN, MAIN]) {
      choose(symbol)
      await sleep(20)
    }
    expect(analyses()).not.toBeNull()
    expect(requests.map((r) => r.url)).toEqual([OUTLINE_PATH])
  })

  it.each([
    ['references', MAIN],
    ['calls', MAIN],
    ['types', WIDGET],
    ['overrides', RENDER],
  ] as [PanelKey, OutlineSymbol][])(
    'only the %s button requests, once, with exactly the canonical { path, line }',
    async (key, symbol) => {
      const { seen, requests } = renderOutline()
      await showOutline()
      choose(symbol)
      await analyze(key, symbol)
      const sent = analysisSeen(seen)
      expect(sent).toEqual([{ url: PANELS[key].url, params: { path: CANONICAL, line: symbol.line } }])
      for (const field of ['name', 'kind', 'end_line', 'column']) {
        expect(sent[0].params).not.toHaveProperty(field)
      }
      const analysis = requests.filter((r) => r.url !== OUTLINE_PATH)
      expect(analysis.map((r) => [r.method, r.baseURL, r.data])).toEqual([['GET', '/api/v1', undefined]])
    },
  )

  it('requests only the clicked panel: references never request the call hierarchy, and the reverse', async () => {
    const { seen } = renderOutline()
    await showOutline()
    choose(MAIN)
    await analyze('references', MAIN)
    expect(analysisSeen(seen).map((entry) => entry.url)).toEqual([REFERENCES_PATH])
    await analyze('calls', MAIN)
    expect(analysisSeen(seen).map((entry) => entry.url)).toEqual([REFERENCES_PATH, CALLS_PATH])
  })

  it('targets the outline response path in every analysis, never the requested path', async () => {
    const { seen } = renderOutline()
    await showOutline()
    expect(seen).toEqual([{ url: OUTLINE_PATH, params: { path: REQUESTED } }])
    choose(WIDGET)
    await analyze('references', WIDGET)
    await analyze('calls', WIDGET)
    await analyze('types', WIDGET)
    choose(RENDER)
    await analyze('overrides', RENDER)
    const sent = analysisSeen(seen)
    expect(sent.map((entry) => entry.url)).toEqual([REFERENCES_PATH, CALLS_PATH, TYPES_PATH, OVERRIDES_PATH])
    expect(sent.map((entry) => entry.params)).toEqual([
      { path: CANONICAL, line: 3 },
      { path: CANONICAL, line: 3 },
      { path: CANONICAL, line: 3 },
      { path: CANONICAL, line: 4 },
    ])
  })

  it('opens result locations through the outline navigation', async () => {
    const { onSelectLine } = renderOutline()
    await showOutline()
    choose(MAIN)
    await analyze('references', MAIN)
    fireEvent.click(
      within(panelRegion('references')).getByRole('button', { name: 'Go to app/use.py at line 9 (name main)' }),
    )
    expect(onSelectLine.mock.lastCall).toEqual(['app/use.py', 9])
  })
})

// --- G. Fresh analysis contexts -------------------------------------------------------------------

describe('Outline symbol analyses: every selection is a fresh context', () => {
  it.each([
    ['references', MAIN, INNER],
    ['calls', MAIN, INNER],
    ['types', WIDGET, META],
    ['overrides', RENDER, FETCH],
  ] as [PanelKey, OutlineSymbol, OutlineSymbol][])(
    '%s: another symbol, then the same symbol again, both start fresh',
    async (key, first, second) => {
      const { seen } = renderOutline()
      await showOutline()
      choose(first)
      await analyze(key, first)
      choose(second)
      expect(screen.queryByTestId(PANELS[key].testId)).toBeNull()
      expect([actionButton(key).textContent, actionButton(key).disabled]).toEqual([buttonLabel(key, second), false])
      choose(first)
      expect(screen.queryByTestId(PANELS[key].testId)).toBeNull()
      expect([actionButton(key).textContent, actionButton(key).disabled]).toEqual([buttonLabel(key, first), false])
      await sleep(30)
      expect(analysisSeen(seen)).toHaveLength(1) // nothing replayed or requested again
    },
  )

  it('never replays a result from a cache: the same symbol requests again and shows the new answer', async () => {
    let answers = 0
    const { seen } = renderOutline({
      routes: {
        [REFERENCES_PATH]: () => ({
          status: 200,
          data: referencesResult(MAIN, ++answers === 1 ? 'app/first.py' : 'app/second.py'),
        }),
      },
    })
    const sources = () => screen.queryAllByTestId('reference').map((row) => row.textContent?.split(' ')[0])
    await showOutline()
    choose(MAIN)
    await analyze('references', MAIN)
    expect(sources()).toEqual(['app/first.py'])
    choose(MAIN)
    expect(sources()).toEqual([])
    await analyze('references', MAIN)
    expect(sources()).toEqual(['app/second.py'])
    expect(analysisSeen(seen)).toHaveLength(2)
  })

  it.each([
    ['another symbol', INNER],
    ['the same symbol again', MAIN],
  ])('never resumes a pending request after selecting %s', async (_label, next) => {
    const held = hold(REFERENCES_PATH)
    renderOutline()
    await showOutline()
    choose(MAIN)
    fireEvent.click(within(panelRegion('references')).getByRole('button', { name: buttonLabel('references', MAIN) }))
    await waitFor(() =>
      expect(actionButton('references').textContent).toBe(`Finding references to function main (${CANONICAL}, line 14)…`),
    )
    choose(next)
    expect([actionButton('references').textContent, actionButton('references').disabled]).toEqual([
      buttonLabel('references', next),
      false,
    ])
    held[0].release(referencesResult(MAIN))
    await sleep(50)
    expect(screen.queryAllByTestId('reference')).toHaveLength(0)
    expect(screen.queryByTestId('references-target')).toBeNull()
    expect(held).toHaveLength(1)
  })

  it.each([
    ['another symbol', INNER],
    ['the same symbol again', MAIN],
  ])('never carries an error over to %s', async (_label, next) => {
    renderOutline({ routes: { [REFERENCES_PATH]: { status: 409, data: { detail: CHANGED } } } })
    await showOutline()
    choose(MAIN)
    fireEvent.click(within(panelRegion('references')).getByRole('button', { name: buttonLabel('references', MAIN) }))
    expect((await within(panelRegion('references')).findByRole('alert')).textContent).toBe(CHANGED)
    choose(next)
    expect(within(analyses() as HTMLElement).queryByRole('alert')).toBeNull()
    expect(actionButton('references').textContent).toBe(buttonLabel('references', next))
  })
})

// --- H. A new outline clears the selection --------------------------------------------------------

describe('Outline symbol analyses: outline refresh and errors', () => {
  it('clears the selection as soon as Refresh outline is clicked; a later click starts fresh', async () => {
    const { seen } = renderOutline()
    await showOutline()
    choose(MAIN)
    await analyze('references', MAIN)
    fireEvent.click(screen.getByRole('button', { name: 'Refresh outline' }))
    expect(analyses()).toBeNull() // already while the new outline loads
    await screen.findByRole('list', { name: 'Outline' })
    expect(analyses()).toBeNull()
    expect(subject()).toBeNull()
    choose(MAIN)
    expect(screen.queryAllByTestId('reference')).toHaveLength(0)
    expect(actionButton('references').textContent).toBe(buttonLabel('references', MAIN))
    expect(seen.map((entry) => entry.url)).toEqual([OUTLINE_PATH, REFERENCES_PATH, OUTLINE_PATH])
  })

  it('clears the selection when the outline request fails; the next outline does not bring it back', async () => {
    let calls = 0
    renderOutline({
      outlineRoute: () => (++calls === 2 ? { status: 409, data: { detail: CHANGED } } : { status: 200, data: outline() }),
    })
    await showOutline()
    choose(MAIN)
    fireEvent.click(screen.getByRole('button', { name: 'Refresh outline' }))
    expect((await screen.findByRole('alert')).textContent).toBe(CHANGED)
    expect(analyses()).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Show outline' }))
    await screen.findByRole('list', { name: 'Outline' })
    expect(analyses()).toBeNull()
  })

  it.each([
    ['syntax_error', STATUS_MESSAGES.syntax_error],
    ['too_large', STATUS_MESSAGES.too_large],
    ['binary', STATUS_MESSAGES.binary],
    ['unsupported_language', STATUS_MESSAGES.unsupported_language],
    ['mystery', UNKNOWN_STATUS],
  ])('hides the analyses for a %s outline; the next parsed outline does not bring them back', async (status, message) => {
    let calls = 0
    renderOutline({
      outlineRoute: () => ({
        status: 200,
        data: ++calls === 2 ? outline({ status: status as CodeOutline['status'], symbols: [] }) : outline(),
      }),
    })
    await showOutline()
    choose(WIDGET)
    fireEvent.click(screen.getByRole('button', { name: 'Refresh outline' }))
    await screen.findByText(message)
    expect(analyses()).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Refresh outline' }))
    await screen.findByRole('list', { name: 'Outline' })
    expect(analyses()).toBeNull()
  })
})

// --- I. Security ------------------------------------------------------------------------------------

describe('Outline symbol analyses: security', () => {
  it('renders hostile names as literal text and never executes them', async () => {
    const evalSpy = vi.spyOn(globalThis, 'eval')
    const hostile: OutlineSymbol[] = [
      { kind: 'function', async: false, name: HTML, line: 1, end_line: 2, depth: 0 },
      { kind: 'class', async: false, name: '<b>bold</b>', line: 3, end_line: 4, depth: 0 },
      { kind: 'method', async: false, name: 'globalThis.__pwned = true', line: 5, end_line: 6, depth: 1 },
    ]
    renderOutline({ outlineRoute: { status: 200, data: outline({ symbols: hostile }) } })
    await showOutline()
    for (const symbol of hostile) {
      choose(symbol)
      expect(subject()).toBe(`Analyses for ${symbol.kind} ${symbol.name} (line ${symbol.line})`)
      expect(actionButton('references').textContent).toBe(buttonLabel('references', symbol))
    }
    choose(hostile[0])
    expect(screen.getByTestId('symbol-analyses-subject').innerHTML).toBe(
      'Analyses for function &lt;img src=x onerror=alert(1)&gt; (line 1)',
    )
    expect(document.querySelector('img, b, script, iframe')).toBeNull()
    expect(document.querySelector('[src], [onerror]')).toBeNull()
    expect((globalThis as { __pwned?: unknown }).__pwned).toBeUndefined()
    expect(evalSpy).not.toHaveBeenCalled()
  })

  it('uses no browser storage, cookies or direct fetch: only the app API client', async () => {
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
      const { requests } = renderOutline()
      await showOutline()
      choose(MAIN)
      await analyze('references', MAIN)
      choose(WIDGET)
      await analyze('types', WIDGET)
      expect(new Set(getItem.mock.calls.map((call) => call[0]))).toEqual(new Set(['ai_se_access_token']))
      for (const spy of writes) {
        expect(spy).not.toHaveBeenCalled()
      }
      expect([localStorage.length, sessionStorage.length]).toEqual(stored)
      expect(cookieGet).not.toHaveBeenCalled()
      expect(cookieSet).not.toHaveBeenCalled()
      expect(fetchSpy).not.toHaveBeenCalled()
      expect(requests.map((r) => `${r.method} ${r.url}`)).toEqual([
        `GET ${OUTLINE_PATH}`,
        `GET ${REFERENCES_PATH}`,
        `GET ${TYPES_PATH}`,
      ])
      expect(requests.every((r) => r.baseURL === '/api/v1')).toBe(true)
    } finally {
      globalThis.fetch = realFetch
      delete (document as { cookie?: unknown }).cookie
    }
  })

  it('never renders unknown response fields', async () => {
    const extra = { source: 'FAKE-SOURCE-MARKER', docstring: 'FAKE-DOC-MARKER', sha256: 'FAKE-SHA-MARKER', id: 'FAKE-ID-MARKER' }
    renderOutline({
      outlineRoute: { status: 200, data: { ...outline(), ...extra, symbols: SYMBOLS.map((s) => ({ ...s, ...extra })) } },
      routes: { [REFERENCES_PATH]: { status: 200, data: { ...referencesResult(MAIN), ...extra } } },
    })
    await showOutline()
    choose(MAIN)
    await analyze('references', MAIN)
    const text = document.body.textContent ?? ''
    for (const marker of Object.values(extra)) {
      expect(text).not.toContain(marker)
    }
    for (const button of screen.getAllByRole('button')) {
      expect(button.getAttribute('aria-label') ?? '').not.toMatch(/FAKE/)
    }
  })

  it('renders repeated symbols and repeated selections without React key warnings', async () => {
    const consoleError = vi.spyOn(console, 'error')
    renderOutline({ outlineRoute: { status: 200, data: outline({ symbols: [MAIN, MAIN, WIDGET] }) } })
    await showOutline()
    const [first, second] = screen.getAllByRole('button', { name: symbolLabel(MAIN) })
    fireEvent.click(first)
    await analyze('references', MAIN)
    fireEvent.click(second)
    expect(screen.queryAllByTestId('reference')).toHaveLength(0) // the twin is a context of its own
    fireEvent.click(second)
    choose(WIDGET)
    expect(subject()).toBe('Analyses for class Widget (line 3)')
    const messages = consoleError.mock.calls.map((call) => call.map(String).join(' '))
    expect(messages.filter((m) => /same key|unique "key"/.test(m))).toEqual([])
  })
})

// --- FileViewer integration -------------------------------------------------------------------------

const MODELS = [
  'import os',
  '',
  'class Widget:',
  '    def render(self):',
  '        return 1',
  '',
  '    async def fetch(self):',
  '        return 2',
  '',
  '    class Meta:',
  '        pass',
  '',
  '',
  'def main():',
  '    def inner():',
  '        return Widget()',
  '    return inner()',
  '',
].join('\n')
const OTHER = 'print(1)\n'

function content(path: string, text: string): MockResponse {
  const data: FileContent = { path, language: 'Python', size_bytes: text.length, content: text, reason: null }
  return { status: 200, data }
}

function renderViewer(path: string) {
  const served = serve({
    ...defaultRoutes(),
    [CONTENT_PATH]: (config) => {
      const requested = (config.params as { path: string }).path
      return content(requested, requested === CANONICAL ? MODELS : OTHER)
    },
  })
  const client = createTestQueryClient()
  const onSelectLine = vi.fn<Navigate>()
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
  return { ...served, onSelectLine, open: (next: string) => view.rerender(ui(next)) }
}

const codeText = () => document.querySelector('pre code')?.textContent

describe('Outline symbol analyses in the file viewer', () => {
  it('opens the analyses inside the File outline panel and navigates with the viewer path', async () => {
    const { onSelectLine } = renderViewer(CANONICAL)
    await waitFor(() => expect(codeText()).toBe(MODELS))
    fireEvent.click(within(outlineRegion()).getByRole('button', { name: 'Show outline' }))
    await screen.findByRole('list', { name: 'Outline' })
    choose(MAIN)
    expect(onSelectLine.mock.calls).toEqual([[CANONICAL, 14]])
    const section = analyses() as HTMLElement
    expect(outlineRegion().contains(section)).toBe(true)
    expect(screen.getByRole('region', { name: 'Go to definition' }).contains(section)).toBe(false)
    expect(panelNames()).toEqual(['Find references', 'Call hierarchy'])
  })

  it('never carries a selection or a result into another already-loaded file', async () => {
    const { open } = renderViewer('app/other.py')
    await waitFor(() => expect(codeText()).toBe(OTHER))
    open(CANONICAL)
    await waitFor(() => expect(codeText()).toBe(MODELS))
    fireEvent.click(within(outlineRegion()).getByRole('button', { name: 'Show outline' }))
    await screen.findByRole('list', { name: 'Outline' })
    choose(MAIN)
    await analyze('references', MAIN)
    open('app/other.py')
    expect(codeText()).toBe(OTHER) // shown at once: both files are already loaded
    expect(analyses()).toBeNull()
    expect(screen.queryAllByTestId('reference')).toHaveLength(0)
    open(CANONICAL)
    expect(codeText()).toBe(MODELS)
    expect(analyses()).toBeNull()
    expect(screen.queryByRole('list', { name: 'Outline' })).toBeNull()
    expect(within(outlineRegion()).getByRole('button', { name: 'Show outline' })).toBeTruthy()
  })
})
