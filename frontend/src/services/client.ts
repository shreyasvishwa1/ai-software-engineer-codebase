import axios, { type AxiosError, type InternalAxiosRequestConfig } from 'axios'

import { clearToken, getToken, notifyUnauthorized } from './session'

// Relative base path: requests go to the same origin and, in development, through the
// Vite proxy. No backend host is hardcoded.
export const API_BASE_PATH = '/api/v1'

export const apiClient = axios.create({ baseURL: API_BASE_PATH, timeout: 30_000 })

function isAbsoluteUrl(url: string | undefined): boolean {
  return !!url && /^([a-z][a-z\d+\-.]*:)?\/\//i.test(url)
}

/** The token is attached only to requests for our own /api/v1 API. */
export function attachToken(config: InternalAxiosRequestConfig): InternalAxiosRequestConfig {
  const token = getToken()
  const ownApi = config.baseURL === API_BASE_PATH && !isAbsoluteUrl(config.url)
  if (token && ownApi) {
    config.headers.set('Authorization', `Bearer ${token}`)
  }
  return config
}

apiClient.interceptors.request.use(attachToken)

apiClient.interceptors.response.use(undefined, (error: AxiosError) => {
  if (error.response?.status === 401) {
    clearToken()
    notifyUnauthorized()
  }
  return Promise.reject(error)
})

interface ValidationIssue {
  msg?: unknown
}

/**
 * A user-facing message from an API error: the backend's ``detail`` when present. For
 * validation errors only the messages are used (never the submitted input values).
 */
export function getErrorMessage(error: unknown, fallback = 'Something went wrong'): string {
  if (axios.isAxiosError(error)) {
    const detail = (error.response?.data as { detail?: unknown } | undefined)?.detail
    if (typeof detail === 'string' && detail) {
      return detail
    }
    if (Array.isArray(detail)) {
      const messages = (detail as ValidationIssue[])
        .map((issue) => (typeof issue?.msg === 'string' ? issue.msg : null))
        .filter((msg): msg is string => !!msg)
      if (messages.length) {
        return messages.join('; ')
      }
    }
    if (!error.response) {
      return 'Could not reach the server'
    }
  }
  return fallback
}
