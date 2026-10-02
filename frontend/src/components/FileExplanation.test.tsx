import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { AxiosAdapter, InternalAxiosRequestConfig } from 'axios'
import type { ReactElement } from 'react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import { RepositoryPage } from '../pages/RepositoryPage'
import { apiClient } from '../services/client'
import {
  mockApi,
  type MockResponse,
  PROJECT,
  type RecordedRequest,
  ZIP_REPOSITORY,
} from '../test/mockApi'
import { createTestQueryClient } from '../test/render'
import type { FileContent, FileExplanation, TreeDirectory } from '../types/api'
import { PRIVACY_NOTICE } from './FileExplanation'
import { FileViewer } from './FileViewer'

const P = PROJECT.id
const R = ZIP_REPOSITORY.id
const BASE = `/projects/${P}/repositories/${R}`
const CONTENT_KEY = `GET ${BASE}/files/content`
const EXPLAIN_KEY = `POST ${BASE}/files/explain`
const BUTTON = { name: 'Explain this file' }

type Route = MockResponse | ((config: InternalAxiosRequestConfig) => MockResponse)

function content(path: string, extra: Partial<FileContent> = {}): FileContent {
  return { path, language: 'Python', size_bytes: 12, content: 'print(1)\n', reason: null, ...extra }
}

function explanation(path: string, extra: Partial<FileExplanation> = {}): FileExplanation {
  return {
    path,
    explanation: 'This file prints 1.',
    model: 'claude-opus-5-5',
    input_tokens: 120,
    output_tokens: 30,
    ...extra,
  }
}

/** Serves any path's content and records explain requests (params included). */
function serve(routes: Record<string, Route> = {}) {
  const explainParams: unknown[] = []
  const requests = mockApi({
    [CONTENT_KEY]: (config) => ({
      status: 200,
      data: content((config.params as { path: string }).path),
    }),
    [EXPLAIN_KEY]: (config) => {
      explainParams.push(config.params)
      const path = (config.params as { path: string }).path
      return { status: 200, data: explanation(path) }
    },
    ...routes,
  })
  return { requests, explainParams }
}

function renderViewer(path: string | null): ReturnType<typeof render> {
  return render(withClient(<FileViewer projectId={P} repositoryId={R} selectedPath={path} />))
}

let sharedClient = createTestQueryClient()
function withClient(ui: ReactElement): ReactElement {
  return <QueryClientProvider client={sharedClient}>{ui}</QueryClientProvider>
}

const explainPosts = (requests: RecordedRequest[]) =>
  requests.filter((r) => r.method === 'POST')

describe('When the explanation UI is shown', () => {
  it('shows the button and privacy notice for text, without requesting anything', async () => {
    sharedClient = createTestQueryClient()
    const { requests } = serve()
    renderViewer('src/app.py')
    const button = await screen.findByRole('button', BUTTON)
    expect(button.textContent).toBe('Explain this file')
    expect(screen.getByText(PRIVACY_NOTICE)).toBeTruthy()
    expect(PRIVACY_NOTICE).toBe("Sends this file's content to Anthropic to generate an explanation.")
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(explainPosts(requests)).toHaveLength(0) // no automatic request
  })

  it.each([
    ['binary', { content: null, reason: 'binary' as const }],
    ['too large', { content: null, reason: 'too_large' as const }],
    ['empty', { content: '', size_bytes: 0 }],
  ])('is hidden for %s files', async (_label, extra) => {
    sharedClient = createTestQueryClient()
    serve({ [CONTENT_KEY]: { status: 200, data: content('a.bin', extra) } })
    renderViewer('a.bin')
    await screen.findByTestId('file-header')
    expect(screen.queryByRole('button', BUTTON)).toBeNull()
    expect(screen.queryByText(PRIVACY_NOTICE)).toBeNull()
  })

  it('is hidden while the file is loading', () => {
    sharedClient = createTestQueryClient()
    serve()
    renderViewer('src/app.py')
    expect(screen.getByText('Loading file…')).toBeTruthy()
    expect(screen.queryByRole('button', BUTTON)).toBeNull()
  })

  it.each([
    [404, 'File not found'],
    [409, 'File content is not available; try again later'],
  ])('is hidden when the viewer shows a %i error', async (status, detail) => {
    sharedClient = createTestQueryClient()
    serve({ [CONTENT_KEY]: { status, data: { detail } } })
    renderViewer('gone.py')
    expect((await screen.findByRole('alert')).textContent).toBe(detail)
    expect(screen.queryByRole('button', BUTTON)).toBeNull()
  })
})

