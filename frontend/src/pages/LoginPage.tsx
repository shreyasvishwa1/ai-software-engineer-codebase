import { useState, type FormEvent } from 'react'
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom'

import { AUTH_BUTTON, AUTH_INPUT, AuthLayout } from '../components/AuthLayout'
import { ErrorMessage } from '../components/ErrorMessage'
import { useAuth } from '../hooks/useAuth'
import { getErrorMessage } from '../services/client'

export function LoginPage() {
  const { status, login } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const from = (location.state as { from?: string } | null)?.from ?? '/projects'
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  if (status === 'authenticated') {
    return <Navigate to={from} replace />
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      await login(email, password)
      navigate(from, { replace: true })
    } catch (err) {
      setError(getErrorMessage(err, 'Sign-in failed'))
    } finally {
      setPassword('')
      setSubmitting(false)
    }
  }

  return (
    <AuthLayout>
      <div className="space-y-1.5">
        <h1 className="text-2xl font-bold tracking-tight">Sign in</h1>
        <p className="text-sm text-slate-400">Welcome back. Pick up where you left off.</p>
      </div>
      <form onSubmit={onSubmit} className="space-y-4" aria-label="Sign in">
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
          <span className="font-medium text-slate-300">Password</span>
          <input
            type="password"
            autoComplete="current-password"
            required
            className={AUTH_INPUT}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        <div className="flex justify-end">
          <Link to="/forgot-password" className="text-sm font-medium text-indigo-300 hover:text-indigo-200 hover:underline">
            Forgot your password?
          </Link>
        </div>
        <ErrorMessage message={error} />
        <button type="submit" disabled={submitting} className={AUTH_BUTTON}>
          {submitting ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
      <p className="text-center text-sm text-slate-400">
        No account?{' '}
        <Link to="/register" className="font-medium text-indigo-300 hover:underline">
          Create one
        </Link>
      </p>
    </AuthLayout>
  )
}
