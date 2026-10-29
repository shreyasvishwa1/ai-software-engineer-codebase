import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { mockApi, PROJECT, ZIP_REPOSITORY } from '../test/mockApi'
import { renderWithProviders } from '../test/render'
import type { Repository, RepositoryLanguages, RepositoryTree } from '../types/api'
import { RepositoryDashboard } from './RepositoryDashboard'

const REPO: Repository = { ...ZIP_REPOSITORY, status: 'ready' }
const URL = `/projects/${PROJECT.id}/repositories/${REPO.id}`

const TREE: RepositoryTree = {
  repository_id: REPO.id,
  status: 'ready',
  file_count: 1234,
  total_bytes: 2048,
  tree: { type: 'directory', name: '', path: '', children: [] },
}

const LANGUAGES: RepositoryLanguages = {
  repository_id: REPO.id,
  status: 'ready',
  total_files: 1234,
  total_bytes: 2048,
  languages: [
    { language: 'Python', file_count: 1000, total_bytes: 1500, percentage: 73.24 },
    { language: 'TypeScript', file_count: 200, total_bytes: 500, percentage: 24.41 },
    { language: null, file_count: 34, total_bytes: 48, percentage: 2.35 },
  ],
}

const FILES = [
  { path: 'src/small.py', language: 'Python', status: 'analyzed', line_count: 40, blank_line_count: 4 },
  { path: 'src/medium.py', language: 'Python', status: 'analyzed', line_count: 600, blank_line_count: 60 },
  { path: 'src/deep/huge.py', language: 'Python', status: 'analyzed', line_count: 1200, blank_line_count: 100 },
  { path: 'src/edge.py', language: 'Python', status: 'analyzed', line_count: 1000, blank_line_count: 0 },
  { path: 'logo.png', language: null, status: 'binary', line_count: null, blank_line_count: null },
]

const METRICS = {
  job: null,
  summary: { files: 5, analyzed: 4, binary: 1, too_large: 0, unavailable: 0, total_lines: 1840, blank_lines: 164 },
  files: FILES,
}

const INVENTORY = {
  manifests: [
    {
      path: 'package.json',
      ecosystem: 'npm',
      status: 'parsed',
      dependencies: [
        { name: 'react', specifier: '^18', scope: 'runtime' },
        { name: 'axios', specifier: '^1', scope: 'runtime' },
        { name: 'vitest', specifier: '^3', scope: 'dev' },
      ],
    },
    {
      path: 'requirements.txt',
      ecosystem: 'python',
      status: 'parsed',
      dependencies: [{ name: 'fastapi', specifier: '', scope: 'runtime' }],
    },
  ],
  summary: { manifests: 2, parsed: 2, invalid: 0, too_large: 0, unavailable: 0, dependencies: 4 },
  truncated: false,
}

const SCAN = {
  findings: [],
  summary: { files_scanned: 1234, binary: 0, too_large: 0, unavailable: 0, findings: 0 },
  truncated: false,
}

const JOB = {
  id: 'job-1',
  project_id: PROJECT.id,
  repository_id: REPO.id,
  job_type: 'ingestion',
  status: 'completed',
  progress: 100,
  error_message: null,
  created_at: '2026-10-01T00:00:00Z',
  started_at: '2026-10-01T00:00:00Z',
  completed_at: '2026-10-01T00:00:04Z',
}

function routes(overrides: Record<string, unknown> = {}) {
  return {
    [`GET ${URL}/analysis/code-metrics`]: { status: 200, data: METRICS },
    [`GET ${URL}/analysis/dependencies`]: { status: 200, data: INVENTORY },
    [`GET ${URL}/analysis/secrets`]: { status: 200, data: SCAN },
    [`GET ${URL}/jobs`]: { status: 200, data: [JOB, { ...JOB, id: 'job-2', status: 'failed', completed_at: '2026-10-01T00:00:30Z' }] },
    ...overrides,
  } as Parameters<typeof mockApi>[0]
}

