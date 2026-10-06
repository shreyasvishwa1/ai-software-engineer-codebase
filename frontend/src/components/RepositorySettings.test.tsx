import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { AxiosError, type AxiosAdapter, type InternalAxiosRequestConfig } from 'axios'
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { queryKeys } from '../hooks/queryKeys'
import { ProjectPage } from '../pages/ProjectPage'
import { RepositoryPage } from '../pages/RepositoryPage'
import { apiClient } from '../services/client'
import {
  GITHUB_REPOSITORY,
  mockApi,
  type MockResponse,
  PROJECT,
  type RecordedRequest,
  ZIP_REPOSITORY,
} from '../test/mockApi'
import { createTestQueryClient } from '../test/render'
import type { AnalysisJob, Repository } from '../types/api'

type MockRoute = MockResponse | ((config: InternalAxiosRequestConfig) => MockResponse)

const P = PROJECT.id
const PROJECT_URL = `/projects/${P}`
const ALPHA: Repository = { ...ZIP_REPOSITORY, name: 'Alpha', status: 'ready' }
const BETA: Repository = { ...GITHUB_REPOSITORY, name: 'Beta', status: 'ready', default_branch: 'main' }
const ALPHA_URL = `${PROJECT_URL}/repositories/${ALPHA.id}`
const BETA_URL = `${PROJECT_URL}/repositories/${BETA.id}`
const LIST_URL = `${PROJECT_URL}/repositories`
const SERVER: Repository = { ...ALPHA, name: 'Server Name', updated_at: '2026-10-06T10:00:00Z' }
const RUNNING: AnalysisJob = {
  id: '66666666-6666-6666-6666-666666666666',
  project_id: P,
  repository_id: ALPHA.id,
  job_type: 'ingestion',
  status: 'processing',
  progress: 40,
  error_message: null,
  created_at: '2026-10-06T09:00:00Z',
  started_at: '2026-10-06T09:00:01Z',
  completed_at: null,
}

// The approved wording, written here independently of the component.
const REQUIRED = 'Repository name is required'
const TOO_LONG = 'Repository name must be at most 255 characters'
const UPDATE_FALLBACK = 'Could not update the repository'
const DELETE_FALLBACK = 'Could not delete the repository'
const NETWORK = 'Could not reach the server'
const NOT_FOUND = 'Repository not found'
const warning = (name: string) =>
  `This permanently deletes ${name} with all of its files and analysis results. It cannot be undone.`

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

/** A PATCH answered like the server: the stored repository with the sent fields applied. */
function echo(base: Repository): MockRoute {
  return (config) => ({
    status: 200,
    data: { ...base, ...(JSON.parse(config.data as string) as Partial<Repository>) },
  })
}

/** Answers each call with the next response; the last one repeats. */
function sequence(...responses: MockRoute[]): MockRoute {
  let call = 0
  return (config) => {
    const next = responses[Math.min(call, responses.length - 1)]
    call += 1
    return typeof next === 'function' ? next(config) : next
  }
}

const networkError: MockRoute = () => {
  throw new AxiosError('Network Error', AxiosError.ERR_NETWORK)
}

/** The seven requests a RepositoryPage makes for the repository at ``url``. */
const PAGE_SUFFIXES = ['', '/tree', '/languages', '/jobs', '/analysis/code-metrics', '/analysis/dependencies', '/analysis/secrets']

function pageRoutes(url: string, repository: Repository, jobs: AnalysisJob[] = []): Record<string, MockRoute> {
  const empty = { repository_id: repository.id, status: 'ready', total_bytes: 0 }
  return {
    [`GET ${url}`]: { status: 200, data: repository },
    [`GET ${url}/tree`]: {
      status: 200,
      data: { ...empty, file_count: 0, tree: { type: 'directory', name: '', path: '', children: [] } },
    },
    [`GET ${url}/languages`]: { status: 200, data: { ...empty, total_files: 0, languages: [] } },
    [`GET ${url}/jobs`]: { status: 200, data: jobs },
    [`GET ${url}/analysis/code-metrics`]: { status: 200, data: { job: null, summary: null, files: [] } },
    [`GET ${url}/analysis/dependencies`]: {
      status: 200,
      data: {
        manifests: [],
        summary: { manifests: 0, parsed: 0, invalid: 0, too_large: 0, unavailable: 0, dependencies: 0 },
        truncated: false,
      },
    },
    [`GET ${url}/analysis/secrets`]: {
      status: 200,
      data: {
        findings: [],
        summary: { files_scanned: 0, binary: 0, too_large: 0, unavailable: 0, findings: 0 },
        truncated: false,
      },
    },
  }
}

function routes(extra: Record<string, MockRoute> = {}): Record<string, MockRoute> {
  return {
    [`GET ${PROJECT_URL}`]: { status: 200, data: PROJECT },
    [`GET ${LIST_URL}`]: { status: 200, data: [ALPHA, BETA] },
    ...pageRoutes(ALPHA_URL, ALPHA),
    ...pageRoutes(BETA_URL, BETA),
    [`PATCH ${ALPHA_URL}`]: echo(ALPHA),
    [`PATCH ${BETA_URL}`]: echo(BETA),
    [`DELETE ${ALPHA_URL}`]: { status: 204 },
    ...extra,
  }
}

/** Shows the current location and navigates like a user would, outside the pages. */
function Probe() {
  const location = useLocation()
  const navigate = useNavigate()
  return (
    <div>
      <output data-testid="location">{location.pathname}</output>
      <button type="button" onClick={() => navigate(-1)}>
        Test back
      </button>
      <button type="button" onClick={() => navigate(ALPHA_URL)}>
        Test open Alpha
      </button>
      <button type="button" onClick={() => navigate(BETA_URL)}>
        Test open Beta
      </button>
    </div>
  )
}