describe('Explaining', () => {
  it.each(['src/app.py', 'docs/with space/read me.md', 'src/hash#tag.py', 'src/what?.py', 'src/100%.txt'])(
    'sends exactly one POST with the exact path %s as a parameter',
    async (path) => {
      sharedClient = createTestQueryClient()
      const { requests, explainParams } = serve()
      renderViewer(path)
      fireEvent.click(await screen.findByRole('button', BUTTON))
      await screen.findByTestId('explanation-text')
      const posts = explainPosts(requests)
      expect(posts).toHaveLength(1)
      expect(posts[0].url).toBe(`${BASE}/files/explain`) // path never embedded in the URL
      expect(posts[0].data).toBeUndefined() // no request body
      expect(explainParams).toEqual([{ path }])
    },
  )

  it('shows a pending state with only the explain button disabled', async () => {
    sharedClient = createTestQueryClient()
    serve()
    renderViewer('src/app.py')
    const button = (await screen.findByRole('button', BUTTON)) as HTMLButtonElement
    const mocked = apiClient.defaults.adapter as AxiosAdapter
    let release: () => void = () => undefined
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    apiClient.defaults.adapter = async (config) => {
      if (config.method?.toUpperCase() === 'POST') {
        await gate
      }
      return mocked(config)
    }
    fireEvent.click(button)
    expect(await screen.findByText('Generating explanation…')).toBeTruthy()
    expect(button.disabled).toBe(true)
    release()
    await screen.findByTestId('explanation-text')
    expect(button.disabled).toBe(false)
    expect(screen.queryByText('Generating explanation…')).toBeNull()
  })

  it('shows the explanation with whitespace preserved, plus model and tokens', async () => {
    sharedClient = createTestQueryClient()
    const text = 'Purpose:\n  Prints a value.\n\n\tStructure: one call.'
    serve({
      [EXPLAIN_KEY]: { status: 200, data: explanation('src/app.py', { explanation: text, input_tokens: 321, output_tokens: 54 }) },
    })
    renderViewer('src/app.py')
    fireEvent.click(await screen.findByRole('button', BUTTON))
    const block = await screen.findByTestId('explanation-text')
    expect(block.textContent).toBe(text)
    expect(block.className).toContain('whitespace-pre-wrap')
    expect(
      screen.getByText('Generated by claude-opus-5-5 · 321 input / 54 output tokens'),
    ).toBeTruthy()
  })

  it('handles missing token counts', async () => {
    sharedClient = createTestQueryClient()
    serve({
      [EXPLAIN_KEY]: { status: 200, data: explanation('src/app.py', { input_tokens: null, output_tokens: null }) },
    })
    renderViewer('src/app.py')
    fireEvent.click(await screen.findByRole('button', BUTTON))
    expect(await screen.findByText('Generated by claude-opus-5-5 · — input / — output tokens')).toBeTruthy()
  })

  it('renders HTML-looking model output as text only', async () => {
    sharedClient = createTestQueryClient()
    const hostile = '<script>alert(1)</script><img src=x onerror=alert(2)> [link](https://example.com)'
    serve({ [EXPLAIN_KEY]: { status: 200, data: explanation('src/app.py', { explanation: hostile }) } })
    const { container } = renderViewer('src/app.py')
    fireEvent.click(await screen.findByRole('button', BUTTON))
    expect((await screen.findByTestId('explanation-text')).textContent).toBe(hostile)
    expect(container.querySelector('script, img, a, iframe')).toBeNull()
  })

  it('runs again on every click (no caching)', async () => {
    sharedClient = createTestQueryClient()
    const { explainParams } = serve()
    renderViewer('src/app.py')
    fireEvent.click(await screen.findByRole('button', BUTTON))
    await screen.findByTestId('explanation-text')
    fireEvent.click(screen.getByRole('button', BUTTON))
    await waitFor(() => expect(explainParams).toHaveLength(2))
  })

  it.each([
    [503, 'AI explanations are not configured'],
    [429, 'Too many attempts; please try again later'],
    [422, 'File is too large to explain (limit 64 KiB)'],
    [502, 'The AI service is unavailable; try again later'],
    [404, 'File not found'],
    [409, 'File content is not available; try again later'],
  ])('shows the exact server detail for %i', async (status, detail) => {
    sharedClient = createTestQueryClient()
    serve({ [EXPLAIN_KEY]: { status, data: { detail } } })
    renderViewer('src/app.py')
    fireEvent.click(await screen.findByRole('button', BUTTON))
    const section = within(screen.getByRole('region', { name: 'File explanation' }))
    expect((await section.findByRole('alert')).textContent).toBe(detail)
    expect(screen.queryByTestId('explanation-text')).toBeNull()
  })
})

