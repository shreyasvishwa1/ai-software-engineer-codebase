import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { InternalAxiosRequestConfig } from 'axios'
import type { ReactElement } from 'react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import { RepositoryPage } from '../pages/RepositoryPage'
import { mockApi, type MockResponse, PROJECT, ZIP_REPOSITORY } from '../test/mockApi'
import { createTestQueryClient } from '../test/render'
import type { FileContent, TreeDirectory } from '../types/api'
import { FileTree } from './FileTree'
import { FileViewer } from './FileViewer'

const P = PROJECT.id
const R = ZIP_REPOSITORY.id
const CONTENT_KEY = `GET /projects/${P}/repositories/${R}/files/content`

function file(path: string, extra: Partial<FileContent> = {}): FileContent {
  return { path, language: 'Python', size_bytes: 2048, content: 'x', reason: null, ...extra }
}

/** Serves file content by the requested params.path; records every requested path. */
function serveFiles(files: Record<string, FileContent | MockResponse>) {
  const paths: string[] = []
  const requests = mockApi({
    [CONTENT_KEY]: (config: InternalAxiosRequestConfig) => {
      const path = (config.params as { path: string }).path
      paths.push(path)
      const entry = files[path]
      if (!entry) {
        return { status: 404, data: { detail: 'File not found' } }
      }
      return 'status' in entry ? entry : { status: 200, data: entry }
    },
  })
  return { paths, requests }
}

function renderViewer(selectedPath: string | null) {
  return render(
    <QueryClientProvider client={createTestQueryClient()}>
      <FileViewer projectId={P} repositoryId={R} selectedPath={selectedPath} />
    </QueryClientProvider>,
  )
}

describe('FileViewer', () => {
  it('asks for a selection and makes no request without one', () => {
    const { requests } = serveFiles({})
    renderViewer(null)
    expect(screen.getByText('Select a file to view its content.')).toBeTruthy()
    expect(requests).toHaveLength(0)
  })

  it('shows a loading state, then the exact content with path, language and size', async () => {
    const content = '  indented\n\tdef main():\n\t\treturn "héllo — 世界 🚀"\n'
    serveFiles({ 'src/app.py': file('src/app.py', { content, size_bytes: 2048 }) })
    const { container } = renderViewer('src/app.py')

    expect(screen.getByText('Loading file…')).toBeTruthy()
    const code = await waitFor(() => {
      const element = container.querySelector('pre code')
      expect(element).toBeTruthy()
      return element as HTMLElement
    })
    expect(code.textContent).toBe(content) // whitespace, tabs and Unicode preserved
    const header = within(screen.getByTestId('file-header'))
    expect(header.getByText('src/app.py')).toBeTruthy()
    expect(header.getByText('Python')).toBeTruthy()
    expect(header.getByText('2.0 KB')).toBeTruthy()
  })

  it('shows Unknown for a file without a language', async () => {
    serveFiles({ LICENSE: file('LICENSE', { language: null, size_bytes: 10, content: 'MIT' }) })
    renderViewer('LICENSE')
    const header = within(await screen.findByTestId('file-header'))
    expect(header.getByText('Unknown')).toBeTruthy()
    expect(header.getByText('10 B')).toBeTruthy()
  })

  it.each([
    [{ content: '', size_bytes: 0 }, 'This file is empty.'],
    [{ content: null, reason: 'binary' as const }, 'Binary file — preview not available.'],
    [{ content: null, reason: 'too_large' as const }, 'File is too large to preview (limit 1 MiB).'],
  ])('shows the state for %o', async (extra, message) => {
    serveFiles({ 'a.bin': file('a.bin', extra) })
    const { container } = renderViewer('a.bin')
    expect(await screen.findByText(message)).toBeTruthy()
    expect(container.querySelector('pre')).toBeNull()
  })

  it('shows the server message for a missing file, without Retry', async () => {
    serveFiles({})
    renderViewer('gone.py')
    expect((await screen.findByRole('alert')).textContent).toBe('File not found')
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
  })

  it('shows the server message for unavailable content and retries on request', async () => {
    let calls = 0
    const paths: string[] = []
    mockApi({
      [CONTENT_KEY]: (config: InternalAxiosRequestConfig) => {
        paths.push((config.params as { path: string }).path)
        calls += 1
        return calls === 1
          ? { status: 409, data: { detail: 'File content is not available; try again later' } }
          : { status: 200, data: file('src/app.py', { content: 'print(1)\n' }) }
      },
    })
    const { container } = renderViewer('src/app.py')

    expect((await screen.findByRole('alert')).textContent).toBe(
      'File content is not available; try again later',
    )
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

    await waitFor(() => expect(container.querySelector('pre code')?.textContent).toBe('print(1)\n'))
    expect(paths).toEqual(['src/app.py', 'src/app.py'])
  })

  it.each([
    ['<script>alert("x")</script><img src=x onerror="alert(1)">', 'page.html'],
    ['document.body.innerHTML = "<img src=x onerror=alert(1)>"; eval("1")', 'app.js'],
  ])('renders repository content as text only: %s', async (content, path) => {
    serveFiles({ [path]: file(path, { content }) })
    const { container } = renderViewer(path)
    await waitFor(() => expect(container.querySelector('pre code')?.textContent).toBe(content))
    expect(container.querySelector('script, img, iframe')).toBeNull()
  })

  it.each([
    'docs/with space/read me.md',
    'src/hash#tag.py',
    'src/what?.py',
    'src/100%.txt',
    'dir/a b#c?d%e&f=g.txt',
  ])('requests the exact path %s as a query parameter', async (path) => {
    const { paths, requests } = serveFiles({ [path]: file(path, { content: 'ok' }) })
    renderViewer(path)
    await screen.findByText('ok')
    expect(paths).toEqual([path])
    expect(requests).toHaveLength(1)
    expect(requests[0].url).toBe(`/projects/${P}/repositories/${R}/files/content`) // not in the URL
  })
})

