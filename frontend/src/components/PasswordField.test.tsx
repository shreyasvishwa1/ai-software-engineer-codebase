import { fireEvent, render, screen, within } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'

import App from '../App'
import { mockApi } from '../test/mockApi'
import { renderWithProviders } from '../test/render'
import { PasswordField } from './AuthLayout'

function Harness({ onSubmit }: { onSubmit?: () => void }) {
  const [value, setValue] = useState('')
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault()
        onSubmit?.()
      }}
    >
      <PasswordField label="Password" autoComplete="current-password" value={value} onChange={setValue} />
      <output data-testid="value">{value}</output>
    </form>
  )
}

const input = () => screen.getByLabelText('Password') as HTMLInputElement

describe('PasswordField', () => {
  it('starts hidden and shows or hides the typed password with the eye button', () => {
    render(<Harness />)
    fireEvent.change(input(), { target: { value: 's3cret-pass' } })
    expect(input().type).toBe('password')

    const show = screen.getByRole('button', { name: 'Show password' })
    expect(show.getAttribute('aria-pressed')).toBe('false')
    fireEvent.click(show)

    const hide = screen.getByRole('button', { name: 'Hide password' })
    expect(hide.getAttribute('aria-pressed')).toBe('true')
    expect(input().type).toBe('text')
    expect(input().value).toBe('s3cret-pass') // toggling never loses what was typed

    fireEvent.click(hide)
    expect(input().type).toBe('password')
    expect(screen.getByRole('button', { name: 'Show password' })).toBeTruthy()
  })

  it('keeps the input named by its label alone and wired to the button', () => {
    render(<Harness />)
    const button = screen.getByRole('button', { name: 'Show password' })
    expect(button.getAttribute('aria-controls')).toBe(input().id)
    expect(input().getAttribute('autocomplete')).toBe('current-password')
    expect(input().required).toBe(true)
  })

  it('never submits the form', () => {
    let submitted = 0
    render(<Harness onSubmit={() => (submitted += 1)} />)
    fireEvent.click(screen.getByRole('button', { name: 'Show password' }))
    expect(submitted).toBe(0)
    expect(screen.getByRole('button', { name: 'Hide password' }).getAttribute('type')).toBe('button')
  })

  it('updates the value as the user types', () => {
    render(<Harness />)
    fireEvent.change(input(), { target: { value: 'abc' } })
    expect(screen.getByTestId('value').textContent).toBe('abc')
  })
})

describe('eye buttons on the auth pages', () => {
  it('appears on the sign-in page', async () => {
    mockApi({})
    renderWithProviders(<App />, { route: '/login' })
    const form = await screen.findByRole('heading', { name: 'Sign in' })
    expect(form).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Show password' }))
    expect((screen.getByLabelText('Password') as HTMLInputElement).type).toBe('text')
  })

  it('appears on the registration page', async () => {
    mockApi({})
    renderWithProviders(<App />, { route: '/register' })
    await screen.findByRole('heading', { name: 'Create an account' })
    fireEvent.click(screen.getByRole('button', { name: 'Show password' }))
    expect((screen.getByLabelText(/^password/i) as HTMLInputElement).type).toBe('text')
  })

  it('toggles the two reset fields independently', async () => {
    mockApi({})
    window.history.replaceState(null, '', '/reset-password#token=t')
    renderWithProviders(<App />, { route: '/reset-password' })
    const form = await screen.findByRole('form', { name: 'Choose a new password' })
    const [first, second] = within(form).getAllByRole('button', { name: 'Show password' })
    fireEvent.click(second)
    expect((screen.getByLabelText('New password') as HTMLInputElement).type).toBe('password')
    expect((screen.getByLabelText('Confirm new password') as HTMLInputElement).type).toBe('text')
    fireEvent.click(first)
    expect((screen.getByLabelText('New password') as HTMLInputElement).type).toBe('text')
    window.history.replaceState(null, '', '/')
  })
})
