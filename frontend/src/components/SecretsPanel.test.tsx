import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { AxiosError, type InternalAxiosRequestConfig } from 'axios'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { queryKeys } from '../hooks/queryKeys'
import { RepositoryPage } from '../pages/RepositoryPage'
import { mockApi, type MockResponse, PROJECT, ZIP_REPOSITORY } from '../test/mockApi'
import { createTestQueryClient } from '../test/render'
import type { AnalysisJob, SecretFinding, SecretScan, TreeDirectory } from '../types/api'
import { JobProgress } from './JobProgress'
import { CAVEAT, CLEAN_MESSAGE, SecretsPanel, TRUNCATED_NOTICE } from './SecretsPanel'

// Fixtures hold only locations and rule names: no credential-shaped strings anywhere.

const P = PROJECT.id
const R = ZIP_REPOSITORY.id
const BASE = `/projects/${P}/repositories/${R}`
const SECRETS_PATH = `${BASE}/analysis/secrets`
const GET_SECRETS = `GET ${SECRETS_PATH}`
const JOBS_KEY = `GET ${BASE}/jobs`
const HTML = '<img src=x onerror=alert(1)>'

// Server order (path, line, rule); deliberately not the order a naive client sort would give.
const FINDINGS: SecretFinding[] = [
  { path: 'B.env', line: 9, rule: 'slack-token', description: 'Slack token' },
  { path: 'a/config.py', line: 12, rule: 'aws-access-key-id', description: 'AWS access key ID' },
  { path: 'a/config.py', line: 12, rule: 'github-token', description: 'GitHub token' },
  { path: 'a/config.py', line: 3, rule: 'private-key', description: 'Private key' },
]

function scan(extra: Partial<SecretScan> = {}): SecretScan {
  return {
    findings: FINDINGS,
    summary: { files_scanned: 7, binary: 2, too_large: 1, unavailable: 3, findings: 4 },
    truncated: false,
    ...extra,
  }
}

const CLEAN: SecretScan = {
  findings: [],
  summary: { files_scanned: 5, binary: 0, too_large: 0, unavailable: 0, findings: 0 },
  truncated: false,
}

function ok(data: unknown): MockResponse {
  return { status: 200, data }
}

type Handler = MockResponse | ((config: InternalAxiosRequestConfig) => MockResponse)

function renderPanel(route: Handler, onSelectFile?: (path: string) => void) {
  const requests = mockApi({ [GET_SECRETS]: route })
  const view = render(
    <QueryClientProvider client={createTestQueryClient()}>
      <SecretsPanel projectId={P} repositoryId={R} onSelectFile={onSelectFile} />
    </QueryClientProvider>,
  )
  return { requests, ...view }
}

const findingsTable = () => screen.findByRole('table', { name: 'Secret findings' })

async function findingRows(): Promise<string[][]> {
  const table = await findingsTable()
  return within(table)
    .getAllByRole('row')
    .slice(1) // header
    .map((row) => within(row).getAllByRole('cell').map((cell) => cell.textContent ?? ''))
}

