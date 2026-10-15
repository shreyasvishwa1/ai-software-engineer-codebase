import { Bug, Code2, GitBranch, ShieldCheck } from 'lucide-react'
import type { ReactNode } from 'react'

const HIGHLIGHTS = [
  { icon: GitBranch, title: 'Import any repository', text: 'ZIP uploads or public GitHub archives, ingested safely.' },
  { icon: Code2, title: 'Navigate code across files', text: 'Definitions, references, call and type hierarchies.' },
  { icon: Bug, title: 'Five static diagnostics', text: 'Unused imports and variables, unbound and redefined names, unreachable code.' },
  { icon: ShieldCheck, title: 'Never runs your code', text: 'Python is parsed with ast and tokenize only.' },
]

export function BrandMark({ size = 'md' }: { size?: 'md' | 'lg' }) {
  const box = size === 'lg' ? 'h-11 w-11' : 'h-8 w-8'
  const icon = size === 'lg' ? 'h-6 w-6' : 'h-4 w-4'
  return (
    <span
      aria-hidden="true"
      className={`inline-flex ${box} items-center justify-center rounded-xl bg-gradient-to-br from-indigo-500 via-violet-500 to-cyan-400 shadow-lg shadow-indigo-500/30`}
    >
      <Code2 className={`${icon} text-white`} />
    </span>
  )
}

/** Split-screen frame for the sign-in, registration and password pages. */
export function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-screen">
      <aside className="relative hidden flex-1 overflow-hidden lg:flex">
        <div className="absolute inset-0 bg-gradient-to-br from-indigo-600 via-violet-700 to-slate-950" />
        <div className="absolute -left-24 top-1/3 h-96 w-96 rounded-full bg-cyan-400/25 blur-3xl" />
        <div className="absolute -right-16 -top-16 h-80 w-80 rounded-full bg-fuchsia-500/30 blur-3xl" />
        <div className="relative z-10 flex w-full flex-col justify-between p-12 text-white">
          <div className="flex items-center gap-3 text-lg font-semibold">
            <BrandMark size="lg" />
            AI Software Engineer
          </div>
          <div className="max-w-lg space-y-8">
            <p className="text-4xl font-extrabold leading-tight tracking-tight">
              Understand any codebase in minutes.
            </p>
            <ul className="space-y-5">
              {HIGHLIGHTS.map(({ icon: Icon, title, text }) => (
                <li key={title} className="flex gap-4">
                  <span className="mt-0.5 inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-white/15 ring-1 ring-white/25 backdrop-blur">
                    <Icon className="h-5 w-5" aria-hidden="true" />
                  </span>
                  <span>
                    <span className="block font-semibold">{title}</span>
                    <span className="block text-sm text-indigo-100/80">{text}</span>
                  </span>
                </li>
              ))}
            </ul>
          </div>
          <p className="text-xs text-indigo-100/70">Codebase intelligence platform · FastAPI · React · PostgreSQL</p>
        </div>
      </aside>
      <main className="flex flex-1 items-center justify-center px-6 py-12">
        <div className="w-full max-w-md">
          <div className="mb-8 flex items-center gap-3 lg:hidden">
            <BrandMark />
            <span className="font-semibold">AI Software Engineer</span>
          </div>
          <div className="glass-card space-y-6 p-8">{children}</div>
        </div>
      </main>
    </div>
  )
}

export const AUTH_INPUT =
  'mt-1.5 w-full rounded-xl border border-slate-700 bg-slate-950/70 px-3.5 py-2.5 text-slate-100 placeholder:text-slate-500'
export const AUTH_BUTTON =
  'w-full rounded-xl bg-indigo-600 px-4 py-2.5 font-semibold text-white hover:bg-indigo-500 disabled:cursor-not-allowed disabled:opacity-50'
