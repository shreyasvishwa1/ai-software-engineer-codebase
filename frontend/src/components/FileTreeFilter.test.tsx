import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { RepositoryPage } from '../pages/RepositoryPage'
import { mockApi, PROJECT, ZIP_REPOSITORY } from '../test/mockApi'
import { createTestQueryClient } from '../test/render'
import type { TreeDirectory, TreeFile, TreeNode } from '../types/api'
import { capNotice, FILTER_LABEL, FileTree, matchCount, MAX_MATCHES, NO_MATCHES } from './FileTree'

const P = PROJECT.id
const R = ZIP_REPOSITORY.id
const BASE = `/projects/${P}/repositories/${R}`

// The approved wording, written here independently of the component.
const LABEL = 'Filter files by path'
const NONE = 'No files match the filter.'
const HTML = '<img src=x onerror=alert(1)>.py'

function file(path: string, language: string | null = 'Python', size_bytes = 22): TreeFile {
  const name = path.slice(path.lastIndexOf('/') + 1)
  const dot = name.lastIndexOf('.')
  return { type: 'file', name, path, extension: dot > 0 ? name.slice(dot + 1) : null, language, size_bytes }
}

function dir(path: string, children: TreeNode[]): TreeDirectory {
  return { type: 'directory', name: path.slice(path.lastIndexOf('/') + 1), path, children }
}

// Server order, deliberately not alphabetical; src/utils starts collapsed.
const TREE = dir('', [
  dir('src', [
    dir('src/utils', [file('src/utils/helper.py'), file('src/utils/Zeta.PY', 'Python', 2048)]),
    file('src/app.py', 'Python', 40),
  ]),
  file('README.md', 'Markdown', 9),
  dir('docs', [file('docs/guide.md', 'Markdown', 12)]),
])
const EMPTY = dir('', [])

/** A flat tree of `count` Python files, pkg/m000.py, pkg/m001.py, … in that order. */
function manyFiles(count: number): TreeDirectory {
  return dir('', [dir('pkg', Array.from({ length: count }, (_, i) => file(`pkg/m${String(i).padStart(3, '0')}.py`)))])
}

const nav = () => screen.getByRole('navigation', { name: 'File tree' })
const filterBox = () => screen.getByRole('searchbox', { name: LABEL }) as HTMLInputElement
const typeFilter = (value: string) => fireEvent.change(filterBox(), { target: { value } })
const treeList = () => nav().querySelector(':scope > ul') as HTMLUListElement
const matchList = () => screen.queryByRole('list', { name: 'Matching files' })
const matchRows = () => {
  const list = matchList()
  return list ? within(list).getAllByRole('listitem') : []
}
const matchPaths = () => matchRows().map((row) => row.querySelector('span')?.textContent)
const countText = () => screen.getByTestId('file-filter-count').textContent
const clearButton = () => screen.queryByRole('button', { name: 'Clear the file filter' })
const buttonNames = () => screen.getAllByRole('button').map((b) => b.getAttribute('aria-label') ?? b.textContent)
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

afterEach(() => {
  vi.restoreAllMocks()
})

// --- A. Default state ---------------------------------------------------------------------------