/** Renders the real project and repository pages at ``path``; the API must already be mocked. */
function mount(path: string) {
  const queryClient = createTestQueryClient()
  const view = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/projects/:projectId" element={<ProjectPage />} />
          <Route path="/projects/:projectId/repositories/:repositoryId" element={<RepositoryPage />} />
        </Routes>
        <Probe />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return { queryClient, container: view.container }
}

/** Opens a repository page directly (the project's repository list was never loaded). */
async function openRepository(extra: Record<string, MockRoute> = {}, path = ALPHA_URL) {
  const requests = mockApi(routes(extra))
  const view = mount(path)
  await screen.findByRole('region', { name: 'Repository settings' })
  return { requests, ...view }
}

/** Opens Alpha from its project page, so the repository list is cached as in normal use. */
async function openFromProject(extra: Record<string, MockRoute> = {}) {
  const requests = mockApi(routes(extra))
  const view = mount(PROJECT_URL)
  fireEvent.click(await screen.findByRole('link', { name: /Alpha/ }))
  await screen.findByRole('region', { name: 'Repository settings' })
  return { requests, ...view }
}

/**
 * Holds every matching request until released; others go straight to the mock. Install it before
 * the request starts: axios takes the adapter when a request begins.
 */
function hold(method: string, url: string) {
  const inner = apiClient.defaults.adapter as AxiosAdapter
  const held: string[] = []
  let open: () => void = () => undefined
  const gate = new Promise<void>((resolve) => {
    open = resolve
  })
  apiClient.defaults.adapter = (async (config: InternalAxiosRequestConfig) => {
    if ((config.method ?? 'get').toUpperCase() === method && config.url === url) {
      held.push(`${method} ${url}`)
      await gate
    }
    return inner(config)
  }) as AxiosAdapter
  return { held, release: () => open() }
}

const settings = () => screen.getByRole('region', { name: 'Repository settings' })
const editRegion = () => screen.queryByRole('region', { name: 'Edit repository details' })
const deleteRegion = () => screen.queryByRole('region', { name: 'Confirm repository deletion' })
const button = (name: string) => screen.getByRole('button', { name }) as HTMLButtonElement
const queryButton = (name: string) => screen.queryByRole('button', { name })
const nameInput = () => screen.getByLabelText('Repository name') as HTMLInputElement
const confirmInput = () =>
  screen.getByLabelText('Type the repository name to confirm') as HTMLInputElement
const heading = () => screen.getByRole('heading', { level: 1 })
const currentPath = () => screen.getByTestId('location').textContent
const alertText = () => within(settings()).queryByRole('alert')?.textContent ?? null
const findAlert = async () => (await within(settings()).findByRole('alert')).textContent

function type(input: HTMLInputElement, value: string) {
  fireEvent.change(input, { target: { value } })
}

function openEdit() {
  fireEvent.click(button('Edit repository'))
  return editRegion() as HTMLElement
}

function openDelete() {
  fireEvent.click(button('Delete repository'))
  return deleteRegion() as HTMLElement
}

function confirmAndDelete(name = ALPHA.name) {
  openDelete()
  type(confirmInput(), name)
  fireEvent.click(button('Delete permanently'))
}

const byMethod = (requests: RecordedRequest[], method: string) =>
  requests.filter((request) => request.method === method)
const sentBody = (request: RecordedRequest) => JSON.parse(request.data as string) as Record<string, unknown>

/** Saves the edit form and returns the single PATCH body it produced. */
async function saveAndGetBody(requests: RecordedRequest[]) {
  fireEvent.click(button('Save changes'))
  await waitFor(() => expect(editRegion()).toBeNull())
  const patches = byMethod(requests, 'PATCH')
  expect(patches).toHaveLength(1)
  return sentBody(patches[0])
}

/** Saves and checks that nothing was sent and the form closed. */
async function saveExpectingNoRequest(requests: RecordedRequest[]) {
  fireEvent.click(button('Save changes'))
  await waitFor(() => expect(editRegion()).toBeNull())
  await sleep(50)
  expect(byMethod(requests, 'PATCH')).toHaveLength(0)
}

/** Requests sent after the (single) DELETE. */
function afterDelete(requests: RecordedRequest[]) {
  const index = requests.findIndex((request) => request.method === 'DELETE')
  expect(index).toBeGreaterThanOrEqual(0)
  return requests.slice(index + 1).map((request) => `${request.method} ${request.url}`)
}

afterEach(() => {
  vi.restoreAllMocks()
})

// --- 1. Visibility -------------------------------------------------------------------------------

describe('Repository settings visibility', () => {
  it('shows the settings region with Edit repository and Delete repository once the repository loads', async () => {
    await openRepository()
    expect(
      within(settings()).getAllByRole('button').map((b) => [b.textContent, (b as HTMLButtonElement).disabled]),
    ).toEqual([
      ['Edit repository', false],
      ['Delete repository', false],
    ])
    expect([editRegion(), deleteRegion()]).toEqual([null, null])
  })

  it('sends no PATCH or DELETE on mount: only the page loads its data', async () => {
    const { requests } = await openRepository()
    await sleep(50)
    expect(requests.map((r) => `${r.method} ${r.url}`).sort()).toEqual(
      PAGE_SUFFIXES.map((suffix) => `GET ${ALPHA_URL}${suffix}`).sort(),
    )
  })

  it('shows nothing while the repository is still loading', async () => {
    mockApi(routes())
    hold('GET', ALPHA_URL)
    mount(ALPHA_URL)
    expect(await screen.findByText('Loading repository…')).toBeTruthy()
    await sleep(50)
    expect(screen.queryByRole('region', { name: 'Repository settings' })).toBeNull()
    expect([queryButton('Edit repository'), queryButton('Delete repository')]).toEqual([null, null])
  })

  it('shows nothing when the repository cannot be loaded', async () => {
    mockApi(routes({ [`GET ${ALPHA_URL}`]: { status: 404, data: { detail: NOT_FOUND } } }))
    mount(ALPHA_URL)
    expect((await screen.findByRole('alert')).textContent).toBe(NOT_FOUND)
    expect(screen.queryByRole('region', { name: 'Repository settings' })).toBeNull()
    expect(queryButton('Edit repository')).toBeNull()
  })
})

