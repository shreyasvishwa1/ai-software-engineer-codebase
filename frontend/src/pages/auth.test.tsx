import { fireEvent, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'

import App from '../App'
import { getToken, setToken } from '../services/session'
import { mockApi, USER } from '../test/mockApi'
import { renderWithProviders } from '../test/render'

const TOKEN = 'header.payload.signature-secret'
const PASSWORD = 'correct-horse-battery'

const authenticatedRoutes = {
  'GET /auth/me': { status: 200, data: USER },
  'GET /projects': { status: 200, data: [] },
  'GET /health': { status: 200, data: { status: 'ok' } },
}

function fill(label: RegExp, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } })
}

let consoleSpies: MockInstance[] = []

beforeEach(() => {
  consoleSpies = (['log', 'info', 'warn', 'error', 'debug'] as const).map((level) =>
    vi.spyOn(console, level).mockImplementation(() => {}),
  )
})

afterEach(() => {
  // Neither the token nor the password is ever written to the console.
  for (const spy of consoleSpies) {
    const output = JSON.stringify(spy.mock.calls)
    expect(output).not.toContain(TOKEN)
    expect(output).not.toContain(PASSWORD)
    spy.mockRestore()
  }
})

describe('sign in', () => {
  it('stores the token in sessionStorage and opens the dashboard', async () => {
    const requests = mockApi({
      'POST /auth/login': { status: 200, data: { access_token: TOKEN, token_type: 'bearer' } },
      ...authenticatedRoutes,
    })
    renderWithProviders(<App />, { route: '/login' })
    fill(/email/i, 'alice@example.com')
    fill(/password/i, PASSWORD)
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))

    expect(await screen.findByRole('heading', { name: 'Projects' })).toBeTruthy()
    expect(getToken()).toBe(TOKEN)
    expect(window.localStorage.length).toBe(0) // sessionStorage only
    const login = requests.find((r) => r.url === '/auth/login')
    expect(JSON.parse(login?.data as string)).toEqual({
      email: 'alice@example.com',
      password: PASSWORD,
    })
    expect(requests.every((r) => !r.url.includes(TOKEN))).toBe(true)
    expect(document.body.textContent).not.toContain(TOKEN)
    expect(document.body.textContent).toContain('alice@example.com')
  })

  it('shows the API error and does not disclose the password', async () => {
    mockApi({
      'POST /auth/login': { status: 401, data: { detail: 'Invalid email or password' } },
    })
    renderWithProviders(<App />, { route: '/login' })
    fill(/email/i, 'alice@example.com')
    fill(/password/i, PASSWORD)
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))

    expect((await screen.findByRole('alert')).textContent).toBe('Invalid email or password')
    expect(getToken()).toBeNull()
    expect((screen.getByLabelText(/password/i) as HTMLInputElement).value).toBe('')
    expect(document.body.textContent).not.toContain(PASSWORD)
  })
})

describe('registration', () => {
  it('registers, signs in and opens the dashboard', async () => {
    const requests = mockApi({
      'POST /auth/register': { status: 201, data: USER },
      'POST /auth/login': { status: 200, data: { access_token: TOKEN, token_type: 'bearer' } },
      ...authenticatedRoutes,
    })
    renderWithProviders(<App />, { route: '/register' })
    fill(/email/i, 'alice@example.com')
    fill(/password/i, PASSWORD)
    fireEvent.click(screen.getByRole('button', { name: 'Create account' }))

    expect(await screen.findByRole('heading', { name: 'Projects' })).toBeTruthy()
    expect(requests.map((r) => `${r.method} ${r.url}`).slice(0, 3)).toEqual([
      'POST /auth/register',
      'POST /auth/login',
      'GET /auth/me',
    ])
    expect(getToken()).toBe(TOKEN)
  })

  it('shows the duplicate-account error', async () => {
    mockApi({
      'POST /auth/register': {
        status: 409,
        data: { detail: 'An account with this email already exists' },
      },
    })
    renderWithProviders(<App />, { route: '/register' })
    fill(/email/i, 'alice@example.com')
    fill(/password/i, PASSWORD)
    fireEvent.click(screen.getByRole('button', { name: 'Create account' }))
    expect((await screen.findByRole('alert')).textContent).toBe(
      'An account with this email already exists',
    )
    expect(getToken()).toBeNull()
  })

  it('validates the password length before calling the API', async () => {
    const requests = mockApi({})
    renderWithProviders(<App />, { route: '/register' })
    fill(/email/i, 'alice@example.com')
    fill(/password/i, 'short')
    fireEvent.click(screen.getByRole('button', { name: 'Create account' }))
    expect((await screen.findByRole('alert')).textContent).toContain('at least 8 characters')
    expect(requests).toHaveLength(0)
  })
})

describe('protected routes', () => {
  it('redirects to sign-in without a session', async () => {
    const requests = mockApi({})
    renderWithProviders(<App />, { route: '/projects' })
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeTruthy()
    expect(requests).toHaveLength(0)
  })

  it('restores a valid session from sessionStorage', async () => {
    setToken(TOKEN)
    mockApi(authenticatedRoutes)
    renderWithProviders(<App />, { route: '/projects' })
    expect(await screen.findByRole('heading', { name: 'Projects' })).toBeTruthy()
  })

  it('clears an expired session and returns to sign-in on 401', async () => {
    setToken(TOKEN)
    mockApi({ 'GET /auth/me': { status: 401, data: { detail: 'Not authenticated' } } })
    renderWithProviders(<App />, { route: '/projects' })
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeTruthy()
    expect(getToken()).toBeNull()
  })

  it('signs out when any API call returns 401 later', async () => {
    setToken(TOKEN)
    mockApi({
      'GET /auth/me': { status: 200, data: USER },
      'GET /projects': { status: 401, data: { detail: 'Not authenticated' } },
      'GET /health': { status: 200, data: { status: 'ok' } },
    })
    renderWithProviders(<App />, { route: '/projects' })
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeTruthy()
    expect(getToken()).toBeNull()
  })

  it('signs out from the header', async () => {
    setToken(TOKEN)
    mockApi(authenticatedRoutes)
    renderWithProviders(<App />, { route: '/projects' })
    fireEvent.click(await screen.findByRole('button', { name: 'Sign out' }))
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeTruthy()
    expect(getToken()).toBeNull()
  })

  it('shows the backend health indicator', async () => {
    setToken(TOKEN)
    mockApi(authenticatedRoutes)
    renderWithProviders(<App />, { route: '/projects' })
    await waitFor(() => expect(screen.getByText('Backend online')).toBeTruthy())
  })
})
