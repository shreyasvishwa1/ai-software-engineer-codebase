import { StrictMode } from 'react'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import App from '../App'
import { mockApi } from '../test/mockApi'
import { renderWithProviders } from '../test/render'

const SENT = 'If an account exists for that email, a reset link is on its way'
const DONE = 'Your password has been reset; you can now sign in'
const INVALID = 'This reset link is invalid or has expired; request a new one'
const TOKEN = 'reset.token.value'
const NEW_PASSWORD = 'brand-new-Passw0rd'

function fill(label: RegExp, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } })
}

function bodyOf(request: { data: unknown }) {
  return JSON.parse(String(request.data)) as Record<string, string>
}

beforeEach(() => {
  window.history.replaceState(null, '', '/')
})

afterEach(() => {
  window.history.replaceState(null, '', '/')
})

describe('forgot password', () => {
  it('links to the reset page from sign-in', async () => {
    mockApi({})
    renderWithProviders(<App />, { route: '/login' })
    fireEvent.click(screen.getByRole('link', { name: 'Forgot your password?' }))
    expect(await screen.findByRole('heading', { name: 'Reset your password' })).toBeTruthy()
  })

  it('requests a reset email and shows the neutral confirmation', async () => {
    const requests = mockApi({ 'POST /auth/password-reset/request': { status: 202, data: { detail: SENT } } })
    renderWithProviders(<App />, { route: '/forgot-password' })
    fill(/email/i, 'alice@example.com')
    fireEvent.click(screen.getByRole('button', { name: 'Send reset link' }))
    const status = await screen.findByRole('status')
    expect(status.textContent).toContain(SENT)
    expect(status.textContent).toContain('30 minutes')
    const posts = requests.filter((r) => r.method === 'POST')
    expect(posts.map((r) => r.url)).toEqual(['/auth/password-reset/request'])
    expect(bodyOf(posts[0])).toEqual({ email: 'alice@example.com' })
    expect(screen.queryByRole('button', { name: 'Send reset link' })).toBeNull()
  })

  it('shows the server message when email is not configured', async () => {
    mockApi({
      'POST /auth/password-reset/request': {
        status: 503,
        data: { detail: 'Password reset by email is not available' },
      },
    })
    renderWithProviders(<App />, { route: '/forgot-password' })
    fill(/email/i, 'alice@example.com')
    fireEvent.click(screen.getByRole('button', { name: 'Send reset link' }))
    expect((await screen.findByRole('alert')).textContent).toBe('Password reset by email is not available')
    expect(screen.getByRole('button', { name: 'Send reset link' })).toBeTruthy()
  })

  it('links back to sign in', async () => {
    mockApi({})
    renderWithProviders(<App />, { route: '/forgot-password' })
    fireEvent.click(screen.getByRole('link', { name: 'Back to sign in' }))
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeTruthy()
  })
})

describe('reset password', () => {
  function openWithToken(token = TOKEN) {
    window.history.replaceState(null, '', `/reset-password#token=${token}`)
    renderWithProviders(<App />, { route: '/reset-password' })
  }

  it('sends the token from the link with the new password, then offers sign in', async () => {
    const requests = mockApi({ 'POST /auth/password-reset/confirm': { status: 200, data: { detail: DONE } } })
    openWithToken()
    fill(/^new password/i, NEW_PASSWORD)
    fill(/confirm new password/i, NEW_PASSWORD)
    fireEvent.click(screen.getByRole('button', { name: 'Reset password' }))
    expect((await screen.findByRole('status')).textContent).toContain(DONE)
    const posts = requests.filter((r) => r.method === 'POST')
    expect(bodyOf(posts[0])).toEqual({ token: TOKEN, password: NEW_PASSWORD })
    fireEvent.click(screen.getByRole('link', { name: 'Sign in' }))
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeTruthy()
  })

  it('removes the token from the address bar once read', async () => {
    mockApi({})
    openWithToken()
    await screen.findByRole('heading', { name: 'Choose a new password' })
    expect(window.location.hash).toBe('')
    expect(window.location.href).not.toContain(TOKEN)
  })

  it('keeps the token under StrictMode double rendering', async () => {
    const requests = mockApi({ 'POST /auth/password-reset/confirm': { status: 200, data: { detail: DONE } } })
    window.history.replaceState(null, '', `/reset-password#token=${TOKEN}`)
    renderWithProviders(
      <StrictMode>
        <App />
      </StrictMode>,
      { route: '/reset-password' },
    )
    fill(/^new password/i, NEW_PASSWORD)
    fill(/confirm new password/i, NEW_PASSWORD)
    fireEvent.click(screen.getByRole('button', { name: 'Reset password' }))
    expect((await screen.findByRole('status')).textContent).toContain(DONE)
    expect(bodyOf(requests.filter((r) => r.method === 'POST')[0]).token).toBe(TOKEN)
    expect(window.location.hash).toBe('')
  })

  it('checks length and matching before sending anything', async () => {
    const requests = mockApi({})
    openWithToken()
    fill(/^new password/i, 'short')
    fill(/confirm new password/i, 'short')
    fireEvent.click(screen.getByRole('button', { name: 'Reset password' }))
    expect((await screen.findByRole('alert')).textContent).toContain('at least 8 characters')
    fill(/^new password/i, NEW_PASSWORD)
    fill(/confirm new password/i, `${NEW_PASSWORD}x`)
    fireEvent.click(screen.getByRole('button', { name: 'Reset password' }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toBe('The two passwords do not match'))
    expect(requests.filter((r) => r.method === 'POST')).toHaveLength(0)
  })

  it('shows an expired link and clears the password fields', async () => {
    mockApi({ 'POST /auth/password-reset/confirm': { status: 400, data: { detail: INVALID } } })
    openWithToken()
    fill(/^new password/i, NEW_PASSWORD)
    fill(/confirm new password/i, NEW_PASSWORD)
    fireEvent.click(screen.getByRole('button', { name: 'Reset password' }))
    expect((await screen.findByRole('alert')).textContent).toBe(INVALID)
    expect((screen.getByLabelText(/^new password/i) as HTMLInputElement).value).toBe('')
    expect(screen.getByRole('link', { name: 'Request another reset email' })).toBeTruthy()
  })

  it('explains a link without a token and shows no form', async () => {
    mockApi({})
    renderWithProviders(<App />, { route: '/reset-password' })
    expect((await screen.findByRole('alert')).textContent).toContain('incomplete')
    expect(screen.queryByRole('button', { name: 'Reset password' })).toBeNull()
  })
})