// --- 2. Edit -------------------------------------------------------------------------------------

describe('Repository edit form', () => {
  it('opens prefilled with the server name and hides the idle buttons', async () => {
    await openRepository()
    const form = openEdit()
    expect(nameInput().value).toBe('Alpha')
    expect([queryButton('Edit repository'), queryButton('Delete repository')]).toEqual([null, null])
    expect(within(form).getAllByRole('button').map((b) => b.textContent)).toEqual(['Save changes', 'Cancel'])
    expect(settings().contains(form)).toBe(true)
  })

  it('Cancel discards the edit without a request and restores the idle buttons', async () => {
    const { requests } = await openRepository()
    openEdit()
    type(nameInput(), 'Changed name')
    fireEvent.click(button('Cancel'))
    expect(editRegion()).toBeNull()
    expect([button('Edit repository').textContent, button('Delete repository').textContent]).toEqual([
      'Edit repository',
      'Delete repository',
    ])
    await sleep(50)
    expect(byMethod(requests, 'PATCH')).toHaveLength(0)
    expect(heading().textContent).toBe('Alpha')
  })

  it('reopening starts again from the server name', async () => {
    await openRepository()
    openEdit()
    type(nameInput(), 'Changed name')
    fireEvent.click(button('Cancel'))
    openEdit()
    expect(nameInput().value).toBe('Alpha')
  })

  it('never shows the edit form and the delete confirmation together', async () => {
    await openRepository()
    openEdit()
    expect(deleteRegion()).toBeNull()
    expect(queryButton('Delete repository')).toBeNull()
    fireEvent.click(button('Cancel'))
    openDelete()
    expect(editRegion()).toBeNull()
    expect(queryButton('Edit repository')).toBeNull()
  })

  it('compares with the name captured when the form opened, not later cache changes', async () => {
    const { requests, queryClient } = await openRepository()
    openEdit()
    queryClient.setQueryData(queryKeys.repository(P, ALPHA.id), { ...ALPHA, name: 'Renamed elsewhere' })
    await waitFor(() => expect(heading().textContent).toBe('Renamed elsewhere'))
    expect(nameInput().value).toBe('Alpha')
    await saveExpectingNoRequest(requests) // the opened name is unchanged: nothing is sent
  })
})

// --- 3. Validation -------------------------------------------------------------------------------

describe('Repository edit validation', () => {
  it('rejects an empty name without a request', async () => {
    const { requests } = await openRepository()
    openEdit()
    type(nameInput(), '')
    fireEvent.click(button('Save changes'))
    expect(alertText()).toBe(REQUIRED)
    await sleep(50)
    expect(byMethod(requests, 'PATCH')).toHaveLength(0)
    expect(editRegion()).not.toBeNull()
  })

  it('rejects a whitespace-only name without a request', async () => {
    const { requests } = await openRepository()
    openEdit()
    type(nameInput(), '  \t ')
    fireEvent.click(button('Save changes'))
    expect(alertText()).toBe(REQUIRED)
    await sleep(50)
    expect(byMethod(requests, 'PATCH')).toHaveLength(0)
  })

  it('accepts a name of exactly 255 characters', async () => {
    const { requests } = await openRepository()
    openEdit()
    type(nameInput(), 'n'.repeat(255))
    expect(await saveAndGetBody(requests)).toEqual({ name: 'n'.repeat(255) })
  })

  it('rejects a name of 256 characters without a request', async () => {
    const { requests } = await openRepository()
    openEdit()
    type(nameInput(), 'n'.repeat(256))
    fireEvent.click(button('Save changes'))
    expect(alertText()).toBe(TOO_LONG)
    await sleep(50)
    expect(byMethod(requests, 'PATCH')).toHaveLength(0)
  })

  it('checks the length after trimming and sends the trimmed name', async () => {
    const { requests } = await openRepository()
    openEdit()
    type(nameInput(), `  ${'m'.repeat(255)}  `)
    expect(await saveAndGetBody(requests)).toEqual({ name: 'm'.repeat(255) })
  })

  it('sends the name trimmed', async () => {
    const { requests } = await openRepository()
    openEdit()
    type(nameInput(), '  Gamma \t')
    expect(await saveAndGetBody(requests)).toEqual({ name: 'Gamma' })
  })

  it('clears the validation message as soon as a valid name is submitted', async () => {
    const { requests } = await openRepository()
    const patch = hold('PATCH', ALPHA_URL)
    openEdit()
    type(nameInput(), ' ')
    fireEvent.click(button('Save changes'))
    expect(alertText()).toBe(REQUIRED)
    type(nameInput(), 'Gamma')
    fireEvent.click(button('Save changes'))
    await screen.findByRole('button', { name: 'Saving…' })
    expect(alertText()).toBeNull() // already gone while the request is pending
    patch.release()
    await waitFor(() => expect(editRegion()).toBeNull())
    expect(byMethod(requests, 'PATCH').map(sentBody)).toEqual([{ name: 'Gamma' }])
  })
})

// --- 4. PATCH body -------------------------------------------------------------------------------

