import { Cpu } from 'lucide-react'
import { Link, Outlet } from 'react-router-dom'

import { useAuth } from '../hooks/useAuth'
import { useHealth } from '../hooks/useHealth'

function HealthIndicator() {
  const health = useHealth()
  const online = health.data?.status === 'ok'
  const label = health.isPending ? 'Checking backend…' : online ? 'Backend online' : 'Backend offline'
  return (
    <span className="flex items-center gap-2 text-xs text-slate-400" role="status">
      <span
        aria-hidden="true"
        className={`h-2 w-2 rounded-full ${online ? 'bg-emerald-400' : health.isPending ? 'bg-slate-500' : 'bg-red-400'}`}
      />
      {label}
    </span>
  )
}

export function AppShell() {
  const { user, logout } = useAuth()
  return (
    <div className="flex min-h-screen flex-col">
      <header className="border-b border-slate-800 bg-slate-900/60">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-3">
          <Link to="/projects" className="flex items-center gap-2 font-semibold">
            <Cpu className="h-5 w-5 text-indigo-400" aria-hidden="true" />
            AI Software Engineer
          </Link>
          <div className="flex items-center gap-4">
            <HealthIndicator />
            {user && <span className="text-sm text-slate-300">{user.email}</span>}
            <button
              type="button"
              onClick={logout}
              className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm hover:bg-slate-800"
            >
              Sign out
            </button>
          </div>
        </div>
      </header>
      <main className="mx-auto w-full max-w-6xl flex-1 px-6 py-8">
        <Outlet />
      </main>
    </div>
  )
}
