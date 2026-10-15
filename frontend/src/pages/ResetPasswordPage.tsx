import { CheckCircle2 } from 'lucide-react'
import { useEffect, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'

import { AUTH_BUTTON, AUTH_INPUT, AuthLayout } from '../components/AuthLayout'
import { ErrorMessage } from '../components/ErrorMessage'
import { confirmPasswordReset } from '../services/auth'
import { getErrorMessage } from '../services/client'

const MIN_PASSWORD_LENGTH = 8

/**
 * The reset token arrives in the URL fragment (#token=…), which browsers never send to the
 * server. It is read once (purely, so StrictMode's double render sees it too) and then removed
 * from the address bar and browser history by an effect.
 */
function readTokenFromUrl(): string {
  return new URLSearchParams(window.location.hash.slice(1)).get('token') ?? ''
}

export function ResetPasswordPage() {
  const [token] = useState(readTokenFromUrl)
  useEffect(() => {
    if (window.location.hash) {
      window.history.replaceState(null, '', window.location.pathname + window.location.search)
    }
  }, [])
  const [password, setPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  async function onSubmit(event: FormEvent) {
    event.preventDefault()
    if (submitting) {
      return
    }
    if (password.length < MIN_PASSWORD_LENGTH) {
      setError(`Password must be at least ${MIN_PASSWORD_LENGTH} characters long`)
      return
    }
    if (password !== confirmation) {
      setError('The two passwords do not match')
      return
    }
    setError(null)
    setSubmitting(true)
    try {
      setDone(await confirmPasswordReset(token, password))
    } catch (err) {
      setError(getErrorMessage(err, 'Could not reset the password'))
    } finally {
      setPassword('')
      setConfirmation('')
      setSubmitting(false)
    }
  }

  return (
    <AuthLayout>
      <div className="space-y-1.5">
        <h1 className="text-2xl font-bold tracking-tight">Choose a new password</h1>
        <p className="text-sm text-slate-400">Use at least {MIN_PASSWORD_LENGTH} characters.</p>
      </div>
      {!token ? (
        <ErrorMessage message="This reset link is incomplete. Open the link from your email again, or request a new one." />
      ) : done ? (
        <div role="status" className="flex gap-3 rounded-xl border border-emerald-500/30 bg-emerald-500/10 p-4">
          <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0 text-emerald-300" aria-hidden="true" />
          <p className="text-sm font-medium text-emerald-200">{done}.</p>
        </div>
      ) : (
        <form onSubmit={onSubmit} className="space-y-4" aria-label="Choose a new password">
          <label className="block text-sm">
            <span className="font-medium text-slate-300">New password</span>
            <input
              type="password"
              autoComplete="new-password"
              required
              className={AUTH_INPUT}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>
          <label className="block text-sm">
            <span className="font-medium text-slate-300">Confirm new password</span>
            <input
              type="password"
              autoComplete="new-password"
              required
              className={AUTH_INPUT}
              value={confirmation}
              onChange={(e) => setConfirmation(e.target.value)}
            />
          </label>
          <ErrorMessage message={error} />
          <button type="submit" disabled={submitting} className={AUTH_BUTTON}>
            {submitting ? 'Saving…' : 'Reset password'}
          </button>
        </form>
      )}
      <p className="text-center text-sm text-slate-400">
        {done ? 'Ready? ' : 'Need a new link? '}
        <Link to={done ? '/login' : '/forgot-password'} className="font-medium text-indigo-300 hover:underline">
          {done ? 'Sign in' : 'Request another reset email'}
        </Link>
      </p>
    </AuthLayout>
  )
}