describe('File filter default state', () => {
  it('shows an empty filter above the tree inside the navigation, adding no button', () => {
    render(<FileTree tree={TREE} />)
    expect([filterBox().value, filterBox().placeholder]).toEqual(['', LABEL])
    expect(nav().contains(filterBox())).toBe(true)
    expect(filterBox().compareDocumentPosition(treeList()) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(clearButton()).toBeNull()
    expect(countText()).toBe('')
    // Mounted from the start, so every later count is announced.
    expect(screen.getByTestId('file-filter-count').getAttribute('aria-live')).toBe('polite')
    expect(matchList()).toBeNull()
    expect(treeList().hidden).toBe(false)
    expect(screen.getAllByRole('button').map((b) => b.textContent)).toEqual(['▾src', '▸utils', '▾docs'])
  })

  it('shows no filter for an empty tree, and the filter once files exist', () => {
    const { rerender } = render(<FileTree tree={EMPTY} />)
    expect(screen.getByText('No files have been ingested yet.')).toBeTruthy()
    expect(screen.queryByRole('searchbox')).toBeNull()
    rerender(<FileTree tree={TREE} />) // the same component: hooks run in the same order
    typeFilter('app')
    expect(matchPaths()).toEqual(['src/app.py'])
    rerender(<FileTree tree={EMPTY} />)
    expect(screen.getByText('No files have been ingested yet.')).toBeTruthy()
    expect(screen.queryByRole('searchbox')).toBeNull()
    // Still the same component: its state survives (hooks never follow the empty-tree return).
    rerender(<FileTree tree={TREE} />)
    expect(filterBox().value).toBe('app')
    expect(matchPaths()).toEqual(['src/app.py'])
  })

  it('has the approved wording', () => {
    expect([FILTER_LABEL, NO_MATCHES, MAX_MATCHES]).toEqual([LABEL, NONE, 100])
    expect([matchCount(0), matchCount(1), matchCount(2), matchCount(150)]).toEqual([
      NONE,
      '1 matching file',
      '2 matching files',
      '150 matching files',
    ])
    expect(capNotice(150)).toBe('Showing the first 100 of 150 matching files.')
  })
})

// --- B. Matching --------------------------------------------------------------------------------

describe('File filter matching', () => {
  it('lists files whose path contains the text, ignoring case, in tree order, and hides the tree', () => {
    render(<FileTree tree={TREE} />)
    typeFilter('py')
    // Zeta.PY matches too, and the files of the collapsed src/utils are found.
    expect(matchPaths()).toEqual(['src/utils/helper.py', 'src/utils/Zeta.PY', 'src/app.py'])
    expect(countText()).toBe('3 matching files')
    expect(treeList().hidden).toBe(true)
    expect(screen.queryByRole('button', { name: 'src' })).toBeNull() // the tree is out of reach
    expect(buttonNames()).toEqual(['Clear the file filter'])
    expect(screen.getByRole('navigation', { name: 'File tree' })).toBe(nav()) // still the landmark
  })

  it('shows the full path, language and size of every match', () => {
    render(<FileTree tree={TREE} />)
    typeFilter('a')
    expect(matchRows().map((row) => row.textContent)).toEqual([
      'src/utils/Zeta.PYPython2.0 KB',
      'src/app.pyPython40 B',
      'README.mdMarkdown9 B',
    ])
  })

  it('matches uppercase text and reports a single match', () => {
    render(<FileTree tree={TREE} />)
    typeFilter('HELPER')
    expect(matchPaths()).toEqual(['src/utils/helper.py'])
    expect(countText()).toBe('1 matching file')
  })

  it.each([
    ['utils/', ['src/utils/helper.py', 'src/utils/Zeta.PY']],
    ['RC/UT', ['src/utils/helper.py', 'src/utils/Zeta.PY']],
    ['docs', ['docs/guide.md']],
    ['src', ['src/utils/helper.py', 'src/utils/Zeta.PY', 'src/app.py']],
  ])('matches directory names in the path too (%s)', (text, paths) => {
    render(<FileTree tree={TREE} />)
    typeFilter(text)
    expect(matchPaths()).toEqual(paths)
  })

  it('ignores surrounding spaces', () => {
    render(<FileTree tree={TREE} />)
    typeFilter('  app.py  ')
    expect(matchPaths()).toEqual(['src/app.py'])
    expect(filterBox().value).toBe('  app.py  ') // the box keeps exactly what was typed
  })

  it('treats a blank filter as no filter, while still offering Clear', () => {
    render(<FileTree tree={TREE} />)
    typeFilter('   ')
    expect(treeList().hidden).toBe(false)
    expect(countText()).toBe('')
    expect(matchList()).toBeNull()
    expect(clearButton()).not.toBeNull()
  })

  it('reports no match without a list, keeping the tree hidden', () => {
    render(<FileTree tree={TREE} />)
    typeFilter('nothing-here')
    expect(countText()).toBe(NONE)
    expect(matchList()).toBeNull()
    expect(treeList().hidden).toBe(true)
  })

  it('matches literal text, never a pattern', () => {
    render(
      <FileTree tree={dir('', [file('a.b/x.py'), file('axb/y.py'), file('(weird)[1].txt', null), file('plain.txt', null)])} />,
    )
    typeFilter('a.b')
    expect(matchPaths()).toEqual(['a.b/x.py'])
    typeFilter('(weird)[')
    expect(matchPaths()).toEqual(['(weird)[1].txt'])
    typeFilter('.*')
    expect(countText()).toBe(NONE)
  })
})

// --- C. Selection -------------------------------------------------------------------------------

describe('File filter selection', () => {
  it('opens a match with its full path, never on typing', () => {
    const onSelectFile = vi.fn()
    render(<FileTree tree={TREE} onSelectFile={onSelectFile} />)
    typeFilter('guide')
    expect(onSelectFile).not.toHaveBeenCalled()
    fireEvent.click(within(matchList() as HTMLElement).getByRole('button', { name: /docs\/guide\.md/ }))
    expect(onSelectFile.mock.calls).toEqual([['docs/guide.md']])
  })

  it('keeps the filter and its matches after a selection', () => {
    render(<FileTree tree={TREE} onSelectFile={vi.fn()} />)
    typeFilter('py')
    fireEvent.click(within(matchList() as HTMLElement).getAllByRole('button')[1])
    expect(filterBox().value).toBe('py')
    expect(matchPaths()).toEqual(['src/utils/helper.py', 'src/utils/Zeta.PY', 'src/app.py'])
  })

  it('marks the selected file among the matches', () => {
    const { rerender } = render(<FileTree tree={TREE} onSelectFile={vi.fn()} selectedPath="src/app.py" />)
    typeFilter('py')
    const current = () =>
      within(matchList() as HTMLElement)
        .getAllByRole('button')
        .map((b) => b.getAttribute('aria-current'))
    expect(current()).toEqual([null, null, 'true'])
    rerender(<FileTree tree={TREE} onSelectFile={vi.fn()} selectedPath="src/utils/Zeta.PY" />)
    expect(current()).toEqual([null, 'true', null])
  })

  it('keeps matches as plain text without onSelectFile', () => {
    render(<FileTree tree={TREE} />)
    typeFilter('md')
    expect(matchPaths()).toEqual(['README.md', 'docs/guide.md'])
    expect(within(matchList() as HTMLElement).queryAllByRole('button')).toHaveLength(0)
  })
})

// --- D. Clearing and the tree's state -----------------------------------------------------------

describe('File filter clearing', () => {
  it('clears with the Clear button and shows the tree again', () => {
    render(<FileTree tree={TREE} />)
    typeFilter('py')
    fireEvent.click(clearButton() as HTMLElement)
    expect(filterBox().value).toBe('')
    expect([countText(), matchList(), clearButton()]).toEqual(['', null, null])
    expect(treeList().hidden).toBe(false)
  })

  it('clears with Escape and with no other key', () => {
    render(<FileTree tree={TREE} />)
    typeFilter('py')
    fireEvent.keyDown(filterBox(), { key: 'Enter' })
    fireEvent.keyDown(filterBox(), { key: 'a' })
    expect(matchPaths()).toHaveLength(3)
    fireEvent.keyDown(filterBox(), { key: 'Escape' })
    expect(filterBox().value).toBe('')
    expect(matchList()).toBeNull()
    expect(treeList().hidden).toBe(false)
  })

  it('keeps the expanded and collapsed directories while filtering', () => {
    render(<FileTree tree={TREE} />)
    fireEvent.click(screen.getByRole('button', { name: 'utils' })) // expand a nested directory
    fireEvent.click(screen.getByRole('button', { name: 'docs' })) // collapse a top-level one
    typeFilter('py')
    fireEvent.click(clearButton() as HTMLElement)
    expect(screen.getByRole('button', { name: 'utils' }).getAttribute('aria-expanded')).toBe('true')
    expect(screen.getByRole('button', { name: 'docs' }).getAttribute('aria-expanded')).toBe('false')
    expect(screen.getByText('helper.py')).toBeTruthy()
    expect(screen.queryByText('guide.md')).toBeNull()
  })
})

// --- E. Limits ----------------------------------------------------------------------------------

describe('File filter limits', () => {
  it('lists at most 100 matches, with the exact count and a notice', () => {
    const consoleError = vi.spyOn(console, 'error')
    render(<FileTree tree={manyFiles(150)} onSelectFile={vi.fn()} />)
    typeFilter('pkg/')
    const paths = matchPaths()
    expect(paths).toHaveLength(100)
    expect([paths[0], paths[99]]).toEqual(['pkg/m000.py', 'pkg/m099.py']) // the first 100, in tree order
    expect(countText()).toBe('150 matching files')
    expect(nav().textContent).toContain('Showing the first 100 of 150 matching files.')
    expect(consoleError).not.toHaveBeenCalled() // every row has its own key
  })

  it.each([
    [100, false],
    [101, true],
  ])('shows the notice only above 100 matches (%i)', (count, notice) => {
    render(<FileTree tree={manyFiles(count)} />)
    typeFilter('m')
    expect(matchPaths()).toHaveLength(100)
    expect(countText()).toBe(`${count} matching files`)
    expect(nav().textContent?.includes(`Showing the first 100 of ${count} matching files.`)).toBe(notice)
  })

  it('updates the matches when the tree changes, keeping the filter', () => {
    const { rerender } = render(<FileTree tree={TREE} />)
    typeFilter('app')
    expect(matchPaths()).toEqual(['src/app.py'])
    rerender(<FileTree tree={dir('', [file('lib/app_main.py'), ...TREE.children])} />)
    expect(filterBox().value).toBe('app')
    expect(matchPaths()).toEqual(['lib/app_main.py', 'src/app.py'])
  })
})

// --- F. Security --------------------------------------------------------------------------------

describe('File filter security', () => {
  it.each([
    ['as buttons', true],
    ['as plain text', false],
  ])('renders markup-like paths as literal text %s', (_name, navigable) => {
    const { container } = render(
      <FileTree tree={dir('', [file(HTML), file('safe.py')])} onSelectFile={navigable ? vi.fn() : undefined} />,
    )
    typeFilter('img')
    expect(matchPaths()).toEqual([HTML])
    expect(container.querySelector('img, [onerror]')).toBeNull()
    expect((matchList() as HTMLElement).innerHTML).toContain('&lt;img src=x onerror=alert(1)&gt;.py')
  })

  it('requests nothing and uses no storage, cookies or fetch', () => {
    const requests = mockApi({})
    const storage = [
      vi.spyOn(Storage.prototype, 'getItem'),
      vi.spyOn(Storage.prototype, 'setItem'),
      vi.spyOn(Storage.prototype, 'removeItem'),
      vi.spyOn(Storage.prototype, 'clear'),
    ]
    const cookieGet = vi.fn(() => '')
    const cookieSet = vi.fn()
    Object.defineProperty(document, 'cookie', { configurable: true, get: cookieGet, set: cookieSet })
    const realFetch = globalThis.fetch
    const fetchSpy = vi.fn()
    globalThis.fetch = fetchSpy as unknown as typeof fetch
    try {
      render(<FileTree tree={TREE} onSelectFile={vi.fn()} />)
      typeFilter('py')
      fireEvent.click(within(matchList() as HTMLElement).getAllByRole('button')[0])
      fireEvent.keyDown(filterBox(), { key: 'Escape' })
      typeFilter('md')
      fireEvent.click(clearButton() as HTMLElement)
      for (const spy of storage) {
        expect(spy).not.toHaveBeenCalled()
      }
      expect(cookieGet).not.toHaveBeenCalled()
      expect(cookieSet).not.toHaveBeenCalled()
      expect(fetchSpy).not.toHaveBeenCalled()
      expect(requests).toHaveLength(0)
    } finally {
      globalThis.fetch = realFetch
      delete (document as { cookie?: unknown }).cookie
    }
  })
})

// --- G. Labels ----------------------------------------------------------------------------------

describe('File filter labels', () => {
  it('keeps the default labels clear of the frozen test patterns', () => {
    const frozen = [
      /definition/i,
      /outline/i,
      /^Go to/,
      /^Open/,
      /^(Jump|Open) /,
      /^Find/,
      /^Search/,
      /^Cancel /,
      /^Loading/,
      /^Line /,
      /occurrences/,
      /importers|Retry/,
      /src/,
      /utils/,
      /app\.py/,
      /B\.md/,
      /README\.md/,
      /helper\.py/,
    ]
    for (const pattern of frozen) {
      expect(pattern.test(LABEL)).toBe(false)
    }
  })
})

// --- H. Repository page -------------------------------------------------------------------------

const PAGE_TREE = dir('', [
  dir('a', [file('a/app.py', 'Python', 40), dir('a/deep', [file('a/deep/c.py', 'Python', 3)])]),
  file('B.md', 'Markdown', 9),
])

const scrollSpy = vi.fn()

function stubScroll() {
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
    configurable: true,
    writable: true,
    value: scrollSpy,
  })
}

