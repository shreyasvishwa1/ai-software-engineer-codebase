import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactElement } from 'react'
import { describe, expect, it } from 'vitest'

import { mockApi, PROJECT, ZIP_REPOSITORY } from '../test/mockApi'
import { createTestQueryClient } from '../test/render'
import { CreateProjectForm } from './CreateProjectForm'
import { CreateRepositoryForm } from './CreateRepositoryForm'

function renderWithClient(ui: ReactElement) {
  render(<QueryClientProvider client={createTestQueryClient()}>{ui}</QueryClientProvider>)
}

function fill(label: RegExp, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } })
}

const REPOSITORIES_KEY = `POST /projects/${PROJECT.id}/repositories`

describe('CreateProjectForm', () => {
  it('submits a trimmed name and optional description', async () => {
    const requests = mockApi({ 'POST /projects': { status: 201, data: PROJECT } })
    renderWithClient(<CreateProjectForm />)
    fill(/^name$/i, '  Demo project  ')
    fireEvent.click(screen.getByRole('button', { name: 'Create project' }))
    await waitFor(() => expect(requests).toHaveLength(1))
    expect(JSON.parse(requests[0].data as string)).toEqual({ name: 'Demo project', description: null })
    await waitFor(() => expect((screen.getByLabelText(/^name$/i) as HTMLInputElement).value).toBe(''))
  })

  it('validates the name before calling the API', async () => {
    const requests = mockApi({})
    renderWithClient(<CreateProjectForm />)
    fill(/^name$/i, '   ')
    fireEvent.click(screen.getByRole('button', { name: 'Create project' }))
    expect((await screen.findByRole('alert')).textContent).toBe('Project name is required')
    fill(/^name$/i, 'x'.repeat(101))
    fireEvent.click(screen.getByRole('button', { name: 'Create project' }))
    expect((await screen.findByRole('alert')).textContent).toContain('at most 100 characters')
    expect(requests).toHaveLength(0)
  })

  it('shows server validation messages', async () => {
    mockApi({
      'POST /projects': {
        status: 422,
        data: { detail: [{ msg: 'Value error, must not be empty', input: '' }] },
      },
    })
    renderWithClient(<CreateProjectForm />)
    fill(/^name$/i, 'Valid')
    fireEvent.click(screen.getByRole('button', { name: 'Create project' }))
    expect((await screen.findByRole('alert')).textContent).toBe('Value error, must not be empty')
  })
})

describe('CreateRepositoryForm', () => {
  it('creates a ZIP repository without source fields', async () => {
    const requests = mockApi({ [REPOSITORIES_KEY]: { status: 201, data: ZIP_REPOSITORY } })
    renderWithClient(<CreateRepositoryForm projectId={PROJECT.id} />)
    fill(/^name$/i, 'backend')
    fireEvent.click(screen.getByRole('button', { name: 'Create repository' }))
    await waitFor(() => expect(requests).toHaveLength(1))
    expect(JSON.parse(requests[0].data as string)).toEqual({ name: 'backend', source_type: 'zip' })
  })

  it('creates a GitHub repository with URL and optional branch', async () => {
    const requests = mockApi({ [REPOSITORIES_KEY]: { status: 201, data: ZIP_REPOSITORY } })
    renderWithClient(<CreateRepositoryForm projectId={PROJECT.id} />)
    fill(/^name$/i, 'demo')
    fireEvent.change(screen.getByLabelText(/source/i), { target: { value: 'github' } })
    fill(/github url/i, ' https://github.com/octo/demo ')
    fill(/branch/i, 'main')
    fireEvent.click(screen.getByRole('button', { name: 'Create repository' }))
    await waitFor(() => expect(requests).toHaveLength(1))
    expect(JSON.parse(requests[0].data as string)).toEqual({
      name: 'demo',
      source_type: 'github',
      source_url: 'https://github.com/octo/demo',
      default_branch: 'main',
    })
  })

  it('requires a GitHub URL for GitHub repositories', async () => {
    const requests = mockApi({})
    renderWithClient(<CreateRepositoryForm projectId={PROJECT.id} />)
    fill(/^name$/i, 'demo')
    fireEvent.change(screen.getByLabelText(/source/i), { target: { value: 'github' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create repository' }))
    expect((await screen.findByRole('alert')).textContent).toBe('GitHub repository URL is required')
    expect(requests).toHaveLength(0)
  })

  it('shows the API detail on failure', async () => {
    mockApi({ [REPOSITORIES_KEY]: { status: 404, data: { detail: 'Project not found' } } })
    renderWithClient(<CreateRepositoryForm projectId={PROJECT.id} />)
    fill(/^name$/i, 'backend')
    fireEvent.click(screen.getByRole('button', { name: 'Create repository' }))
    expect((await screen.findByRole('alert')).textContent).toBe('Project not found')
  })
})
