import { useState, type FormEvent } from 'react'
import { Link, Navigate, useNavigate } from 'react-router-dom'

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
    <div className="mx-auto mt-24 max-w-sm space-y-6 px-6">
      <h1 className="text-2xl font-bold">Create an account</h1>
      <form onSubmit={onSubmit} className="space-y-4" aria-label="Create an account">
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
          <span className="text-slate-400">Password (at least {MIN_PASSWORD_LENGTH} characters)</span>
          <input
            type="password"
            autoComplete="new-password"
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
          {submitting ? 'Creating account…' : 'Create account'}
        </button>
      </form>
      <p className="text-sm text-slate-400">
        Already registered? <Link to="/login" className="text-indigo-300 hover:underline">Sign in</Link>
      </p>
    </div>
  )
}