describe('SecretsPanel states', () => {
  it('shows a loading state, then the result', async () => {
    renderPanel(ok(scan()))
    expect(screen.getByText('Scanning for secrets…')).toBeTruthy()
    await findingsTable()
    expect(screen.queryByText('Scanning for secrets…')).toBeNull()
  })

  it('shows a clean repository with its summary and the caveat', async () => {
    renderPanel(ok(CLEAN))
    expect(await screen.findByText(CLEAN_MESSAGE)).toBeTruthy()
    expect(CLEAN_MESSAGE).toBe('No likely secrets found.')
    expect(screen.getByText(CAVEAT)).toBeTruthy()
    expect(CAVEAT).toBe(
      'Pattern-based scan for a few common credential formats; no findings does not guarantee there are no secrets.',
    )
    expect(screen.getByRole('table', { name: 'Secret scan summary' })).toBeTruthy()
    expect(screen.queryByRole('table', { name: 'Secret findings' })).toBeNull()
    expect(screen.queryByText(TRUNCATED_NOTICE)).toBeNull()
  })

  it('shows all five summary values', async () => {
    renderPanel(ok(scan()))
    const summary = await screen.findByRole('table', { name: 'Secret scan summary' })
    const rows = within(summary)
      .getAllByRole('row')
      .map((row) => [
        within(row).getByRole('rowheader').textContent,
        within(row).getByRole('cell').textContent,
      ])
    expect(rows).toEqual([
      ['Files scanned', '7'],
      ['Binary or not UTF-8', '2'],
      ['Too large', '1'],
      ['Unavailable', '3'],
      ['Findings', '4'],
    ])
  })

  it('lists findings with file, line and type, and keeps the caveat', async () => {
    renderPanel(ok(scan()))
    const table = await findingsTable()
    const headers = within(table)
      .getAllByRole('columnheader')
      .map((header) => header.textContent)
    expect(headers).toEqual(['File', 'Line', 'Type'])
    expect(await findingRows()).toEqual([
      ['B.env', '9', 'Slack tokenslack-token'],
      ['a/config.py', '12', 'AWS access key IDaws-access-key-id'],
      ['a/config.py', '12', 'GitHub tokengithub-token'],
      ['a/config.py', '3', 'Private keyprivate-key'],
    ])
    expect(screen.queryByText(CLEAN_MESSAGE)).toBeNull()
    expect(screen.getByText(CAVEAT)).toBeTruthy()
  })

  it('keeps the exact server order, without sorting or deduplicating', async () => {
    const order: SecretFinding[] = [
      { path: 'z.txt', line: 2, rule: 'private-key', description: 'Private key' },
      { path: 'a.txt', line: 1, rule: 'google-api-key', description: 'Google API key' },
      { path: 'a.txt', line: 1, rule: 'google-api-key', description: 'Google API key' },
      { path: 'm.txt', line: 10, rule: 'anthropic-api-key', description: 'Anthropic API key' },
      { path: 'm.txt', line: 2, rule: 'stripe-live-key', description: 'Stripe live secret key' },
    ]
    renderPanel(ok(scan({ findings: order })))
    const rows = await findingRows()
    expect(rows.map(([path, line]) => `${path}:${line}`)).toEqual([
      'z.txt:2',
      'a.txt:1',
      'a.txt:1',
      'm.txt:10',
      'm.txt:2',
    ])
  })

  it('shows the truncated notice only when truncated is true', async () => {
    const { unmount } = renderPanel(ok(scan({ truncated: true })))
    expect(await screen.findByText(TRUNCATED_NOTICE)).toBeTruthy()
    expect(TRUNCATED_NOTICE).toBe(
      'Results are incomplete: the scan stopped at a limit (at most 5,000 files, 64 MiB or 500 findings).',
    )
    unmount()

    renderPanel(ok(scan({ truncated: false })))
    await findingsTable()
    expect(screen.queryByText(TRUNCATED_NOTICE)).toBeNull()
  })

  it('shows the truncated notice with a clean result too', async () => {
    renderPanel(ok({ ...CLEAN, truncated: true }))
    expect(await screen.findByText(TRUNCATED_NOTICE)).toBeTruthy()
    expect(screen.getByText(CLEAN_MESSAGE)).toBeTruthy()
  })
})

describe('SecretsPanel errors', () => {
  it.each([
    [401, 'Could not validate credentials'],
    [404, 'Repository not found'],
    [429, 'Too many attempts; please try again later'],
  ])('shows the exact server detail for %i', async (status, detail) => {
    renderPanel({ status, data: { detail } })
    expect((await screen.findByRole('alert')).textContent).toBe(detail)
    expect(screen.queryByRole('table')).toBeNull()
  })

  it('falls back to the panel message without a server detail', async () => {
    renderPanel({ status: 500, data: {} })
    expect((await screen.findByRole('alert')).textContent).toBe('Could not scan for secrets')
  })

  it('uses the existing network error message', async () => {
    renderPanel(() => {
      throw new AxiosError('Network Error', AxiosError.ERR_NETWORK)
    })
    expect((await screen.findByRole('alert')).textContent).toBe('Could not reach the server')
  })

  it('renders an HTML-looking error detail as text', async () => {
    renderPanel({ status: 404, data: { detail: HTML } })
    expect((await screen.findByRole('alert')).textContent).toBe(HTML)
    expect(document.querySelector('img, script, a')).toBeNull()
  })
})

