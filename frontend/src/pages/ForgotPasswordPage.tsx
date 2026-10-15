import { MailCheck } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'

import { AUTH_BUTTON, AUTH_INPUT, AuthLayout } from '../components/AuthLayout'
import { ErrorMessage } from '../components/ErrorMessage'
import { requestPasswordReset } from '../services/auth'
import { getErrorMessage } from '../services/client'

export function ForgotPasswordPage() {
  const [email, setEmail] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [sent, setSent] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  async function onSubmit(event: FormEvent) {
    event.preventDefault()
    if (submitting) {
      return
    }
    setError(null)
    setSubmitting(true)
    try {
      setSent(await requestPasswordReset(email))
    } catch (err) {
      setError(getErrorMessage(err, 'Could not send the reset email'))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <AuthLayout>
      <div className="space-y-1.5">
        <h1 className="text-2xl font-bold tracking-tight">Reset your password</h1>
        <p className="text-sm text-slate-400">
          Enter your account email and we&apos;ll send you a link to choose a new password.
        </p>
      </div>
      {sent ? (
        <div role="status" className="flex gap-3 rounded-xl border border-emerald-500/30 bg-emerald-500/10 p-4">
          <MailCheck className="mt-0.5 h-5 w-5 shrink-0 text-emerald-300" aria-hidden="true" />
          <div className="space-y-1 text-sm">
            <p className="font-medium text-emerald-200">{sent}.</p>
            <p className="text-emerald-100/80">
              The link expires in 30 minutes. Check your spam folder if it doesn&apos;t arrive.
            </p>
          </div>
        </div>
      ) : (
        <form onSubmit={onSubmit} className="space-y-4" aria-label="Reset your password">
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
          <ErrorMessage message={error} />
          <button type="submit" disabled={submitting} className={AUTH_BUTTON}>
            {submitting ? 'Sending link…' : 'Send reset link'}
          </button>
        </form>
      )}
      <p className="text-center text-sm text-slate-400">
        Remembered it?{' '}
        <Link to="/login" className="font-medium text-indigo-300 hover:underline">
          Back to sign in
        </Link>
      </p>
    </AuthLayout>
  )
}