describe('Switching files', () => {
  it('clears the previous explanation and error', async () => {
    sharedClient = createTestQueryClient()
    serve({
      [EXPLAIN_KEY]: (config) => {
        const path = (config.params as { path: string }).path
        return path === 'b.py'
          ? { status: 502, data: { detail: 'The AI service is unavailable; try again later' } }
          : { status: 200, data: explanation(path, { explanation: `About ${path}` }) }
      },
    })
    const view = renderViewer('a.py')
    fireEvent.click(await screen.findByRole('button', BUTTON))
    expect((await screen.findByTestId('explanation-text')).textContent).toBe('About a.py')

    view.rerender(withClient(<FileViewer projectId={P} repositoryId={R} selectedPath="b.py" />))
    await waitFor(() => expect(screen.getByTestId('file-header').textContent).toContain('b.py'))
    expect(screen.queryByTestId('explanation-text')).toBeNull() // nothing carried over
    fireEvent.click(screen.getByRole('button', BUTTON))
    expect((await screen.findByRole('alert')).textContent).toBe(
      'The AI service is unavailable; try again later',
    )

    view.rerender(withClient(<FileViewer projectId={P} repositoryId={R} selectedPath="a.py" />))
    await waitFor(() => expect(screen.getByTestId('file-header').textContent).toContain('a.py'))
    expect(screen.queryByRole('alert')).toBeNull() // the error is gone too
    expect(screen.queryByTestId('explanation-text')).toBeNull() // and nothing is cached
  })
})

describe('Repository page integration', () => {
  it('explains the file selected in the tree', async () => {
    sharedClient = createTestQueryClient()
    const tree: TreeDirectory = {
      type: 'directory',
      name: '',
      path: '',
      children: [
        { type: 'file', name: 'app.py', path: 'app.py', extension: 'py', language: 'Python', size_bytes: 9 },
      ],
    }
    serve({
      [`GET ${BASE}`]: { status: 200, data: { ...ZIP_REPOSITORY, status: 'ready' } },
      [`GET ${BASE}/tree`]: {
        status: 200,
        data: { repository_id: R, status: 'ready', file_count: 1, total_bytes: 9, tree },
      },
      [`GET ${BASE}/languages`]: {
        status: 200,
        data: { repository_id: R, status: 'ready', total_files: 1, total_bytes: 9, languages: [] },
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
    })
    render(
      withClient(
        <MemoryRouter initialEntries={[BASE]}>
          <Routes>
            <Route path="/projects/:projectId/repositories/:repositoryId" element={<RepositoryPage />} />
          </Routes>
        </MemoryRouter>,
      ),
    )
    fireEvent.click(await screen.findByRole('button', { name: /app\.py/ }))
    const fileCard = within(screen.getByRole('region', { name: 'File' }))
    fireEvent.click(await fileCard.findByRole('button', BUTTON))
    expect((await fileCard.findByTestId('explanation-text')).textContent).toBe('This file prints 1.')
    expect(screen.getByRole('region', { name: 'File' }).querySelector('pre code')?.textContent).toBe(
      'print(1)\n',
    )
  })
})
