import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { AxiosError, type AxiosAdapter, type InternalAxiosRequestConfig } from 'axios'
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { queryKeys } from '../hooks/queryKeys'
import { ProjectPage } from '../pages/ProjectPage'
import { ProjectsPage } from '../pages/ProjectsPage'
import { apiClient } from '../services/client'
import { mockApi, type MockResponse, PROJECT, type RecordedRequest, ZIP_REPOSITORY } from '../test/mockApi'
import { createTestQueryClient } from '../test/render'
import type { Project } from '../types/api'

type MockRoute = MockResponse | ((config: InternalAxiosRequestConfig) => MockResponse)

const ALPHA: Project = { ...PROJECT, name: 'Alpha Project', description: 'Test' }
const BETA: Project = {
  ...PROJECT,
  id: '55555555-5555-5555-5555-555555555555',
  name: 'Beta Project',
  description: null,
}
const A = ALPHA.id
const ALPHA_URL = `/projects/${A}`
const BETA_URL = `/projects/${BETA.id}`
const SERVER: Project = {
  ...ALPHA,
  name: 'Server Name',
  description: 'Server description',
  updated_at: '2026-10-06T10:00:00Z',
}

// The approved wording, written here independently of the component.
const REQUIRED = 'Project name is required'
const TOO_LONG = 'Project name must be at most 100 characters'
const UPDATE_FALLBACK = 'Could not update the project'
const DELETE_FALLBACK = 'Could not delete the project'
const NETWORK = 'Could not reach the server'
const NOT_FOUND = 'Project not found'
const warning = (name: string) =>
  `This permanently deletes ${name} with all of its repositories, files and analysis results. It cannot be undone.`

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

