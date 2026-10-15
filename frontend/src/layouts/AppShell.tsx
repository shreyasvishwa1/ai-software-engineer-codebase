import { LogOut } from 'lucide-react'
import { Link, Outlet } from 'react-router-dom'

import { BrandMark } from '../components/AuthLayout'
import { useAuth } from '../hooks/useAuth'
import { useHealth } from '../hooks/useHealth'

function HealthIndicator() {
  const health = useHealth()
  const online = health.data?.status === 'ok'
  const label = health.isPending ? 'Checking backend…' : online ? 'Backend online' : 'Backend offline'
  return (
    <span
      className="hidden items-center gap-2 rounded-full border border-slate-800 bg-slate-900/60 px-3 py-1 text-xs text-slate-300 sm:flex"
      role="status"
    >
      <span className="relative flex h-2 w-2" aria-hidden="true">
        {online && <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-60" />}
        <span
          className={`relative inline-flex h-2 w-2 rounded-full ${online ? 'bg-emerald-400' : health.isPending ? 'bg-slate-500' : 'bg-red-400'}`}
        />
      </span>
      {label}
    </span>
  )
}

export function AppShell() {
  const { user, logout } = useAuth()
  return (
    <div className="flex min-h-screen flex-col">
      <header className="sticky top-0 z-30 border-b border-slate-800/80 bg-slate-950/70 backdrop-blur-xl">
        <div className="h-0.5 bg-gradient-to-r from-indigo-500 via-fuchsia-500 to-cyan-400" />
        <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-3">
          <Link to="/projects" className="flex items-center gap-2.5 font-semibold tracking-tight">
            <BrandMark />
            <span>
              AI Software Engineer
              <span className="ml-2 hidden rounded-md bg-indigo-500/15 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-indigo-300 md:inline">
                Codebase Intelligence
              </span>
            </span>
          </Link>
          <div className="flex items-center gap-3">
            <HealthIndicator />
            {user && (
              <span className="hidden items-center gap-2 text-sm text-slate-300 md:flex">
                <span
                  aria-hidden="true"
                  className="inline-flex h-7 w-7 items-center justify-center rounded-full bg-gradient-to-br from-fuchsia-500 to-indigo-500 text-xs font-bold uppercase text-white"
                >
                  {user.email.charAt(0)}
                </span>
                <span>{user.email}</span>
              </span>
            )}
            <button
              type="button"
              onClick={logout}
              className="inline-flex items-center gap-1.5 rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-200 hover:border-slate-600 hover:bg-slate-800"
            >
              <LogOut className="h-4 w-4" aria-hidden="true" />
              Sign out
            </button>
          </div>
        </div>
      </header>
      <main className="mx-auto w-full max-w-6xl flex-1 px-6 py-8">
        <Outlet />
      </main>
      <footer className="border-t border-slate-800/60 py-6 text-center text-xs text-slate-500">
        AI Software Engineer · Codebase intelligence that never executes your code
      </footer>
    </div>
  )
}