function render(onSelectFile?: (path: string) => void, repository: Repository = REPO) {
  renderWithProviders(
    <RepositoryDashboard
      projectId={PROJECT.id}
      repositoryId={REPO.id}
      repository={repository}
      tree={TREE}
      languages={LANGUAGES}
      onSelectFile={onSelectFile}
    />,
  )
  return screen.getByRole('region', { name: 'Repository dashboard' })
}

const tile = (dashboard: HTMLElement, label: string) =>
  within(dashboard).getByText(label, { selector: 'p' }).parentElement as HTMLElement

const health = () => screen.getByRole('region', { name: 'Repository health' })

describe('RepositoryDashboard', () => {
  it('shows the headline numbers from the shared queries', async () => {
    mockApi(routes())
    const dashboard = render()
    await waitFor(() => expect(tile(dashboard, 'Secret findings').textContent).toContain('None found'))
    expect(tile(dashboard, 'Files').textContent).toBe('Files1,2342.0 KB stored')
    expect(tile(dashboard, 'Lines of code').textContent).toBe('Lines of code1,6761,840 lines in total')
    // Unknown (null) languages are not counted as a language.
    expect(tile(dashboard, 'Languages').textContent).toBe('Languages2Mostly Python (73%)')
    expect(tile(dashboard, 'Dependencies').textContent).toBe('Dependencies4From 2 manifests')
    expect(tile(dashboard, 'Secret findings').textContent).toBe('Secret findings0None found')
  })

  it('scores health as the share of checks that pass', async () => {
    mockApi(routes())
    render()
    await waitFor(() => expect(health().textContent).toContain('80%'))
    expect(health().textContent).toContain('4 of 5 checks passing')
    expect(health().textContent).toContain('Healthy')
    expect(within(health()).getByLabelText('Needs attention').closest('li')?.textContent).toBe(
      'No files over 1,000 lines2 files at 1,000+ lines',
    )
    expect(within(health()).getAllByLabelText('Passing')).toHaveLength(4)
  })

  it('flags committed secrets and marks the score at risk when most checks fail', async () => {
    mockApi(
      routes({
        [`GET ${URL}/analysis/secrets`]: { status: 200, data: { ...SCAN, summary: { ...SCAN.summary, findings: 3 } } },
        [`GET ${URL}/analysis/dependencies`]: {
          status: 200,
          data: { ...INVENTORY, summary: { ...INVENTORY.summary, invalid: 1 } },
        },
      }),
    )
    const dashboard = render(undefined, { ...REPO, status: 'failed' })
    await waitFor(() => expect(tile(dashboard, 'Secret findings').textContent).toBe('Secret findings3Review them in Secrets below'))
    expect(health().textContent).toContain('1 of 5 checks passing')
    expect(health().textContent).toContain('20%')
    expect(health().textContent).toContain('At risk')
    expect(health().textContent).toContain('3 findings in 1,234 files')
    expect(health().textContent).toContain('1 of 2 could not be read')
  })

  it('says it is loading while queries run, and leaves those checks out of the score', () => {
    mockApi(routes())
    const dashboard = render()
    expect(tile(dashboard, 'Secret findings').textContent).toBe('Secret findings—Scanning…')
    expect(tile(dashboard, 'Dependencies').textContent).toBe('Dependencies—Checking…')
    expect(tile(dashboard, 'Lines of code').textContent).toBe('Lines of code—Loading…')
    expect(health().textContent).toContain('1 of 1 checks passing')
    expect(health().textContent).toContain('4 not run yet')
  })

  it('says a failed request is unavailable instead of pretending nothing ran', async () => {
    mockApi(routes({ [`GET ${URL}/analysis/secrets`]: { status: 429, data: { detail: 'Too many requests' } } }))
    const dashboard = render()
    await waitFor(() =>
      expect(tile(dashboard, 'Secret findings').textContent).toBe('Secret findings—Unavailable right now'),
    )
    expect(within(health()).getByText('No likely secrets committed').nextSibling?.textContent).toBe(
      'Unavailable right now',
    )
  })

  it('asks for code analysis when the snapshot has no metrics yet', async () => {
    mockApi(routes({ [`GET ${URL}/analysis/code-metrics`]: { status: 200, data: { job: null, summary: null, files: [] } } }))
    const dashboard = render()
    await waitFor(() => expect(tile(dashboard, 'Lines of code').textContent).toBe('Lines of code—Run "Analyze code"'))
    expect(screen.getByRole('region', { name: 'Largest files' }).textContent).toContain('Run "Analyze code" to measure')
    expect(within(health()).getByText('Code metrics up to date').nextSibling?.textContent).toBe('Run "Analyze code" below')
    expect(within(health()).getByText('No files over 1,000 lines').nextSibling?.textContent).toBe('Needs code metrics')
  })

  it('ranks the largest files and opens one by its full path', async () => {
    mockApi(routes())
    const onSelectFile = vi.fn()
    render(onSelectFile)
    const panel = screen.getByRole('region', { name: 'Largest files' })
    const buttons = await within(panel).findAllByRole('button')
    expect(buttons.map((b) => b.getAttribute('aria-label'))).toEqual([
      'Open src/deep/huge.py (1,200 lines)',
      'Open src/edge.py (1,000 lines)',
      'Open src/medium.py (600 lines)',
      'Open src/small.py (40 lines)',
    ])
    expect(buttons[0].textContent).toBe('huge.py1,200')
    fireEvent.click(buttons[2])
    expect(onSelectFile).toHaveBeenCalledWith('src/medium.py')
  })

  it('buckets analyzed files by size and counts dependencies per ecosystem', async () => {
    mockApi(routes())
    render()
    const sizes = screen.getByRole('region', { name: 'File size distribution' })
    await waitFor(() => expect(sizes.textContent).toContain('Under 100 lines1 files'))
    expect(within(sizes).getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      'Under 100 lines1 files',
      '100–499 lines0 files',
      '500–999 lines1 files',
      '1,000+ lines2 files',
    ])
    const deps = screen.getByRole('region', { name: 'Dependencies by ecosystem' })
    expect(deps.textContent).toContain('npm3 declared')
    expect(deps.textContent).toContain('Python1 declared')
    expect(within(deps).getByTitle('Runtime: 2')).toBeTruthy()
    expect(within(deps).getByTitle('Development: 1')).toBeTruthy()
  })

  it('summarizes job activity', async () => {
    mockApi(routes())
    render()
    const activity = screen.getByRole('region', { name: 'Job activity' })
    await waitFor(() => expect(activity.textContent).toContain('Jobs run2'))
    expect(activity.textContent).toContain('Succeeded1')
    expect(activity.textContent).toContain('Failed1')
    expect(activity.textContent).toContain('Average run4.0 s')
  })

  it('folds languages beyond five into Other', async () => {
    mockApi(routes())
    const many = Array.from({ length: 7 }, (_, i) => ({
      language: `Lang${i}`,
      file_count: 10 - i,
      total_bytes: 10,
      percentage: i < 5 ? 15 : 12.5,
    }))
    renderWithProviders(
      <RepositoryDashboard
        projectId={PROJECT.id}
        repositoryId={REPO.id}
        repository={REPO}
        tree={TREE}
        languages={{ ...LANGUAGES, languages: many }}
      />,
    )
    const legend = within(screen.getByRole('region', { name: 'Code distribution' })).getAllByRole('listitem')
    expect(legend.map((li) => li.textContent)).toEqual([
      'Lang015.0%',
      'Lang115.0%',
      'Lang215.0%',
      'Lang315.0%',
      'Lang415.0%',
      'Other25.0%',
    ])
  })

  it('makes exactly one request per shared query and nothing else', async () => {
    const requests = mockApi(routes())
    const dashboard = render()
    await waitFor(() => expect(tile(dashboard, 'Secret findings').textContent).toContain('None found'))
    expect(requests.map((r) => `${r.method} ${r.url}`).sort()).toEqual(
      ['/analysis/code-metrics', '/analysis/dependencies', '/analysis/secrets', '/jobs']
        .map((suffix) => `GET ${URL}${suffix}`)
        .sort(),
    )
  })
})
