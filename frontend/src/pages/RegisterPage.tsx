import { useState, type FormEvent } from 'react'
import { Link, Navigate, useNavigate } from 'react-router-dom'

import { AUTH_BUTTON, AUTH_INPUT, AuthLayout } from '../components/AuthLayout'
import { ErrorMessage } from '../components/ErrorMessage'
import { useAuth } from '../hooks/useAuth'
import { getErrorMessage } from '../services/client'

const MIN_PASSWORD_LENGTH = 8

export function RegisterPage() {
  const { status, register } = useAuth()
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  if (status === 'authenticated') {
    return <Navigate to="/projects" replace />
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault()
    if (password.length < MIN_PASSWORD_LENGTH) {
      setError(`Password must be at least ${MIN_PASSWORD_LENGTH} characters long`)
      return
    }
    setError(null)
    setSubmitting(true)
    try {
      await register(email, password)
      navigate('/projects', { replace: true })
    } catch (err) {
      setError(getErrorMessage(err, 'Registration failed'))
    } finally {
      setPassword('')
      setSubmitting(false)
    }
  }

  return (
    <AuthLayout>
      <div className="space-y-1.5">
        <h1 className="text-2xl font-bold tracking-tight">Create an account</h1>
        <p className="text-sm text-slate-400">Start analyzing repositories in under a minute.</p>
      </div>
      <form onSubmit={onSubmit} className="space-y-4" aria-label="Create an account">
        <label className="block text-sm">
          <span className="font-medium text-slate-300">Email</span>
          <input
            type="email"
            autoComplete="email"
            required
            placeholder="you@example.com"
            className={AUTH_INPUT}
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </label>
        <label className="block text-sm">
          <span className="font-medium text-slate-300">Password (at least {MIN_PASSWORD_LENGTH} characters)</span>
          <input
            type="password"
            autoComplete="new-password"
            required
            className={AUTH_INPUT}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        <ErrorMessage message={error} />
        <button type="submit" disabled={submitting} className={AUTH_BUTTON}>
          {submitting ? 'Creating account…' : 'Create account'}
        </button>
      </form>
      <p className="text-center text-sm text-slate-400">
        Already registered?{' '}
        <Link to="/login" className="font-medium text-indigo-300 hover:underline">
          Sign in
        </Link>
      </p>
    </AuthLayout>
  )
}
