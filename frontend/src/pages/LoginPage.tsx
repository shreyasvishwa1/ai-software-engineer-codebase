import { useState, type FormEvent } from 'react'
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom'

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
    <div className="mx-auto mt-24 max-w-sm space-y-6 px-6">
      <h1 className="text-2xl font-bold">Sign in</h1>
      <form onSubmit={onSubmit} className="space-y-4" aria-label="Sign in">
        <label className="block text-sm">
          <span className="text-slate-400">Email</span>
          <input
            type="email"
            autoComplete="email"
            required
            className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </label>
        <label className="block text-sm">
          <span className="text-slate-400">Password</span>
          <input
            type="password"
            autoComplete="current-password"
            required
            className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        <ErrorMessage message={error} />
        <button
          type="submit"
          disabled={submitting}
          className="w-full rounded-lg bg-indigo-600 px-4 py-2 font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
        >
          {submitting ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
      <p className="text-sm text-slate-400">
        No account? <Link to="/register" className="text-indigo-300 hover:underline">Create one</Link>
      </p>
    </div>
  )
}