describe('SecretsPanel requests', () => {
  it('makes exactly one GET to the secret scan endpoint and does not poll', async () => {
    let calls = 0
    const { requests } = renderPanel(() => {
      calls += 1
      return ok(scan())
    })
    await findingsTable()
    await new Promise((resolve) => setTimeout(resolve, 150))
    expect(calls).toBe(1)
    expect(requests).toHaveLength(1)
    expect(requests[0].method).toBe('GET')
    expect(requests[0].url).toBe(`/projects/${P}/repositories/${R}/analysis/secrets`)
    expect(requests[0].url).toContain(P)
    expect(requests[0].url).toContain(R)
  })

  it('uses the exact project and repository ids it is given', async () => {
    const requests = mockApi({
      'GET /projects/proj-x/repositories/repo-y/analysis/secrets': ok(CLEAN),
    })
    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <SecretsPanel projectId="proj-x" repositoryId="repo-y" />
      </QueryClientProvider>,
    )
    expect(await screen.findByText(CLEAN_MESSAGE)).toBeTruthy()
    expect(requests.map((r) => `${r.method} ${r.url}`)).toEqual([
      'GET /projects/proj-x/repositories/repo-y/analysis/secrets',
    ])
  })

  it('makes no request when the panel is not rendered', async () => {
    const requests = mockApi({ [GET_SECRETS]: ok(scan()) })
    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <p>nothing here</p>
      </QueryClientProvider>,
    )
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(requests).toHaveLength(0)
  })

  it('never requests file content', async () => {
    const onSelectFile = vi.fn()
    const { requests } = renderPanel(ok(scan()), onSelectFile)
    await findingsTable()
    fireEvent.click(screen.getAllByRole('button', { name: /in the viewer/ })[0])
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(requests.map((r) => r.url)).toEqual([SECRETS_PATH])
  })

  it('refreshes the scan when an ingestion job finishes', async () => {
    let jobCalls = 0
    let scanCalls = 0
    const job = (status: AnalysisJob['status']): AnalysisJob => ({
      id: 'job-1',
      project_id: P,
      repository_id: R,
      job_type: 'ingestion',
      status,
      progress: status === 'completed' ? 100 : 0,
      error_message: null,
      created_at: '2026-10-02T00:00:00Z',
      started_at: null,
      completed_at: null,
    })
    mockApi({
      [JOBS_KEY]: () => {
        jobCalls += 1
        return { status: 200, data: [job(jobCalls >= 3 ? 'completed' : 'processing')] }
      },
      [GET_SECRETS]: () => {
        scanCalls += 1
        return ok(scanCalls === 1 ? scan() : CLEAN)
      },
    })
    const queryClient = createTestQueryClient()
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    render(
      <QueryClientProvider client={queryClient}>
        <JobProgress projectId={P} repositoryId={R} pollIntervalMs={20} />
        <SecretsPanel projectId={P} repositoryId={R} />
      </QueryClientProvider>,
    )
    await findingsTable()
    await waitFor(() => expect(screen.getByTestId('status-badge').textContent).toBe('completed'))
    expect(await screen.findByText(CLEAN_MESSAGE)).toBeTruthy()
    expect(scanCalls).toBeGreaterThanOrEqual(2)
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: queryKeys.secretScan(P, R),
      exact: true,
    })
  })
})

describe('SecretsPanel navigation', () => {
  it('has a separate View file button per finding that passes the exact path', async () => {
    const onSelectFile = vi.fn()
    renderPanel(ok(scan()), onSelectFile)
    const table = await findingsTable()
    const buttons = within(table).getAllByRole('button')
    expect(buttons).toHaveLength(FINDINGS.length)
    for (const button of buttons) {
      expect(button.textContent).toBe('View file')
      expect(button.closest('a')).toBeNull()
      expect(button.querySelector('button, a')).toBeNull() // nothing nested
    }
    expect(buttons[0].getAttribute('aria-label')).toBe(
      'Open B.env in the viewer (line 9, Slack token)',
    )
    fireEvent.click(buttons[3])
    fireEvent.click(buttons[0])
    expect(onSelectFile.mock.calls).toEqual([['a/config.py'], ['B.env']])
  })

  it('passes an unusual path through unchanged', async () => {
    const onSelectFile = vi.fn()
    const path = `dir with space/${HTML}/ü%2e%2e.env`
    renderPanel(
      ok(scan({ findings: [{ path, line: 1, rule: 'private-key', description: 'Private key' }] })),
      onSelectFile,
    )
    fireEvent.click(await screen.findByRole('button', { name: /in the viewer/ }))
    expect(onSelectFile).toHaveBeenCalledWith(path)
  })

  it('has no View file buttons without onSelectFile', async () => {
    renderPanel(ok(scan()))
    await findingsTable()
    expect(screen.queryAllByRole('button')).toHaveLength(0)
    expect(screen.queryByText('View file')).toBeNull()
  })
})