describe('Repository PATCH body', () => {
  it('sends only the name', async () => {
    const { requests } = await openRepository()
    openEdit()
    type(nameInput(), 'Gamma')
    const body = await saveAndGetBody(requests)
    expect(body).toEqual({ name: 'Gamma' })
    expect(Object.keys(body)).toEqual(['name'])
  })

  it('never sends source_url or default_branch, even for a GitHub repository', async () => {
    const { requests } = await openRepository({}, BETA_URL)
    openEdit()
    type(nameInput(), 'Gamma')
    const body = await saveAndGetBody(requests)
    expect(Object.keys(body)).toEqual(['name'])
    expect('source_url' in body || 'default_branch' in body).toBe(false)
    expect(byMethod(requests, 'PATCH')[0].url).toBe(BETA_URL)
  })

  it('sends no PATCH when the name only differs before trimming, and closes the form', async () => {
    const { requests } = await openRepository()
    openEdit()
    type(nameInput(), ' Alpha ')
    await saveExpectingNoRequest(requests)
    expect(button('Edit repository').textContent).toBe('Edit repository')
  })

  it('sends no PATCH for an unchanged name, and closes the form', async () => {
    const { requests } = await openRepository()
    openEdit()
    await saveExpectingNoRequest(requests)
    expect(heading().textContent).toBe('Alpha')
  })
})

// --- 5. PATCH success ----------------------------------------------------------------------------

describe('Repository save success', () => {
  it('shows the server response, not the submitted name, and closes the form', async () => {
    await openRepository({ [`PATCH ${ALPHA_URL}`]: { status: 200, data: SERVER } })
    openEdit()
    type(nameInput(), 'Typed name')
    fireEvent.click(button('Save changes'))
    await waitFor(() => expect(heading().textContent).toBe('Server Name'))
    expect(screen.queryByText('Typed name')).toBeNull()
    expect(editRegion()).toBeNull()
    expect(button('Edit repository').textContent).toBe('Edit repository')
  })

  it('stores the server response as the cached repository', async () => {
    const { queryClient } = await openRepository({ [`PATCH ${ALPHA_URL}`]: { status: 200, data: SERVER } })
    openEdit()
    type(nameInput(), 'Typed name')
    fireEvent.click(button('Save changes'))
    await waitFor(() => expect(editRegion()).toBeNull())
    expect(queryClient.getQueryData(queryKeys.repository(P, ALPHA.id))).toEqual(SERVER)
  })

  it("replaces only this repository's entry in the cached list with the server response", async () => {
    const { queryClient } = await openFromProject({ [`PATCH ${ALPHA_URL}`]: { status: 200, data: SERVER } })
    openEdit()
    type(nameInput(), 'Typed name')
    fireEvent.click(button('Save changes'))
    await waitFor(() => expect(editRegion()).toBeNull())
    expect(queryClient.getQueryData(queryKeys.repositories(P))).toEqual([SERVER, BETA])
  })

  it('shows the server name on the project page at once while the list request is held', async () => {
    await openFromProject({ [`PATCH ${ALPHA_URL}`]: { status: 200, data: SERVER } })
    const list = hold('GET', LIST_URL)
    openEdit()
    type(nameInput(), 'Typed name')
    fireEvent.click(button('Save changes'))
    await waitFor(() => expect(heading().textContent).toBe('Server Name'))
    fireEvent.click(screen.getByRole('link', { name: '← Project' }))
    expect(await screen.findByRole('link', { name: /Server Name/ })).toBeTruthy()
    expect(screen.queryByText('Alpha')).toBeNull()
    expect(screen.getByRole('link', { name: /Beta/ })).toBeTruthy()
    await waitFor(() => expect(list.held).toEqual([`GET ${LIST_URL}`])) // the refetch never answered
  })

  it('reopening after a save starts from the saved server name', async () => {
    await openRepository({ [`PATCH ${ALPHA_URL}`]: { status: 200, data: SERVER } })
    openEdit()
    type(nameInput(), 'Typed name')
    fireEvent.click(button('Save changes'))
    await waitFor(() => expect(editRegion()).toBeNull())
    openEdit()
    expect(nameInput().value).toBe('Server Name')
  })
})

// --- 6. Pending ----------------------------------------------------------------------------------

describe('Repository actions while pending', () => {
  it('shows Saving… on a disabled button and blocks Cancel until the PATCH answers', async () => {
    await openRepository()
    const patch = hold('PATCH', ALPHA_URL)
    openEdit()
    type(nameInput(), 'Gamma')
    fireEvent.click(button('Save changes'))
    const saving = (await screen.findByRole('button', { name: 'Saving…' })) as HTMLButtonElement
    expect([saving.disabled, button('Cancel').disabled]).toEqual([true, true])
    expect(queryButton('Save changes')).toBeNull()
    patch.release()
    await waitFor(() => expect(heading().textContent).toBe('Gamma'))
  })

  it('sends one PATCH for a double submit', async () => {
    const { requests } = await openRepository()
    const patch = hold('PATCH', ALPHA_URL)
    openEdit()
    type(nameInput(), 'Gamma')
    // Submitted twice before any re-render can disable the button (e.g. Enter pressed twice).
    const form = nameInput().closest('form') as HTMLFormElement
    fireEvent.submit(form)
    fireEvent.submit(form)
    await screen.findByRole('button', { name: 'Saving…' })
    await sleep(50)
    expect(patch.held).toHaveLength(1)
    patch.release()
    await waitFor(() => expect(editRegion()).toBeNull())
    expect(byMethod(requests, 'PATCH')).toHaveLength(1)
  })

  it('shows Deleting… on a disabled button and blocks Cancel until the DELETE answers', async () => {
    await openRepository()
    const remove = hold('DELETE', ALPHA_URL)
    confirmAndDelete()
    const deleting = (await screen.findByRole('button', { name: 'Deleting…' })) as HTMLButtonElement
    expect([deleting.disabled, button('Cancel').disabled]).toEqual([true, true])
    expect(queryButton('Delete permanently')).toBeNull()
    remove.release()
    await waitFor(() => expect(currentPath()).toBe(PROJECT_URL))
  })

  it('sends one DELETE for a double submit', async () => {
    const { requests } = await openRepository()
    const remove = hold('DELETE', ALPHA_URL)
    openDelete()
    type(confirmInput(), ALPHA.name)
    const form = confirmInput().closest('form') as HTMLFormElement
    fireEvent.submit(form)
    fireEvent.submit(form)
    await screen.findByRole('button', { name: 'Deleting…' })
    await sleep(50)
    expect(remove.held).toHaveLength(1)
    remove.release()
    await waitFor(() => expect(currentPath()).toBe(PROJECT_URL))
    expect(byMethod(requests, 'DELETE')).toHaveLength(1)
  })
})

