import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { GITHUB_REPOSITORY, mockApi, ZIP_REPOSITORY } from '../test/mockApi'
import { createTestQueryClient } from '../test/render'
import type { Repository } from '../types/api'
import { IngestPanel } from './IngestPanel'

function ingestKey(repository: Repository): string {
  return `POST /projects/${repository.project_id}/repositories/${repository.id}/ingest`
}

const ACCEPTED = { status: 202, data: { job_id: 'job-1', repository_id: 'r', status: 'pending' } }

function renderPanel(repository: Repository) {
  render(
    <QueryClientProvider client={createTestQueryClient()}>
      <IngestPanel repository={repository} />
    </QueryClientProvider>,
  )
}

describe('IngestPanel (ZIP)', () => {
  it('uploads the chosen archive as multipart form data', async () => {
    const requests = mockApi({ [ingestKey(ZIP_REPOSITORY)]: ACCEPTED })
    renderPanel(ZIP_REPOSITORY)
    const file = new File(['PK\u0003\u0004'], 'source.zip', { type: 'application/zip' })
    fireEvent.change(screen.getByLabelText('ZIP archive'), { target: { files: [file] } })
    fireEvent.click(screen.getByRole('button', { name: 'Upload and ingest' }))

    expect((await screen.findByRole('status')).textContent).toBe('Ingestion queued.')
    const ingest = requests.find((r) => r.url.endsWith('/ingest'))
    expect(ingest?.data).toBeInstanceOf(FormData)
    expect(((ingest?.data as FormData).get('file') as File).name).toBe('source.zip')
  })

  it('requires a file before uploading', async () => {
    const requests = mockApi({})
    renderPanel(ZIP_REPOSITORY)
    fireEvent.click(screen.getByRole('button', { name: 'Upload and ingest' }))
    expect((await screen.findByRole('alert')).textContent).toBe('Choose a ZIP archive to upload')
    expect(requests).toHaveLength(0)
  })

  it.each([
    [409, 'Repository ingestion is already in progress'],
    [413, 'Upload exceeds the size limit'],
    [422, "Expected a single ZIP file in the 'file' field"],
    [503, 'Ingestion could not be queued; try again later'],
  ])('shows the API detail for %i', async (status, detail) => {
    mockApi({ [ingestKey(ZIP_REPOSITORY)]: { status, data: { detail } } })
    renderPanel(ZIP_REPOSITORY)
    const file = new File(['PK'], 'source.zip', { type: 'application/zip' })
    fireEvent.change(screen.getByLabelText('ZIP archive'), { target: { files: [file] } })
    fireEvent.click(screen.getByRole('button', { name: 'Upload and ingest' }))
    expect((await screen.findByRole('alert')).textContent).toBe(detail)
  })
})

describe('IngestPanel (GitHub)', () => {
  it('queues GitHub ingestion without a body and has no file input', async () => {
    const requests = mockApi({ [ingestKey(GITHUB_REPOSITORY)]: ACCEPTED })
    renderPanel(GITHUB_REPOSITORY)
    expect(screen.queryByLabelText('ZIP archive')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Ingest from GitHub' }))
    expect((await screen.findByRole('status')).textContent).toBe('Ingestion queued.')
    const ingest = requests.find((r) => r.url.endsWith('/ingest'))
    expect(ingest?.method).toBe('POST')
    expect(ingest?.data).toBeUndefined()
  })

  it('shows the API detail on conflict', async () => {
    mockApi({
      [ingestKey(GITHUB_REPOSITORY)]: {
        status: 422,
        data: { detail: 'Invalid GitHub repository URL' },
      },
    })
    renderPanel(GITHUB_REPOSITORY)
    fireEvent.click(screen.getByRole('button', { name: 'Ingest from GitHub' }))
    expect((await screen.findByRole('alert')).textContent).toBe('Invalid GitHub repository URL')
  })
})