describe('SecretsPanel safety', () => {
  it('renders HTML- and script-looking values as plain text', async () => {
    const script = '<script>alert("x")</script>'
    const link = '<a href="https://example.invalid">click</a>'
    renderPanel(
      ok(
        scan({
          findings: [
            { path: HTML, line: 1, rule: script as SecretFinding['rule'], description: link },
          ],
        }),
      ),
      vi.fn(),
    )
    const rows = await findingRows()
    expect(rows[0].slice(0, 3)).toEqual([HTML, '1', `${link}${script}`])
    expect(document.querySelector('img, script, a, iframe')).toBeNull()
    expect(document.querySelector('[href], [src], [onerror]')).toBeNull()
  })

  it('never renders unexpected fields such as a value or a snippet', async () => {
    const extra = {
      value: 'FAKE-VALUE-MARKER',
      secret: 'FAKE-SECRET-MARKER',
      snippet: 'FAKE-SNIPPET-MARKER',
      preview: 'FAKE-PREVIEW-MARKER',
      column: 4242,
      sha256: 'FAKE-SHA-MARKER',
      id: 'FAKE-ID-MARKER',
    }
    const findings = FINDINGS.map((finding) => ({ ...finding, ...extra }))
    renderPanel(ok({ ...scan(), findings, ...extra, summary: { ...scan().summary, ...extra } }), vi.fn())
    await findingsTable()
    const text = document.body.textContent ?? ''
    for (const marker of Object.values(extra)) {
      expect(text).not.toContain(String(marker))
    }
    for (const button of screen.getAllByRole('button')) {
      expect(button.getAttribute('aria-label')).not.toMatch(/FAKE|4242/)
    }
    const summary = screen.getByRole('table', { name: 'Secret scan summary' })
    expect(within(summary).getAllByRole('row')).toHaveLength(5)
  })

  it('uses no browser storage or cookies of its own', async () => {
    const getItem = vi.spyOn(Storage.prototype, 'getItem')
    const writes = [
      vi.spyOn(Storage.prototype, 'setItem'),
      vi.spyOn(Storage.prototype, 'removeItem'),
      vi.spyOn(Storage.prototype, 'clear'),
    ]
    const storage = [getItem, ...writes]
    const cookieGet = vi.fn(() => '')
    const cookieSet = vi.fn()
    Object.defineProperty(document, 'cookie', { configurable: true, get: cookieGet, set: cookieSet })
    try {
      renderPanel(ok(scan()), vi.fn())
      await findingsTable()
      fireEvent.click(screen.getAllByRole('button')[0])
      // The only read is the existing apiClient's access token lookup (services/session.ts).
      expect(getItem.mock.calls).toEqual([['ai_se_access_token']])
      for (const spy of writes) {
        expect(spy).not.toHaveBeenCalled()
      }
      expect(cookieGet).not.toHaveBeenCalled()
      expect(cookieSet).not.toHaveBeenCalled()
    } finally {
      for (const spy of storage) {
        spy.mockRestore()
      }
      delete (document as { cookie?: unknown }).cookie // back to the prototype accessor
    }
  })
})

// --- Repository page ---------------------------------------------------------------------

const TREE: TreeDirectory = {
  type: 'directory',
  name: '',
  path: '',
  children: [
    {
      type: 'directory',
      name: 'a',
      path: 'a',
      children: [
        { type: 'file', name: 'config.py', path: 'a/config.py', extension: 'py', language: 'Python', size_bytes: 40 },
      ],
    },
    { type: 'file', name: 'B.env', path: 'B.env', extension: 'env', language: null, size_bytes: 9 },
  ],
}

const scrollSpy = vi.fn()

afterEach(() => {
  scrollSpy.mockReset()
  delete (HTMLElement.prototype as { scrollIntoView?: unknown }).scrollIntoView
})