// --- 7. PATCH errors -----------------------------------------------------------------------------

describe('Repository save errors', () => {
  async function failSave(response: MockRoute) {
    const view = await openFromProject({ [`PATCH ${ALPHA_URL}`]: response })
    openEdit()
    type(nameInput(), 'Gamma')
    fireEvent.click(button('Save changes'))
    await findAlert()
    return view
  }

  it('joins the messages of a 422 validation error', async () => {
    await failSave({
      status: 422,
      data: {
        detail: [{ msg: 'Value error, must not be empty' }, { msg: 'Value error, must be at most 255 characters' }],
      },
    })
    expect(alertText()).toBe('Value error, must not be empty; Value error, must be at most 255 characters')
  })

  it('shows a 422 string detail as sent', async () => {
    await failSave({ status: 422, data: { detail: 'Name is not acceptable' } })
    expect(alertText()).toBe('Name is not acceptable')
  })

  it('shows the 404 detail', async () => {
    await failSave({ status: 404, data: { detail: NOT_FOUND } })
    expect(alertText()).toBe(NOT_FOUND)
  })

  it('shows the update fallback for a 500 without detail', async () => {
    await failSave({ status: 500, data: {} })
    expect(alertText()).toBe(UPDATE_FALLBACK)
  })

  it('shows the network message when the server cannot be reached', async () => {
    await failSave(networkError)
    expect(alertText()).toBe(NETWORK)
  })

  it('keeps the form open with the typed name, on the repository page', async () => {
    await failSave({ status: 500, data: {} })
    expect(editRegion()).not.toBeNull()
    expect(nameInput().value).toBe('Gamma')
    expect([button('Save changes').disabled, button('Cancel').disabled]).toEqual([false, false])
    expect(currentPath()).toBe(ALPHA_URL)
  })

  it('leaves the cached repository and list unchanged', async () => {
    const { queryClient } = await failSave({ status: 404, data: { detail: NOT_FOUND } })
    expect(queryClient.getQueryData(queryKeys.repository(P, ALPHA.id))).toEqual(ALPHA)
    expect(queryClient.getQueryData(queryKeys.repositories(P))).toEqual([ALPHA, BETA])
    expect(heading().textContent).toBe('Alpha')
  })

  it('succeeds on retry and clears the error', async () => {
    await failSave(sequence({ status: 500, data: {} }, echo(ALPHA)))
    expect(alertText()).toBe(UPDATE_FALLBACK)
    fireEvent.click(button('Save changes'))
    await waitFor(() => expect(heading().textContent).toBe('Gamma'))
    expect(alertText()).toBeNull()
    expect(editRegion()).toBeNull()
  })

  it('Cancel clears the error and reopening shows none', async () => {
    await failSave({ status: 500, data: {} })
    fireEvent.click(button('Cancel'))
    expect(alertText()).toBeNull()
    openEdit()
    expect(alertText()).toBeNull()
    expect(nameInput().value).toBe('Alpha')
  })
})

// --- 8. Delete confirmation ----------------------------------------------------------------------

describe('Repository delete confirmation', () => {
  it('shows a destructive warning naming the repository as text', async () => {
    await openRepository()
    const region = openDelete()
    expect(within(region).getByText(ALPHA.name, { selector: 'strong' })).toBeTruthy()
    expect(region.querySelector('p')?.textContent).toBe(warning(ALPHA.name))
    expect(within(region).getAllByRole('button').map((b) => b.textContent)).toEqual([
      'Delete permanently',
      'Cancel',
    ])
  })

  it('keeps Delete permanently disabled while empty and enables it for the exact name', async () => {
    await openRepository()
    openDelete()
    expect([confirmInput().value, button('Delete permanently').disabled]).toEqual(['', true])
    type(confirmInput(), 'Alpha')
    expect(button('Delete permanently').disabled).toBe(false)
  })

  it.each([
    ['a case mismatch', 'alpha'],
    ['leading whitespace', ' Alpha'],
    ['trailing whitespace', 'Alpha '],
    ['a partial name', 'Alph'],
    ['a modified name', 'Alpha!'],
  ])('keeps Delete permanently disabled for %s', async (_case, typed) => {
    await openRepository()
    openDelete()
    type(confirmInput(), typed)
    expect(button('Delete permanently').disabled).toBe(true)
  })

  it('never sends DELETE without the exact name, even when the form is submitted directly', async () => {
    const { requests } = await openRepository()
    openDelete()
    const form = confirmInput().closest('form') as HTMLFormElement
    fireEvent.submit(form)
    type(confirmInput(), 'Alpha ')
    fireEvent.submit(form)
    await sleep(50)
    expect(byMethod(requests, 'DELETE')).toHaveLength(0)
    expect(currentPath()).toBe(ALPHA_URL)
  })

  it('Cancel closes the confirmation and reopening starts empty', async () => {
    const { requests } = await openRepository()
    openDelete()
    type(confirmInput(), 'Alpha')
    fireEvent.click(button('Cancel'))
    expect(deleteRegion()).toBeNull()
    expect(button('Delete repository').textContent).toBe('Delete repository')
    openDelete()
    expect([confirmInput().value, button('Delete permanently').disabled]).toEqual(['', true])
    await sleep(50)
    expect(byMethod(requests, 'DELETE')).toHaveLength(0)
  })

  it('Cancel clears a delete error', async () => {
    await openRepository({ [`DELETE ${ALPHA_URL}`]: { status: 500, data: {} } })
    confirmAndDelete()
    expect(await findAlert()).toBe(DELETE_FALLBACK)
    fireEvent.click(button('Cancel'))
    expect(alertText()).toBeNull()
    openDelete()
    expect(alertText()).toBeNull()
  })
})