/** A PATCH answered like the server: the stored project with the sent fields applied. */
function echo(base: Project): MockRoute {
  return (config) => ({
    status: 200,
    data: { ...base, ...(JSON.parse(config.data as string) as Partial<Project>) },
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

function routes(extra: Record<string, MockRoute> = {}): Record<string, MockRoute> {
  return {
    'GET /projects': { status: 200, data: [ALPHA, BETA] },
    [`GET ${ALPHA_URL}`]: { status: 200, data: ALPHA },
    [`GET ${ALPHA_URL}/repositories`]: { status: 200, data: [ZIP_REPOSITORY] },
    [`GET ${BETA_URL}`]: { status: 200, data: BETA },
    [`GET ${BETA_URL}/repositories`]: { status: 200, data: [] },
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

/** Renders the real project pages at ``path``; the API must already be mocked. */
function mount(path: string) {
  const queryClient = createTestQueryClient()
  const view = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/projects" element={<ProjectsPage />} />
          <Route path="/projects/:projectId" element={<ProjectPage />} />
        </Routes>
        <Probe />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return { queryClient, container: view.container }
}

/** Opens a project page directly (the project list was never loaded). */
async function openProject(extra: Record<string, MockRoute> = {}, path = ALPHA_URL) {
  const requests = mockApi(routes(extra))
  const view = mount(path)
  await screen.findByRole('region', { name: 'Project settings' })
  return { requests, ...view }
}

/** Opens Alpha from the project list, so the list is cached as in normal use. */
async function openFromList(extra: Record<string, MockRoute> = {}) {
  const requests = mockApi(routes(extra))
  const view = mount('/projects')
  fireEvent.click(await screen.findByRole('link', { name: /Alpha Project/ }))
  await screen.findByRole('region', { name: 'Project settings' })
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

const settings = () => screen.getByRole('region', { name: 'Project settings' })
const editRegion = () => screen.queryByRole('region', { name: 'Edit project details' })
const deleteRegion = () => screen.queryByRole('region', { name: 'Confirm project deletion' })
const button = (name: string) => screen.getByRole('button', { name }) as HTMLButtonElement
const queryButton = (name: string) => screen.queryByRole('button', { name })
const nameInput = () => screen.getByLabelText('Project name') as HTMLInputElement
const descriptionInput = () =>
  screen.getByLabelText('Project description (optional)') as HTMLTextAreaElement
const confirmInput = () => screen.getByLabelText('Type the project name to confirm') as HTMLInputElement
const heading = () => screen.getByRole('heading', { level: 1 })
const currentPath = () => screen.getByTestId('location').textContent
const alertText = () => screen.queryByRole('alert')?.textContent ?? null

function type(input: HTMLInputElement | HTMLTextAreaElement, value: string) {
  fireEvent.change(input, { target: { value } })
}

function openEdit() {
  fireEvent.click(button('Edit project'))
  return editRegion() as HTMLElement
}

function openDelete() {
  fireEvent.click(button('Delete project'))
  return deleteRegion() as HTMLElement
}

function confirmAndDelete(name = ALPHA.name) {
  openDelete()
  type(confirmInput(), name)
  fireEvent.click(button('Delete permanently'))
}

const byMethod = (requests: RecordedRequest[], method: string) =>
  requests.filter((request) => request.method === method)
const sentBody = (request: RecordedRequest) => JSON.parse(request.data as string) as unknown

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

// --- 1. Idle / visibility ----------------------------------------------------------------------

describe('Project settings visibility', () => {
  it('shows the settings region with Edit project and Delete project once the project loads', async () => {
    await openProject()
    const region = settings()
    expect(within(region).getAllByRole('button').map((b) => [b.textContent, (b as HTMLButtonElement).disabled])).toEqual([
      ['Edit project', false],
      ['Delete project', false],
    ])
    expect([editRegion(), deleteRegion()]).toEqual([null, null])
  })

  it('sends no PATCH or DELETE on mount: only the page loads its data', async () => {
    const { requests } = await openProject()
    await sleep(50)
    expect(requests.map((r) => `${r.method} ${r.url}`).sort()).toEqual([
      `GET ${ALPHA_URL}`,
      `GET ${ALPHA_URL}/repositories`,
    ])
  })

  it('shows nothing while the project is still loading', async () => {
    mockApi(routes())
    hold('GET', ALPHA_URL)
    mount(ALPHA_URL)
    expect(await screen.findByText('Loading project…')).toBeTruthy()
    await sleep(50)
    expect(screen.queryByRole('region', { name: 'Project settings' })).toBeNull()
    expect([queryButton('Edit project'), queryButton('Delete project')]).toEqual([null, null])
  })

  it('shows nothing when the project cannot be loaded', async () => {
    mockApi(routes({ [`GET ${ALPHA_URL}`]: { status: 404, data: { detail: NOT_FOUND } } }))
    mount(ALPHA_URL)
    expect((await screen.findByRole('alert')).textContent).toBe(NOT_FOUND)
    expect(screen.queryByRole('region', { name: 'Project settings' })).toBeNull()
    expect(queryButton('Edit project')).toBeNull()
  })
})

// --- 2. Edit form --------------------------------------------------------------------------------

describe('Project edit form', () => {
  it('opens prefilled with the server name and description and hides the idle buttons', async () => {
    await openProject()
    const form = openEdit()
    expect([nameInput().value, descriptionInput().value]).toEqual(['Alpha Project', 'Test'])
    expect([queryButton('Edit project'), queryButton('Delete project')]).toEqual([null, null])
    expect(within(form).getAllByRole('button').map((b) => b.textContent)).toEqual(['Save changes', 'Cancel'])
    expect(settings().contains(form)).toBe(true)
  })

  it('prefills an empty description when the server description is null', async () => {
    await openProject({}, BETA_URL)
    openEdit()
    expect([nameInput().value, descriptionInput().value]).toEqual(['Beta Project', ''])
  })

  it('Cancel discards the edits without a request and restores the idle buttons', async () => {
    const { requests } = await openProject()
    openEdit()
    type(nameInput(), 'Changed name')
    type(descriptionInput(), 'Changed description')
    fireEvent.click(button('Cancel'))
    expect(editRegion()).toBeNull()
    expect([button('Edit project').textContent, button('Delete project').textContent]).toEqual([
      'Edit project',
      'Delete project',
    ])
    await sleep(50)
    expect(byMethod(requests, 'PATCH')).toHaveLength(0)
    expect(heading().textContent).toBe('Alpha Project')
  })

  it('reopening starts again from the server values', async () => {
    await openProject()
    openEdit()
    type(nameInput(), 'Changed name')
    type(descriptionInput(), '')
    fireEvent.click(button('Cancel'))
    openEdit()
    expect([nameInput().value, descriptionInput().value]).toEqual(['Alpha Project', 'Test'])
  })

  it('never shows the edit form and the delete confirmation together', async () => {
    await openProject()
    openEdit()
    expect(deleteRegion()).toBeNull()
    expect(queryButton('Delete project')).toBeNull()
    fireEvent.click(button('Cancel'))
    openDelete()
    expect(editRegion()).toBeNull()
    expect(queryButton('Edit project')).toBeNull()
  })

  it('compares with the server values the form opened with, not later cache changes', async () => {
    const { requests, queryClient } = await openProject()
    openEdit()
    queryClient.setQueryData(queryKeys.project(A), { ...ALPHA, name: 'Renamed elsewhere' })
    await waitFor(() => expect(heading().textContent).toBe('Renamed elsewhere'))
    expect(nameInput().value).toBe('Alpha Project')
    type(descriptionInput(), 'New description')
    expect(await saveAndGetBody(requests)).toEqual({ description: 'New description' })
  })
})

// --- 3. Validation -------------------------------------------------------------------------------

describe('Project edit validation', () => {
  it('rejects an empty name without a request', async () => {
    const { requests } = await openProject()
    openEdit()
    type(nameInput(), '')
    fireEvent.click(button('Save changes'))
    expect(alertText()).toBe(REQUIRED)
    await sleep(50)
    expect(byMethod(requests, 'PATCH')).toHaveLength(0)
    expect(editRegion()).not.toBeNull()
  })

  it('rejects a whitespace-only name without a request', async () => {
    const { requests } = await openProject()
    openEdit()
    type(nameInput(), '   \t ')
    fireEvent.click(button('Save changes'))
    expect(alertText()).toBe(REQUIRED)
    await sleep(50)
    expect(byMethod(requests, 'PATCH')).toHaveLength(0)
  })

  it('accepts a name of exactly 100 characters', async () => {
    const { requests } = await openProject()
    openEdit()
    type(nameInput(), 'n'.repeat(100))
    expect(await saveAndGetBody(requests)).toEqual({ name: 'n'.repeat(100) })
  })

  it('rejects a name of 101 characters without a request', async () => {
    const { requests } = await openProject()
    openEdit()
    type(nameInput(), 'n'.repeat(101))
    fireEvent.click(button('Save changes'))
    expect(alertText()).toBe(TOO_LONG)
    await sleep(50)
    expect(byMethod(requests, 'PATCH')).toHaveLength(0)
  })

  it('checks the length after trimming and sends the trimmed name', async () => {
    const { requests } = await openProject()
    openEdit()
    type(nameInput(), `  ${'m'.repeat(100)}  `)
    expect(await saveAndGetBody(requests)).toEqual({ name: 'm'.repeat(100) })
  })

  it('sends the name trimmed', async () => {
    const { requests } = await openProject()
    openEdit()
    type(nameInput(), '  Beta  ')
    expect(await saveAndGetBody(requests)).toEqual({ name: 'Beta' })
  })

  it('sends the description trimmed', async () => {
    const { requests } = await openProject()
    openEdit()
    type(descriptionInput(), '  New text \n')
    expect(await saveAndGetBody(requests)).toEqual({ description: 'New text' })
  })

  it('sends null for a whitespace-only description', async () => {
    const { requests } = await openProject()
    openEdit()
    type(descriptionInput(), '  \n ')
    expect(await saveAndGetBody(requests)).toEqual({ description: null })
  })

  it('clears the validation message as soon as a corrected name is submitted', async () => {
    const { requests } = await openProject()
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

describe('Project PATCH body', () => {
  it('sends only the name when only the name changed', async () => {
    const { requests } = await openProject()
    openEdit()
    type(nameInput(), 'Beta')
    expect(await saveAndGetBody(requests)).toEqual({ name: 'Beta' })
  })

  it('sends only the description when only the description changed', async () => {
    const { requests } = await openProject()
    openEdit()
    type(descriptionInput(), 'Other')
    expect(await saveAndGetBody(requests)).toEqual({ description: 'Other' })
  })

  it('sends description null when the description is cleared', async () => {
    const { requests } = await openProject()
    openEdit()
    type(descriptionInput(), '')
    expect(await saveAndGetBody(requests)).toEqual({ description: null })
  })

  it('sends both fields when both changed', async () => {
    const { requests } = await openProject()
    openEdit()
    type(nameInput(), 'Beta')
    type(descriptionInput(), 'Other')
    expect(await saveAndGetBody(requests)).toEqual({ name: 'Beta', description: 'Other' })
  })

  it('does not send an unchanged null description', async () => {
    const { requests } = await openProject({}, BETA_URL)
    openEdit()
    type(nameInput(), 'Gamma')
    expect(await saveAndGetBody(requests)).toEqual({ name: 'Gamma' })
  })

  it('sends no PATCH when nothing changed and closes the form', async () => {
    const { requests } = await openProject()
    openEdit()
    await saveExpectingNoRequest(requests)
    expect(button('Edit project').textContent).toBe('Edit project')
  })

  it('sends no PATCH when the values only differ before normalization', async () => {
    const { requests } = await openProject()
    openEdit()
    type(nameInput(), ' Alpha Project ')
    type(descriptionInput(), ' Test\n')
    await saveExpectingNoRequest(requests)
  })
})

// --- 5. Server-authoritative success -------------------------------------------------------------

describe('Project save success', () => {
  it('shows the server response, not the submitted values, and closes the form', async () => {
    await openProject({ [`PATCH ${ALPHA_URL}`]: { status: 200, data: SERVER } })
    openEdit()
    type(nameInput(), 'Typed name')
    type(descriptionInput(), 'Typed description')
    fireEvent.click(button('Save changes'))
    await waitFor(() => expect(heading().textContent).toBe('Server Name'))
    expect(screen.getByText('Server description')).toBeTruthy()
    expect(screen.queryByText('Typed name')).toBeNull()
    expect(editRegion()).toBeNull()
    expect(alertText()).toBeNull()
  })

  it('stores the server response as the cached project', async () => {
    const { queryClient } = await openProject({ [`PATCH ${ALPHA_URL}`]: { status: 200, data: SERVER } })
    openEdit()
    type(nameInput(), 'Typed name')
    fireEvent.click(button('Save changes'))
    await waitFor(() => expect(editRegion()).toBeNull())
    expect(queryClient.getQueryData(queryKeys.project(A))).toEqual(SERVER)
  })

  it('replaces only this project in the cached list with the server response', async () => {
    const { queryClient } = await openFromList({ [`PATCH ${ALPHA_URL}`]: { status: 200, data: SERVER } })
    openEdit()
    type(nameInput(), 'Typed name')
    fireEvent.click(button('Save changes'))
    await waitFor(() => expect(editRegion()).toBeNull())
    expect(queryClient.getQueryData(queryKeys.projects)).toEqual([SERVER, BETA])
  })

  it('shows the server name in the project list at once while the list request is held', async () => {
    await openFromList({ [`PATCH ${ALPHA_URL}`]: { status: 200, data: SERVER } })
    const list = hold('GET', '/projects')
    openEdit()
    type(nameInput(), 'Typed name')
    fireEvent.click(button('Save changes'))
    await waitFor(() => expect(heading().textContent).toBe('Server Name'))
    fireEvent.click(screen.getByRole('link', { name: '← Projects' }))
    expect(await screen.findByRole('link', { name: /Server Name/ })).toBeTruthy()
    expect(screen.queryByText('Alpha Project')).toBeNull()
    expect(screen.getByRole('link', { name: /Beta Project/ })).toBeTruthy()
    await waitFor(() => expect(list.held).toEqual(['GET /projects'])) // the refetch never answered
  })

  it('reopening after a save starts from the saved server values', async () => {
    await openProject({ [`PATCH ${ALPHA_URL}`]: { status: 200, data: SERVER } })
    openEdit()
    type(nameInput(), 'Typed name')
    fireEvent.click(button('Save changes'))
    await waitFor(() => expect(editRegion()).toBeNull())
    openEdit()
    expect([nameInput().value, descriptionInput().value]).toEqual(['Server Name', 'Server description'])
  })
})

// --- 6. Pending ----------------------------------------------------------------------------------

describe('Project actions while pending', () => {
  it('shows Saving… on a disabled button and blocks Cancel until the PATCH answers', async () => {
    await openProject()
    const patch = hold('PATCH', ALPHA_URL)
    openEdit()
    type(nameInput(), 'Beta')
    fireEvent.click(button('Save changes'))
    const saving = (await screen.findByRole('button', { name: 'Saving…' })) as HTMLButtonElement
    expect([saving.disabled, button('Cancel').disabled]).toEqual([true, true])
    expect(queryButton('Save changes')).toBeNull()
    patch.release()
    await waitFor(() => expect(heading().textContent).toBe('Beta'))
  })

  it('sends one PATCH for a double submit', async () => {
    const { requests } = await openProject()
    const patch = hold('PATCH', ALPHA_URL)
    openEdit()
    type(nameInput(), 'Beta')
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
    await openProject()
    const remove = hold('DELETE', ALPHA_URL)
    confirmAndDelete()
    const deleting = (await screen.findByRole('button', { name: 'Deleting…' })) as HTMLButtonElement
    expect([deleting.disabled, button('Cancel').disabled]).toEqual([true, true])
    expect(queryButton('Delete permanently')).toBeNull()
    remove.release()
    await waitFor(() => expect(currentPath()).toBe('/projects'))
  })

  it('sends one DELETE for a double submit', async () => {
    const { requests } = await openProject()
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
    await waitFor(() => expect(currentPath()).toBe('/projects'))
    expect(byMethod(requests, 'DELETE')).toHaveLength(1)
  })
})

// --- 7. Edit errors ------------------------------------------------------------------------------

describe('Project save errors', () => {
  async function failSave(response: MockRoute) {
    const view = await openFromList({ [`PATCH ${ALPHA_URL}`]: response })
    openEdit()
    type(nameInput(), 'Beta')
    type(descriptionInput(), 'Typed description')
    fireEvent.click(button('Save changes'))
    await screen.findByRole('alert')
    return view
  }

  it('joins the messages of a 422 validation error', async () => {
    await failSave({
      status: 422,
      data: {
        detail: [{ msg: 'Value error, must not be empty' }, { msg: 'Value error, must be at most 100 characters' }],
      },
    })
    expect(alertText()).toBe('Value error, must not be empty; Value error, must be at most 100 characters')
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

  it('keeps the form open with the typed input, on the project page', async () => {
    await failSave({ status: 500, data: {} })
    expect(editRegion()).not.toBeNull()
    expect([nameInput().value, descriptionInput().value]).toEqual(['Beta', 'Typed description'])
    expect([button('Save changes').disabled, button('Cancel').disabled]).toEqual([false, false])
    expect(currentPath()).toBe(ALPHA_URL)
  })

  it('leaves the cached project and list unchanged', async () => {
    const { queryClient } = await failSave({ status: 404, data: { detail: NOT_FOUND } })
    expect(queryClient.getQueryData(queryKeys.project(A))).toEqual(ALPHA)
    expect(queryClient.getQueryData(queryKeys.projects)).toEqual([ALPHA, BETA])
    expect(heading().textContent).toBe('Alpha Project')
  })

  it('clears the error after a successful retry', async () => {
    await failSave(sequence({ status: 500, data: {} }, echo(ALPHA)))
    expect(alertText()).toBe(UPDATE_FALLBACK)
    fireEvent.click(button('Save changes'))
    await waitFor(() => expect(heading().textContent).toBe('Beta'))
    expect(alertText()).toBeNull()
    expect(editRegion()).toBeNull()
  })

  it('Cancel clears the error and reopening shows none', async () => {
    await failSave({ status: 500, data: {} })
    fireEvent.click(button('Cancel'))
    expect(alertText()).toBeNull()
    openEdit()
    expect(alertText()).toBeNull()
    expect([nameInput().value, descriptionInput().value]).toEqual(['Alpha Project', 'Test'])
  })
})

// --- 8. Delete confirmation ----------------------------------------------------------------------

describe('Project delete confirmation', () => {
  it('shows a destructive warning naming the project as text', async () => {
    await openProject()
    const region = openDelete()
    expect(within(region).getByText(ALPHA.name, { selector: 'strong' })).toBeTruthy()
    expect(region.querySelector('p')?.textContent).toBe(warning(ALPHA.name))
    expect(within(region).getAllByRole('button').map((b) => b.textContent)).toEqual([
      'Delete permanently',
      'Cancel',
    ])
  })

  it('keeps Delete permanently disabled while empty and enables it for the exact name', async () => {
    await openProject()
    openDelete()
    expect([confirmInput().value, button('Delete permanently').disabled]).toEqual(['', true])
    type(confirmInput(), 'Alpha Project')
    expect(button('Delete permanently').disabled).toBe(false)
  })

  it.each([
    ['a case mismatch', 'alpha project'],
    ['leading whitespace', ' Alpha Project'],
    ['trailing whitespace', 'Alpha Project '],
    ['a modified name', 'Alpha Project!'],
    ['a partial name', 'Alpha'],
  ])('keeps Delete permanently disabled for %s', async (_case, typed) => {
    await openProject()
    openDelete()
    type(confirmInput(), typed)
    expect(button('Delete permanently').disabled).toBe(true)
  })

  it('never sends DELETE without the exact name, even when the form is submitted directly', async () => {
    const { requests } = await openProject()
    openDelete()
    const form = confirmInput().closest('form') as HTMLFormElement
    fireEvent.submit(form)
    type(confirmInput(), 'Alpha Project ')
    fireEvent.submit(form)
    await sleep(50)
    expect(byMethod(requests, 'DELETE')).toHaveLength(0)
    expect(currentPath()).toBe(ALPHA_URL)
  })

  it('Cancel closes the confirmation and reopening starts empty', async () => {
    const { requests } = await openProject()
    openDelete()
    type(confirmInput(), 'Alpha Project')
    fireEvent.click(button('Cancel'))
    expect(deleteRegion()).toBeNull()
    expect(button('Delete project').textContent).toBe('Delete project')
    openDelete()
    expect([confirmInput().value, button('Delete permanently').disabled]).toEqual(['', true])
    await sleep(50)
    expect(byMethod(requests, 'DELETE')).toHaveLength(0)
  })

  it('Cancel clears a delete error', async () => {
    await openProject({ [`DELETE ${ALPHA_URL}`]: { status: 500, data: {} } })
    confirmAndDelete()
    expect((await screen.findByRole('alert')).textContent).toBe(DELETE_FALLBACK)
    fireEvent.click(button('Cancel'))
    expect(alertText()).toBeNull()
    openDelete()
    expect(alertText()).toBeNull()
  })
})

// --- 9. Delete success ---------------------------------------------------------------------------

describe('Project delete success', () => {
  it('sends exactly one DELETE to the project URL, without a body', async () => {
    const { requests } = await openProject()
    confirmAndDelete()
    await waitFor(() => expect(currentPath()).toBe('/projects'))
    const deletes = byMethod(requests, 'DELETE')
    expect(deletes.map((r) => [r.url, r.data, r.baseURL])).toEqual([[ALPHA_URL, undefined, '/api/v1']])
  })

  it('lands on the project list after the 204', async () => {
    await openProject()
    confirmAndDelete()
    expect(await screen.findByRole('heading', { name: 'Projects' })).toBeTruthy()
    expect(currentPath()).toBe('/projects')
  })

  it('removes the project from the cached list, shown at once while the list request is held', async () => {
    const { queryClient } = await openFromList()
    const list = hold('GET', '/projects')
    confirmAndDelete()
    await waitFor(() => expect(currentPath()).toBe('/projects'))
    expect(await screen.findByRole('link', { name: /Beta Project/ })).toBeTruthy()
    expect(screen.queryByText('Alpha Project')).toBeNull()
    expect(queryClient.getQueryData(queryKeys.projects)).toEqual([BETA])
    await waitFor(() => expect(list.held).toEqual(['GET /projects']))
  })

  it("removes every cached query of the project and keeps other projects' data", async () => {
    const { queryClient } = await openFromList()
    const repositoryId = ZIP_REPOSITORY.id
    queryClient.setQueryData(queryKeys.tree(A, repositoryId), { tree: [], file_count: 0, total_bytes: 0 })
    queryClient.setQueryData(queryKeys.project(BETA.id), BETA)
    queryClient.setQueryData(queryKeys.repositories(BETA.id), [])
    confirmAndDelete()
    await waitFor(() => expect(currentPath()).toBe('/projects'))
    expect(queryClient.getQueryCache().findAll({ queryKey: queryKeys.project(A) })).toEqual([])
    expect(queryClient.getQueryData(queryKeys.repositories(A))).toBeUndefined()
    expect(queryClient.getQueryData(queryKeys.tree(A, repositoryId))).toBeUndefined()
    expect(queryClient.getQueryData(queryKeys.project(BETA.id))).toEqual(BETA)
    expect(queryClient.getQueryData(queryKeys.repositories(BETA.id))).toEqual([])
  })

  it('requests nothing for the deleted project or its repositories after the DELETE', async () => {
    const { requests } = await openFromList()
    confirmAndDelete()
    await waitFor(() => expect(currentPath()).toBe('/projects'))
    await screen.findByRole('link', { name: /Beta Project/ })
    await sleep(100)
    const later = afterDelete(requests)
    expect(later.filter((request) => request.includes(ALPHA_URL))).toEqual([])
    expect(later).toEqual(['GET /projects'])
  })

  it('navigates with replace: Back does not return to the deleted project', async () => {
    const { requests } = await openFromList()
    confirmAndDelete()
    await waitFor(() => expect(currentPath()).toBe('/projects'))
    fireEvent.click(button('Test back'))
    await sleep(50)
    expect(currentPath()).toBe('/projects')
    expect(screen.queryByRole('region', { name: 'Project settings' })).toBeNull()
    expect(afterDelete(requests).filter((request) => request.includes(ALPHA_URL))).toEqual([])
  })

  it('deletes a project opened directly and shows the server list without it', async () => {
    let deleted = false
    await openProject({
      [`DELETE ${ALPHA_URL}`]: () => {
        deleted = true
        return { status: 204 }
      },
      'GET /projects': () => ({ status: 200, data: deleted ? [BETA] : [ALPHA, BETA] }),
    })
    confirmAndDelete()
    expect(await screen.findByRole('link', { name: /Beta Project/ })).toBeTruthy()
    expect(screen.queryByText('Alpha Project')).toBeNull()
  })
})

// --- 10. Delete errors ---------------------------------------------------------------------------

describe('Project delete errors', () => {
  async function failDelete(response: MockRoute) {
    const view = await openFromList({ [`DELETE ${ALPHA_URL}`]: response })
    confirmAndDelete()
    await screen.findByRole('alert')
    return view
  }

  it('shows the 404 detail and stays on the project page with the confirmation open', async () => {
    await failDelete({ status: 404, data: { detail: NOT_FOUND } })
    expect(alertText()).toBe(NOT_FOUND)
    expect(currentPath()).toBe(ALPHA_URL)
    expect(deleteRegion()).not.toBeNull()
    expect([confirmInput().value, button('Delete permanently').disabled]).toEqual(['Alpha Project', false])
  })

  it('shows the delete fallback for a 500 and does not navigate', async () => {
    await failDelete({ status: 500, data: {} })
    expect(alertText()).toBe(DELETE_FALLBACK)
    await sleep(50)
    expect(currentPath()).toBe(ALPHA_URL)
    expect(heading().textContent).toBe('Alpha Project')
  })

  it('shows the network message and does not navigate', async () => {
    await failDelete(networkError)
    expect(alertText()).toBe(NETWORK)
    await sleep(50)
    expect(currentPath()).toBe(ALPHA_URL)
  })

  it('keeps the cached project and list after a failed delete', async () => {
    const { queryClient } = await failDelete({ status: 500, data: {} })
    expect(queryClient.getQueryData(queryKeys.project(A))).toEqual(ALPHA)
    expect(queryClient.getQueryData(queryKeys.repositories(A))).toEqual([ZIP_REPOSITORY])
    expect(queryClient.getQueryData(queryKeys.projects)).toEqual([ALPHA, BETA])
  })

  it('allows a retry after a failed delete', async () => {
    const { requests } = await failDelete(sequence({ status: 500, data: {} }, { status: 204 }))
    fireEvent.click(button('Delete permanently'))
    await waitFor(() => expect(currentPath()).toBe('/projects'))
    expect(byMethod(requests, 'DELETE')).toHaveLength(2)
  })
})

// --- 11. Isolation / security --------------------------------------------------------------------

describe('Project settings isolation and security', () => {
  /** Beta is opened first so that switching back to it renders it at once from the cache. */
  async function openAlphaAfterBeta(extra: Record<string, MockRoute> = {}) {
    const view = await openProject(extra, BETA_URL)
    fireEvent.click(button('Test open Alpha'))
    await waitFor(() => expect(heading().textContent).toBe('Alpha Project'))
    return view
  }

  async function switchToBeta() {
    fireEvent.click(button('Test open Beta'))
    await waitFor(() => expect(heading().textContent).toBe('Beta Project'))
  }

  it("does not carry Alpha's open edit form or input over to Beta", async () => {
    await openAlphaAfterBeta()
    openEdit()
    type(nameInput(), 'Typed for Alpha')
    await switchToBeta()
    expect(editRegion()).toBeNull()
    expect(button('Edit project').textContent).toBe('Edit project')
    openEdit()
    expect([nameInput().value, descriptionInput().value]).toEqual(['Beta Project', ''])
  })

  it("does not carry Alpha's error over to Beta", async () => {
    await openAlphaAfterBeta({ [`PATCH ${ALPHA_URL}`]: { status: 500, data: {} } })
    openEdit()
    type(nameInput(), 'Typed for Alpha')
    fireEvent.click(button('Save changes'))
    expect((await screen.findByRole('alert')).textContent).toBe(UPDATE_FALLBACK)
    await switchToBeta()
    expect(alertText()).toBeNull()
    expect(editRegion()).toBeNull()
  })

  it("does not carry Alpha's delete confirmation over to Beta", async () => {
    await openAlphaAfterBeta()
    openDelete()
    type(confirmInput(), 'Alpha Project')
    await switchToBeta()
    expect(deleteRegion()).toBeNull()
    openDelete()
    expect([confirmInput().value, button('Delete permanently').disabled]).toEqual(['', true])
    expect(within(deleteRegion() as HTMLElement).getByText('Beta Project', { selector: 'strong' })).toBeTruthy()
  })

  it('renders markup-like project text literally', async () => {
    const markup: Project = { ...ALPHA, name: '<img src=x onerror=alert(1)>', description: '<b>bold</b>' }
    const { container } = await openProject({ [`GET ${ALPHA_URL}`]: { status: 200, data: markup } })
    expect(heading().textContent).toBe(markup.name)
    expect(screen.getByText(markup.description as string)).toBeTruthy()
    openDelete()
    expect(within(deleteRegion() as HTMLElement).getByText(markup.name, { selector: 'strong' })).toBeTruthy()
    fireEvent.click(button('Cancel'))
    openEdit()
    expect([nameInput().value, descriptionInput().value]).toEqual([markup.name, markup.description])
    expect(container.querySelector('img, b')).toBeNull()
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
      const { requests } = await openProject()
      openEdit()
      type(nameInput(), 'Beta')
      fireEvent.click(button('Save changes'))
      await waitFor(() => expect(heading().textContent).toBe('Beta'))
      confirmAndDelete('Beta')
      await waitFor(() => expect(currentPath()).toBe('/projects'))
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

  it('encodes the project id in the PATCH and DELETE URLs', async () => {
    const odd: Project = { ...ALPHA, id: 'odd id?&#', name: 'Odd' }
    const encoded = `/projects/${encodeURIComponent(odd.id)}`
    const { requests } = await openProject(
      {
        [`GET ${encoded}`]: { status: 200, data: odd },
        [`GET ${encoded}/repositories`]: { status: 200, data: [] },
        [`PATCH ${encoded}`]: echo(odd),
        [`DELETE ${encoded}`]: { status: 204 },
      },
      encoded,
    )
    openEdit()
    type(nameInput(), 'Odd renamed')
    fireEvent.click(button('Save changes'))
    await waitFor(() => expect(heading().textContent).toBe('Odd renamed'))
    confirmAndDelete('Odd renamed')
    await waitFor(() => expect(currentPath()).toBe('/projects'))
    expect(
      requests.filter((r) => r.method !== 'GET').map((r) => `${r.method} ${r.url}`),
    ).toEqual([`PATCH ${encoded}`, `DELETE ${encoded}`])
  })
})

// --- 12. Project page integration ----------------------------------------------------------------

describe('Project page integration', () => {
  it('still renders the repository list and the create-repository form', async () => {
    await openProject()
    const link = screen.getByRole('link', { name: /backend/ })
    expect(link.getAttribute('href')).toBe(`${ALPHA_URL}/repositories/${ZIP_REPOSITORY.id}`)
    expect(screen.getByRole('heading', { name: 'Repositories' })).toBeTruthy()
    expect(screen.getByRole('form', { name: 'Create repository' })).toBeTruthy()
  })

  it('places the settings region in the page header, after the description', async () => {
    await openProject()
    const header = heading().parentElement as HTMLElement
    expect(Array.from(header.children).map((child) => child.tagName)).toEqual(['A', 'H1', 'P', 'SECTION'])
    expect(header.lastElementChild).toBe(settings())
    const description = screen.getByText('Test')
    expect(description.compareDocumentPosition(settings()) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('places the settings region right after the name when there is no description', async () => {
    await openProject({}, BETA_URL)
    const header = heading().parentElement as HTMLElement
    expect(Array.from(header.children).map((child) => child.tagName)).toEqual(['A', 'H1', 'SECTION'])
  })

  it('keeps the back link to the project list working', async () => {
    await openProject()
    fireEvent.click(screen.getByRole('link', { name: '← Projects' }))
    expect(await screen.findByRole('heading', { name: 'Projects' })).toBeTruthy()
    expect(currentPath()).toBe('/projects')
  })
})