function renderPage(secrets: Handler = ok(scan())) {
  const contentPaths: string[] = []
  const requests = mockApi({
    [`GET ${BASE}`]: { status: 200, data: { ...ZIP_REPOSITORY, status: 'ready' } },
    [`GET ${BASE}/tree`]: {
      status: 200,
      data: { repository_id: R, status: 'ready', file_count: 2, total_bytes: 49, tree: TREE },
    },
    [`GET ${BASE}/languages`]: {
      status: 200,
      data: { repository_id: R, status: 'ready', total_files: 2, total_bytes: 49, languages: [] },
    },
    [JOBS_KEY]: { status: 200, data: [] },
    [`GET ${BASE}/analysis/code-metrics`]: {
      status: 200,
      data: { job: null, summary: null, files: [] },
    },
    [`GET ${BASE}/analysis/dependencies`]: {
      status: 200,
      data: {
        manifests: [],
        summary: { manifests: 0, parsed: 0, invalid: 0, too_large: 0, unavailable: 0, dependencies: 0 },
        truncated: false,
      },
    },
    [GET_SECRETS]: secrets,
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

const fileCard = () => screen.getByRole('region', { name: 'File' })
const viewerCode = () => fileCard().querySelector('pre code')?.textContent

describe('RepositoryPage integration', () => {
  it('places Secrets between Dependencies and File, keeping the other panels', async () => {
    renderPage()
    const secrets = await screen.findByRole('region', { name: 'Secrets' })
    expect(await within(secrets).findByRole('table', { name: 'Secret findings' })).toBeTruthy()
    const metrics = screen.getByRole('region', { name: 'Code metrics' })
    const deps = screen.getByRole('region', { name: 'Dependencies' })
    const file = fileCard()
    const FOLLOWING = Node.DOCUMENT_POSITION_FOLLOWING
    expect(metrics.compareDocumentPosition(deps) & FOLLOWING).toBeTruthy()
    expect(deps.compareDocumentPosition(secrets) & FOLLOWING).toBeTruthy()
    expect(secrets.compareDocumentPosition(file) & FOLLOWING).toBeTruthy()
    for (const name of ['Files', 'Languages']) {
      expect(screen.getByRole('region', { name })).toBeTruthy()
    }
    expect(within(file).getByText('Select a file to view its content.')).toBeTruthy()
  })

  it('opens a finding in the existing viewer, marks it in the tree and scrolls to it', async () => {
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
      configurable: true,
      writable: true,
      value: scrollSpy,
    })
    const { contentPaths, requests } = renderPage()
    const secrets = within(await screen.findByRole('region', { name: 'Secrets' }))
    fireEvent.click(
      await secrets.findByRole('button', { name: 'Open a/config.py in the viewer (line 3, Private key)' }),
    )
    await waitFor(() => expect(viewerCode()).toBe('content of a/config.py'))
    expect(contentPaths).toEqual(['a/config.py'])
    const contentRequests = requests.filter((r) => r.url.endsWith('/files/content'))
    expect(contentRequests.map((r) => r.url)).toEqual([`${BASE}/files/content`]) // path not in URL
    expect(scrollSpy).toHaveBeenCalledTimes(1)
    expect(scrollSpy.mock.contexts[0]).toBe(fileCard())
    const tree = within(screen.getByRole('navigation', { name: 'File tree' }))
    expect(tree.getByRole('button', { name: /config\.py/ }).getAttribute('aria-current')).toBe('true')
    expect(requests.filter((r) => r.url === SECRETS_PATH)).toHaveLength(1)
  })

  it('keeps tree selection and the viewer working, without scrolling', async () => {
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
      configurable: true,
      writable: true,
      value: scrollSpy,
    })
    const { contentPaths } = renderPage()
    await screen.findByRole('table', { name: 'Secret findings' })
    const tree = within(screen.getByRole('navigation', { name: 'File tree' }))
    fireEvent.click(tree.getByRole('button', { name: /B\.env/ }))
    await waitFor(() => expect(viewerCode()).toBe('content of B.env'))
    expect(tree.getByRole('button', { name: /B\.env/ }).getAttribute('aria-current')).toBe('true')
    expect(scrollSpy).not.toHaveBeenCalled()
    expect(contentPaths).toEqual(['B.env'])
  })

  it('shows a secret scan error inside its own card only', async () => {
    renderPage({ status: 429, data: { detail: 'Too many attempts; please try again later' } })
    const secrets = within(await screen.findByRole('region', { name: 'Secrets' }))
    expect((await secrets.findByRole('alert')).textContent).toBe(
      'Too many attempts; please try again later',
    )
    expect(screen.getAllByRole('alert')).toHaveLength(1)
    expect(within(fileCard()).getByText('Select a file to view its content.')).toBeTruthy()
  })
})