// --- 9. Delete success ---------------------------------------------------------------------------

describe('Repository delete success', () => {
  it('sends exactly one DELETE to the repository URL, without a body', async () => {
    const { requests } = await openRepository()
    confirmAndDelete()
    await waitFor(() => expect(currentPath()).toBe(PROJECT_URL))
    expect(byMethod(requests, 'DELETE').map((r) => [r.url, r.data, r.baseURL])).toEqual([
      [ALPHA_URL, undefined, '/api/v1'],
    ])
  })

  it("lands on the repository's project page after the 204", async () => {
    await openRepository()
    confirmAndDelete()
    expect(await screen.findByRole('heading', { name: PROJECT.name })).toBeTruthy()
    expect(currentPath()).toBe(PROJECT_URL)
  })

  it('removes the repository from the cached list, shown at once while the list request is held', async () => {
    const { queryClient } = await openFromProject()
    const list = hold('GET', LIST_URL)
    confirmAndDelete()
    await waitFor(() => expect(currentPath()).toBe(PROJECT_URL))
    expect(await screen.findByRole('link', { name: /Beta/ })).toBeTruthy()
    expect(screen.queryByRole('link', { name: /Alpha/ })).toBeNull()
    expect(queryClient.getQueryData(queryKeys.repositories(P))).toEqual([BETA])
    await waitFor(() => expect(list.held).toEqual([`GET ${LIST_URL}`]))
  })

  it("removes every cached query of the repository and keeps its siblings' and the project's data", async () => {
    const { queryClient } = await openFromProject()
    queryClient.setQueryData(queryKeys.fileContent(P, ALPHA.id, 'src/app.py'), { path: 'src/app.py' })
    queryClient.setQueryData(queryKeys.repository(P, BETA.id), BETA)
    queryClient.setQueryData(queryKeys.tree(P, BETA.id), { tree: 'beta' })
    const before = queryClient.getQueryCache().findAll({ queryKey: queryKeys.repository(P, ALPHA.id) })
    expect(before.length).toBeGreaterThanOrEqual(8) // the repository, its six page queries, a file
    confirmAndDelete()
    await waitFor(() => expect(currentPath()).toBe(PROJECT_URL))
    expect(queryClient.getQueryCache().findAll({ queryKey: queryKeys.repository(P, ALPHA.id) })).toEqual([])
    for (const key of [
      queryKeys.repository(P, ALPHA.id),
      queryKeys.tree(P, ALPHA.id),
      queryKeys.languages(P, ALPHA.id),
      queryKeys.jobs(P, ALPHA.id),
      queryKeys.codeMetrics(P, ALPHA.id),
      queryKeys.dependencies(P, ALPHA.id),
      queryKeys.secretScan(P, ALPHA.id),
      queryKeys.fileContent(P, ALPHA.id, 'src/app.py'),
    ]) {
      expect(queryClient.getQueryData(key)).toBeUndefined()
    }
    expect(queryClient.getQueryData(queryKeys.repository(P, BETA.id))).toEqual(BETA)
    expect(queryClient.getQueryData(queryKeys.tree(P, BETA.id))).toEqual({ tree: 'beta' })
    expect(queryClient.getQueryData(queryKeys.project(P))).toEqual(PROJECT)
  })

  it('requests nothing for the deleted repository or any of its resources after the DELETE', async () => {
    const { requests } = await openFromProject()
    confirmAndDelete()
    await waitFor(() => expect(currentPath()).toBe(PROJECT_URL))
    await screen.findByRole('link', { name: /Beta/ })
    await sleep(100)
    const later = afterDelete(requests)
    expect(later.filter((request) => request.includes(ALPHA_URL))).toEqual([])
    expect(later.sort()).toEqual([`GET ${PROJECT_URL}`, `GET ${LIST_URL}`].sort())
  })

  it('stops the job polling of the deleted repository: no jobs request follows the DELETE', async () => {
    const { requests } = await openRepository({ [`GET ${ALPHA_URL}/jobs`]: { status: 200, data: [RUNNING] } })
    await screen.findByRole('region', { name: 'Ingestion progress' }) // a running job: the page polls
    confirmAndDelete()
    await waitFor(() => expect(currentPath()).toBe(PROJECT_URL))
    await sleep(2500) // longer than the 2-second polling interval
    expect(afterDelete(requests).filter((request) => request.includes(ALPHA_URL))).toEqual([])
  })

  it('navigates with replace: Back does not restore the deleted repository page', async () => {
    const { requests } = await openFromProject()
    confirmAndDelete()
    await waitFor(() => expect(currentPath()).toBe(PROJECT_URL))
    fireEvent.click(button('Test back'))
    await sleep(50)
    expect(currentPath()).toBe(PROJECT_URL)
    expect(screen.queryByRole('region', { name: 'Repository settings' })).toBeNull()
    expect(afterDelete(requests).filter((request) => request.includes(ALPHA_URL))).toEqual([])
  })

  it('deletes a repository opened directly and shows the server list without it', async () => {
    let deleted = false
    await openRepository({
      [`DELETE ${ALPHA_URL}`]: () => {
        deleted = true
        return { status: 204 }
      },
      [`GET ${LIST_URL}`]: () => ({ status: 200, data: deleted ? [BETA] : [ALPHA, BETA] }),
    })
    confirmAndDelete()
    expect(await screen.findByRole('link', { name: /Beta/ })).toBeTruthy()
    expect(screen.queryByRole('link', { name: /Alpha/ })).toBeNull()
  })
})