afterEach(() => {
  scrollSpy.mockReset()
  delete (HTMLElement.prototype as { scrollIntoView?: unknown }).scrollIntoView
})

function renderPage() {
  const contentPaths: string[] = []
  const requests = mockApi({
    [`GET ${BASE}`]: { status: 200, data: { ...ZIP_REPOSITORY, status: 'ready' } },
    [`GET ${BASE}/tree`]: {
      status: 200,
      data: { repository_id: R, status: 'ready', file_count: 3, total_bytes: 52, tree: PAGE_TREE },
    },
    [`GET ${BASE}/languages`]: {
      status: 200,
      data: { repository_id: R, status: 'ready', total_files: 3, total_bytes: 52, languages: [] },
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
        summary: { files_scanned: 3, binary: 0, too_large: 0, unavailable: 0, findings: 0 },
        truncated: false,
      },
    },
    [`GET ${BASE}/files/content`]: (config) => {
      const path = (config.params as { path: string }).path
      contentPaths.push(path)
      return {
        status: 200,
        data: { path, language: null, size_bytes: 3, content: `content of ${path}`, reason: null },
      }
    },
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

const viewerCode = () => screen.getByRole('region', { name: 'File' }).querySelector('pre code')?.textContent

describe('File filter on the repository page', () => {
  it('sits in the Files card and filters without any request', async () => {
    const { requests } = renderPage()
    await screen.findByRole('navigation', { name: 'File tree' })
    await screen.findByRole('table', { name: 'Secret scan summary' }) // page settled
    await sleep(50)
    const before = requests.length
    const files = within(screen.getByRole('region', { name: 'Files' }))
    expect(files.getByRole('searchbox', { name: LABEL })).toBe(filterBox())
    expect(screen.getAllByRole('searchbox')).toHaveLength(1)
    typeFilter('c.py')
    expect(matchPaths()).toEqual(['a/deep/c.py']) // inside a collapsed directory
    await sleep(50)
    expect(requests).toHaveLength(before)
  })

  it('opens a match in the viewer like a tree selection: marked, without scrolling', async () => {
    stubScroll()
    const { contentPaths } = renderPage()
    await screen.findByRole('navigation', { name: 'File tree' })
    typeFilter('deep/')
    fireEvent.click(within(matchList() as HTMLElement).getByRole('button', { name: /a\/deep\/c\.py/ }))
    await waitFor(() => expect(viewerCode()).toBe('content of a/deep/c.py'))
    expect(contentPaths).toEqual(['a/deep/c.py'])
    expect(
      within(matchList() as HTMLElement)
        .getByRole('button', { name: /a\/deep\/c\.py/ })
        .getAttribute('aria-current'),
    ).toBe('true')
    expect(scrollSpy).not.toHaveBeenCalled()
    expect(filterBox().value).toBe('deep/')
  })
})
