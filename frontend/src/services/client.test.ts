import { AxiosError, AxiosHeaders, type InternalAxiosRequestConfig } from 'axios'
import { describe, expect, it, vi } from 'vitest'

import { mockApi } from '../test/mockApi'
import { API_BASE_PATH, apiClient, attachToken, getErrorMessage } from './client'
import { listProjects } from './projects'
import { ingestGithub, ingestZip } from './repositories'
import { getToken, setToken, setUnauthorizedHandler } from './session'

function config(overrides: Partial<InternalAxiosRequestConfig>): InternalAxiosRequestConfig {
  return { headers: new AxiosHeaders(), baseURL: API_BASE_PATH, url: '/projects', ...overrides }
}

describe('API client', () => {
  it('uses the relative /api/v1 base path (no backend host)', () => {
    expect(API_BASE_PATH).toBe('/api/v1')
    expect(apiClient.defaults.baseURL).toBe('/api/v1')
  })

  it('attaches the Bearer token to /api/v1 requests', async () => {
    setToken('token-abc')
    const requests = mockApi({ 'GET /projects': { status: 200, data: [] } })
    await listProjects()
    expect(requests).toHaveLength(1)
    expect(requests[0].baseURL).toBe('/api/v1')
    expect(requests[0].url).toBe('/projects')
    expect(requests[0].headers.get('Authorization')).toBe('Bearer token-abc')
    expect(requests[0].url).not.toContain('token-abc') // never in the URL
  })

  it('sends no Authorization header without a token', async () => {
    const requests = mockApi({ 'GET /projects': { status: 200, data: [] } })
    await listProjects()
    expect(requests[0].headers.has('Authorization')).toBe(false)
  })

  it('never attaches the token to absolute URLs or other base paths', () => {
    setToken('token-abc')
    for (const url of ['https://evil.example/x', '//evil.example/x', 'http://localhost:8000/x']) {
      expect(attachToken(config({ url })).headers.has('Authorization')).toBe(false)
    }
    expect(attachToken(config({ baseURL: '/other' })).headers.has('Authorization')).toBe(false)
    expect(attachToken(config({})).headers.get('Authorization')).toBe('Bearer token-abc')
  })

  it('clears the session and notifies on 401', async () => {
    setToken('expired-token')
    const onUnauthorized = vi.fn()
    setUnauthorizedHandler(onUnauthorized)
    mockApi({ 'GET /projects': { status: 401, data: { detail: 'Not authenticated' } } })
    await expect(listProjects()).rejects.toBeInstanceOf(AxiosError)
    expect(getToken()).toBeNull()
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
  })

  it('does not clear the session on other errors', async () => {
    setToken('valid-token')
    mockApi({ 'GET /projects': { status: 500, data: { detail: 'boom' } } })
    await expect(listProjects()).rejects.toBeInstanceOf(AxiosError)
    expect(getToken()).toBe('valid-token')
  })
})

describe('getErrorMessage', () => {
  function apiError(status: number, data: unknown): AxiosError {
    const cfg = config({})
    return new AxiosError('failed', 'ERR', cfg, {}, {
      status,
      data,
      statusText: '',
      headers: {},
      config: cfg,
    })
  }

  it('returns a string detail', () => {
    expect(getErrorMessage(apiError(409, { detail: 'Already exists' }))).toBe('Already exists')
  })

  it('joins validation messages and never includes submitted input', () => {
    const error = apiError(422, {
      detail: [
        { msg: 'String should have at least 8 characters', input: 'my-secret-pw' },
        { msg: 'Field required', input: null },
      ],
    })
    const message = getErrorMessage(error)
    expect(message).toBe('String should have at least 8 characters; Field required')
    expect(message).not.toContain('my-secret-pw')
  })

  it('handles network errors and unknown errors', () => {
    expect(getErrorMessage(new AxiosError('Network Error', 'ERR_NETWORK'))).toBe(
      'Could not reach the server',
    )
    expect(getErrorMessage(new Error('x'), 'Fallback')).toBe('Fallback')
    expect(getErrorMessage(apiError(500, '<html>'), 'Fallback')).toBe('Fallback')
  })
})

describe('ingestion requests', () => {
  const projectId = 'p-1'
  const repositoryId = 'r-1'
  const ingestKey = `POST /projects/${projectId}/repositories/${repositoryId}/ingest`
  const accepted = { status: 202, data: { job_id: 'j-1', repository_id: repositoryId, status: 'pending' } }

  it('uploads a ZIP as multipart form data with upload progress', async () => {
    const requests = mockApi({
      [ingestKey]: (cfg) => {
        cfg.onUploadProgress?.({ loaded: 50, total: 100, bytes: 50, lengthComputable: true })
        return accepted
      },
    })
    const file = new File(['PK'], 'repo.zip', { type: 'application/zip' })
    const progress = vi.fn()
    const job = await ingestZip(projectId, repositoryId, file, progress)
    expect(job.job_id).toBe('j-1')
    expect(requests[0].data).toBeInstanceOf(FormData)
    const sent = (requests[0].data as FormData).get('file') as File
    expect(sent.name).toBe('repo.zip')
    expect(progress).toHaveBeenCalledWith(50)
  })

  it('queues GitHub ingestion without a request body', async () => {
    const requests = mockApi({ [ingestKey]: accepted })
    await ingestGithub(projectId, repositoryId)
    expect(requests[0].method).toBe('POST')
    expect(requests[0].data).toBeUndefined()
  })
})