// --- 10. Delete errors ---------------------------------------------------------------------------

describe('Repository delete errors', () => {
  async function failDelete(response: MockRoute) {
    const view = await openFromProject({ [`DELETE ${ALPHA_URL}`]: response })
    confirmAndDelete()
    await findAlert()
    return view
  }

  it('shows the 404 detail and stays on the repository page with the confirmation open', async () => {
    await failDelete({ status: 404, data: { detail: NOT_FOUND } })
    expect(alertText()).toBe(NOT_FOUND)
    expect(currentPath()).toBe(ALPHA_URL)
    expect(deleteRegion()).not.toBeNull()
    expect([confirmInput().value, button('Delete permanently').disabled]).toEqual(['Alpha', false])
  })

  it('shows the delete fallback for a 500 and does not navigate', async () => {
    await failDelete({ status: 500, data: {} })
    expect(alertText()).toBe(DELETE_FALLBACK)
    await sleep(50)
    expect(currentPath()).toBe(ALPHA_URL)
    expect(heading().textContent).toBe('Alpha')
  })

  it('shows the network message and does not navigate', async () => {
    await failDelete(networkError)
    expect(alertText()).toBe(NETWORK)
    await sleep(50)
    expect(currentPath()).toBe(ALPHA_URL)
  })

  it('keeps the cached repository, its resources and the list after a failed delete', async () => {
    const { queryClient } = await failDelete({ status: 500, data: {} })
    expect(queryClient.getQueryData(queryKeys.repository(P, ALPHA.id))).toEqual(ALPHA)
    expect(queryClient.getQueryData(queryKeys.jobs(P, ALPHA.id))).toEqual([])
    expect(queryClient.getQueryData(queryKeys.repositories(P))).toEqual([ALPHA, BETA])
  })

  it('allows a retry after a failed delete', async () => {
    const { requests } = await failDelete(sequence({ status: 500, data: {} }, { status: 204 }))
    fireEvent.click(button('Delete permanently'))
    await waitFor(() => expect(currentPath()).toBe(PROJECT_URL))
    expect(byMethod(requests, 'DELETE')).toHaveLength(2)
  })
})

// --- 11. Isolation / security --------------------------------------------------------------------

