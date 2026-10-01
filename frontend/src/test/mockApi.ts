import {
  AxiosError,
  AxiosHeaders,
  type AxiosAdapter,
  type AxiosResponse,
  type InternalAxiosRequestConfig,
} from 'axios'

import { apiClient } from '../services/client'

/**
 * Replaces only the HTTP transport of the real API client, so services, interceptors (token
 * header, 401 handling) and error parsing run unchanged. Every request is recorded.
 */
export interface RecordedRequest {
  method: string
  url: string
  baseURL: string | undefined
  data: unknown
  headers: AxiosHeaders
}

export interface MockResponse {
  status: number
  data?: unknown
}

type Route = MockResponse | ((config: InternalAxiosRequestConfig) => MockResponse)

const originalAdapter = apiClient.defaults.adapter

export function mockApi(routes: Record<string, Route>): RecordedRequest[] {
  const requests: RecordedRequest[] = []
  const adapter: AxiosAdapter = async (config) => {
    const method = (config.method ?? 'get').toUpperCase()
    const url = config.url ?? ''
    requests.push({
      method,
      url,
      baseURL: config.baseURL,
      data: config.data,
      headers: AxiosHeaders.from(config.headers),
    })
    const route = routes[`${method} ${url}`]
    const result: MockResponse = !route
      ? { status: 404, data: { detail: `No mock for ${method} ${url}` } }
      : typeof route === 'function'
        ? route(config)
        : route
    const response: AxiosResponse = {
      data: result.data,
      status: result.status,
      statusText: String(result.status),
      headers: {},
      config,
      request: {},
    }
    if (result.status >= 400) {
      throw new AxiosError(
        `Request failed with status code ${result.status}`,
        AxiosError.ERR_BAD_REQUEST,
        config,
        {},
        response,
      )
    }
    return response
  }
  apiClient.defaults.adapter = adapter
  return requests
}

export function resetApiMock(): void {
  apiClient.defaults.adapter = originalAdapter
}

export const USER = {
  id: '11111111-1111-1111-1111-111111111111',
  email: 'alice@example.com',
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
}

export const PROJECT = {
  id: '22222222-2222-2222-2222-222222222222',
  name: 'Demo project',
  description: 'A test project',
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
}

export const ZIP_REPOSITORY = {
  id: '33333333-3333-3333-3333-333333333333',
  project_id: PROJECT.id,
  name: 'backend',
  source_type: 'zip' as const,
  source_url: null,
  default_branch: null,
  status: 'pending' as const,
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
}

export const GITHUB_REPOSITORY = {
  ...ZIP_REPOSITORY,
  id: '44444444-4444-4444-4444-444444444444',
  name: 'demo',
  source_type: 'github' as const,
  source_url: 'https://github.com/octo/demo',
}