// --- FileTree selection ----------------------------------------------------------------

const TREE: TreeDirectory = {
  type: 'directory',
  name: '',
  path: '',
  children: [
    {
      type: 'directory',
      name: 'src',
      path: 'src',
      children: [
        {
          type: 'directory',
          name: 'utils',
          path: 'src/utils',
          children: [
            {
              type: 'file',
              name: 'helper.py',
              path: 'src/utils/helper.py',
              extension: 'py',
              language: 'Python',
              size_bytes: 22,
            },
          ],
        },
        {
          type: 'file',
          name: 'app.py',
          path: 'src/app.py',
          extension: 'py',
          language: 'Python',
          size_bytes: 15,
        },
      ],
    },
    {
      type: 'file',
      name: 'README.md',
      path: 'README.md',
      extension: 'md',
      language: 'Markdown',
      size_bytes: 9,
    },
  ],
}

describe('FileTree selection', () => {
  it('calls onSelectFile with the full path and marks the selected file', () => {
    const onSelectFile = vi.fn()
    const { rerender } = render(<FileTree tree={TREE} onSelectFile={onSelectFile} />)

    fireEvent.click(screen.getByRole('button', { name: /utils/ })) // folders still expand
    fireEvent.click(screen.getByRole('button', { name: /helper\.py/ }))
    expect(onSelectFile).toHaveBeenCalledWith('src/utils/helper.py')

    rerender(
      <FileTree tree={TREE} onSelectFile={onSelectFile} selectedPath="src/utils/helper.py" />,
    )
    expect(screen.getByRole('button', { name: /helper\.py/ }).getAttribute('aria-current')).toBe(
      'true',
    )
    expect(screen.getByRole('button', { name: /app\.py/ }).getAttribute('aria-current')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'src' }))
    expect(screen.queryByText('app.py')).toBeNull() // folders still collapse
  })

  it('keeps file rows non-interactive without onSelectFile', () => {
    render(<FileTree tree={TREE} />)
    expect(screen.getAllByRole('button').map((b) => b.textContent)).toEqual(['▾src', '▸utils'])
    const readme = screen.getByText('README.md').closest('li')
    expect(readme?.querySelector('button')).toBeNull()
    expect(readme?.textContent).toContain('Markdown')
  })
})

// --- Repository page integration --------------------------------------------------------

function renderPage(routes: Record<string, MockResponse | ((c: InternalAxiosRequestConfig) => MockResponse)>) {
  const requests = mockApi({
    [`GET /projects/${P}/repositories/${R}`]: {
      status: 200,
      data: { ...ZIP_REPOSITORY, status: 'ready' },
    },
    [`GET /projects/${P}/repositories/${R}/tree`]: {
      status: 200,
      data: { repository_id: R, status: 'ready', file_count: 3, total_bytes: 46, tree: TREE },
    },
    [`GET /projects/${P}/repositories/${R}/languages`]: {
      status: 200,
      data: { repository_id: R, status: 'ready', total_files: 3, total_bytes: 46, languages: [] },
    },
    [`GET /projects/${P}/repositories/${R}/jobs`]: { status: 200, data: [] },
    ...routes,
  })
  const ui: ReactElement = (
    <QueryClientProvider client={createTestQueryClient()}>
      <MemoryRouter initialEntries={[`/projects/${P}/repositories/${R}`]}>
        <Routes>
          <Route path="/projects/:projectId/repositories/:repositoryId" element={<RepositoryPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  )
  render(ui)
  return requests
}

describe('RepositoryPage file viewer', () => {
  it('shows the placeholder, then the clicked file, then another file', async () => {
    const contents: Record<string, string> = {
      'src/app.py': "print('app')\n",
      'README.md': '# Readme\n',
    }
    const requests = renderPage({
      [CONTENT_KEY]: (config) => {
        const path = (config.params as { path: string }).path
        return { status: 200, data: file(path, { content: contents[path] }) }
      },
    })
    const viewer = within(await screen.findByRole('region', { name: 'File' }))
    expect(viewer.getByText('Select a file to view its content.')).toBeTruthy()
    expect(requests.some((r) => r.url.endsWith('/files/content'))).toBe(false)

    fireEvent.click(await screen.findByRole('button', { name: /app\.py/ }))
    await waitFor(() =>
      expect(screen.getByRole('region', { name: 'File' }).querySelector('pre code')?.textContent).toBe(
        "print('app')\n",
      ),
    )
    expect(screen.getByRole('button', { name: /app\.py/ }).getAttribute('aria-current')).toBe('true')

    fireEvent.click(screen.getByRole('button', { name: /README\.md/ }))
    await waitFor(() =>
      expect(screen.getByRole('region', { name: 'File' }).querySelector('pre code')?.textContent).toBe(
        '# Readme\n',
      ),
    )
    expect(screen.getByRole('region', { name: 'File' }).textContent).not.toContain("print('app')")
    // The rest of the page is unchanged.
    expect(screen.getByRole('heading', { name: 'backend' })).toBeTruthy()
    expect(screen.getByRole('region', { name: 'Languages' })).toBeTruthy()
  })
})