describe('Repository settings isolation and security', () => {
  /** Beta is opened first so that switching back to it renders it at once from the cache. */
  async function openAlphaAfterBeta(extra: Record<string, MockRoute> = {}) {
    const view = await openRepository(extra, BETA_URL)
    fireEvent.click(button('Test open Alpha'))
    await waitFor(() => expect(heading().textContent).toBe('Alpha'))
    return view
  }

  async function switchToBeta() {
    fireEvent.click(button('Test open Beta'))
    await waitFor(() => expect(heading().textContent).toBe('Beta'))
  }

  it("does not carry Alpha's open edit form or typed name over to Beta", async () => {
    await openAlphaAfterBeta()
    openEdit()
    type(nameInput(), 'Typed for Alpha')
    await switchToBeta()
    expect(editRegion()).toBeNull()
    expect(button('Edit repository').textContent).toBe('Edit repository')
    openEdit()
    expect(nameInput().value).toBe('Beta')
  })

  it("does not carry Alpha's error over to Beta", async () => {
    await openAlphaAfterBeta({ [`PATCH ${ALPHA_URL}`]: { status: 500, data: {} } })
    openEdit()
    type(nameInput(), 'Typed for Alpha')
    fireEvent.click(button('Save changes'))
    expect(await findAlert()).toBe(UPDATE_FALLBACK)
    await switchToBeta()
    expect(alertText()).toBeNull()
    expect(editRegion()).toBeNull()
  })

  it("does not carry Alpha's delete confirmation over to Beta", async () => {
    await openAlphaAfterBeta()
    openDelete()
    type(confirmInput(), 'Alpha')
    await switchToBeta()
    expect(deleteRegion()).toBeNull()
    openDelete()
    expect([confirmInput().value, button('Delete permanently').disabled]).toEqual(['', true])
    expect(within(deleteRegion() as HTMLElement).getByText('Beta', { selector: 'strong' })).toBeTruthy()
  })

  it("does not carry Alpha's pending save over to Beta", async () => {
    await openAlphaAfterBeta()
    hold('PATCH', ALPHA_URL)
    openEdit()
    type(nameInput(), 'Typed for Alpha')
    fireEvent.click(button('Save changes'))
    await screen.findByRole('button', { name: 'Saving…' })
    await switchToBeta()
    expect(queryButton('Saving…')).toBeNull()
    openEdit()
    expect([button('Save changes').disabled, button('Cancel').disabled]).toEqual([false, false])
  })

  it('renders markup-like repository text literally', async () => {
    const markup: Repository = { ...ALPHA, name: '<img src=x onerror=alert(1)>' }
    const { container } = await openRepository({ [`GET ${ALPHA_URL}`]: { status: 200, data: markup } })
    expect(heading().textContent).toBe(markup.name)
    openDelete()
    expect(within(deleteRegion() as HTMLElement).getByText(markup.name, { selector: 'strong' })).toBeTruthy()
    fireEvent.click(button('Cancel'))
    openEdit()
    expect(nameInput().value).toBe(markup.name)
    expect(container.querySelector('img')).toBeNull()
  })

  it('uses no browser storage, cookies or direct fetch, only the app API', async () => {
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
      const { requests } = await openRepository()
      openEdit()
      type(nameInput(), 'Gamma')
      fireEvent.click(button('Save changes'))
      await waitFor(() => expect(heading().textContent).toBe('Gamma'))
      confirmAndDelete('Gamma')
      await waitFor(() => expect(currentPath()).toBe(PROJECT_URL))
      expect(new Set(getItem.mock.calls.map((call) => call[0]))).toEqual(new Set(['ai_se_access_token']))
      for (const spy of writes) {
        expect(spy).not.toHaveBeenCalled()
      }
      expect([localStorage.length, sessionStorage.length]).toEqual(stored)
      expect(cookieGet).not.toHaveBeenCalled()
      expect(cookieSet).not.toHaveBeenCalled()
      expect(fetchSpy).not.toHaveBeenCalled()
      expect(requests.every((r) => r.baseURL === '/api/v1')).toBe(true)
    } finally {
      globalThis.fetch = realFetch
      delete (document as { cookie?: unknown }).cookie
    }
  })

  it('keys the page caches by the route ids and finds list entries by the server id', async () => {
    // The route may spell the UUID in upper case; the server always answers in lower case.
    const cased: Repository = { ...ALPHA, id: 'abcdefab-cdef-4abc-8def-abcdefabcdef', name: 'Cased' }
    const upper = cased.id.toUpperCase()
    const url = `${LIST_URL}/${upper}`
    const { requests, queryClient } = await openRepository(
      {
        ...pageRoutes(url, cased),
        [`PATCH ${url}`]: echo(cased),
        [`DELETE ${url}`]: { status: 204 },
      },
      url,
    )
    queryClient.setQueryData(queryKeys.repositories(P), [cased, BETA])
    openEdit()
    type(nameInput(), 'Renamed')
    fireEvent.click(button('Save changes'))
    await waitFor(() => expect(heading().textContent).toBe('Renamed')) // the page's own key
    expect(queryClient.getQueryData(queryKeys.repositories(P))).toEqual([{ ...cased, name: 'Renamed' }, BETA])
    hold('GET', LIST_URL) // the project page's refetch must not overwrite the list under test
    confirmAndDelete('Renamed')
    await waitFor(() => expect(currentPath()).toBe(PROJECT_URL))
    expect(queryClient.getQueryCache().findAll({ queryKey: queryKeys.repository(P, upper) })).toEqual([])
    expect(queryClient.getQueryData(queryKeys.repositories(P))).toEqual([BETA])
    expect(requests.filter((r) => r.method !== 'GET').map((r) => `${r.method} ${r.url}`)).toEqual([
      `PATCH ${url}`,
      `DELETE ${url}`,
    ])
  })

  it('encodes the project and repository ids in the PATCH and DELETE URLs', async () => {
    const project = { ...PROJECT, id: 'odd project&1' }
    const odd: Repository = { ...ALPHA, id: 'odd repo&2', project_id: project.id, name: 'Odd' }
    const projectUrl = `/projects/${encodeURIComponent(project.id)}`
    const url = `${projectUrl}/repositories/${encodeURIComponent(odd.id)}`
    const { requests } = await openRepository(
      {
        [`GET ${projectUrl}`]: { status: 200, data: project },
        [`GET ${projectUrl}/repositories`]: { status: 200, data: [odd] },
        ...pageRoutes(url, odd),
        [`PATCH ${url}`]: echo(odd),
        [`DELETE ${url}`]: { status: 204 },
      },
      url,
    )
    openEdit()
    type(nameInput(), 'Odd renamed')
    fireEvent.click(button('Save changes'))
    await waitFor(() => expect(heading().textContent).toBe('Odd renamed'))
    confirmAndDelete('Odd renamed')
    await waitFor(() => expect(currentPath()).toBe(`/projects/${project.id}`))
    expect(requests.filter((r) => r.method !== 'GET').map((r) => `${r.method} ${r.url}`)).toEqual([
      `PATCH ${url}`,
      `DELETE ${url}`,
    ])
  })
})

// --- 12. Repository page integration -------------------------------------------------------------

describe('Repository page integration', () => {
  it('places the settings region in the page header, right after the Source line', async () => {
    await openRepository()
    const header = heading().parentElement?.parentElement as HTMLElement
    expect(Array.from(header.children).map((child) => child.tagName)).toEqual(['A', 'DIV', 'P', 'SECTION'])
    expect(header.children[2].textContent).toBe('Source: zip')
    expect(header.lastElementChild).toBe(settings())
  })

  it('keeps the GitHub source line and places the settings after it', async () => {
    await openRepository({}, BETA_URL)
    const header = heading().parentElement?.parentElement as HTMLElement
    expect(header.children[2].textContent).toBe(`Source: github${BETA.source_url}(main)`)
    expect(header.lastElementChild).toBe(settings())
  })

  it('keeps every existing region of the repository page', async () => {
    await openRepository()
    for (const name of ['Files', 'Languages', 'Search', 'Symbols', 'Code metrics', 'Dependencies', 'Secrets', 'File']) {
      expect(screen.getByRole('region', { name })).toBeTruthy()
    }
    expect(screen.getByRole('form', { name: 'Ingest repository' })).toBeTruthy()
    expect(await screen.findByText('No ingestion jobs yet.')).toBeTruthy()
  })

  it('keeps the back link to the project page working', async () => {
    await openRepository()
    fireEvent.click(screen.getByRole('link', { name: '← Project' }))
    expect(await screen.findByRole('heading', { name: PROJECT.name })).toBeTruthy()
    expect(currentPath()).toBe(PROJECT_URL)
  })
})
