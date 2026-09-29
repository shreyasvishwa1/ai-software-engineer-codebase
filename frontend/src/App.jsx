import { useEffect, useState } from 'react'
import { getHealth, getApiHealth } from './services/api.js'
import { Activity, CheckCircle2, XCircle, Loader2, Cpu, Database, Github, Shield, BarChart3, Code2 } from 'lucide-react'

export default function App() {
  const [health, setHealth] = useState(null)
  const [apiHealth, setApiHealth] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    async function check() {
      try {
        setLoading(true)
        const [h, ah] = await Promise.all([getHealth(), getApiHealth()])
        setHealth(h)
        setApiHealth(ah)
        setError(null)
      } catch (e) {
        setError(e.message || 'Failed to connect to backend')
      } finally {
        setLoading(false)
      }
    }
    check()
    const id = setInterval(check, 10000)
    return () => clearInterval(id)
  }, [])

  const connected = !!health && !!apiHealth && !error

  return (
    <div className="min-h-screen flex flex-col">
      {/* Header */}
      <header className="border-b border-slate-800 bg-slate-900/60 backdrop-blur sticky top-0 z-10">
        <div className="max-w-6xl mx-auto px-6 py-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-xl bg-indigo-600 flex items-center justify-center">
              <Cpu className="w-5 h-5 text-white" />
            </div>
            <div>
              <h1 className="font-bold text-lg leading-none">AI Software Engineer</h1>
              <p className="text-xs text-slate-400">Codebase Intelligence Platform</p>
            </div>
          </div>
          <span className={`inline-flex items-center gap-2 px-3 py-1.5 rounded-full text-xs font-medium border ${
            loading
              ? 'bg-slate-800 text-slate-300 border-slate-700'
              : connected
                ? 'bg-emerald-500/10 text-emerald-300 border-emerald-500/30'
                : 'bg-red-500/10 text-red-400 border-red-500/30'
          }`}>
            {loading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : connected ? <CheckCircle2 className="w-3.5 h-3.5" /> : <XCircle className="w-3.5 h-3.5" />}
            {loading ? 'Checking...' : connected ? 'Backend Connected' : 'Backend Disconnected'}
          </span>
        </div>
      </header>

      {/* Hero */}
      <main className="flex-1 max-w-6xl mx-auto w-full px-6 py-10">
        <div className="rounded-2xl border border-slate-800 bg-gradient-to-br from-slate-900 to-slate-900/50 p-8 md:p-10">
          <div className="flex flex-wrap gap-2 mb-4">
            <span className="text-xs px-2.5 py-1 rounded-full bg-indigo-500/15 text-indigo-300 border border-indigo-500/20">Phase 1 — Foundation</span>
            <span className="text-xs px-2.5 py-1 rounded-full bg-slate-800 text-slate-300 border border-slate-700">FastAPI + React + Vite</span>
          </div>
          <h2 className="text-3xl md:text-4xl font-extrabold tracking-tight">
            AI Software Engineer
          </h2>
          <p className="mt-3 text-slate-400 max-w-2xl leading-relaxed">
            Web-based AI-powered platform to understand, analyze and explain any software repository.
            Import a GitHub URL or ZIP, get language detection, file analysis, security findings, complexity, architecture, health score & RAG-powered codebase chat.
          </p>

          {/* Status cards */}
          <div className="grid md:grid-cols-2 gap-4 mt-8">
            <div className="rounded-xl bg-slate-950 border border-slate-800 p-5">
              <div className="flex items-center gap-2 text-sm font-semibold text-slate-200">
                <Activity className="w-4 h-4 text-indigo-400" /> GET /health
              </div>
              <div className="mt-3">
                {loading && <p className="text-sm text-slate-500 flex items-center gap-2"><Loader2 className="w-4 h-4 animate-spin" /> Checking backend...</p>}
                {error && <p className="text-sm text-red-400">{error} — Is backend running on http://127.0.0.1:8000 ?</p>}
                {health && (
                  <pre className="text-xs bg-slate-900 border border-slate-800 rounded-lg p-3 overflow-auto text-emerald-300">{JSON.stringify(health, null, 2)}</pre>
                )}
              </div>
            </div>
            <div className="rounded-xl bg-slate-950 border border-slate-800 p-5">
              <div className="flex items-center gap-2 text-sm font-semibold text-slate-200">
                <Database className="w-4 h-4 text-indigo-400" /> GET /api/health
              </div>
              <div className="mt-3">
                {loading && <p className="text-sm text-slate-500 flex items-center gap-2"><Loader2 className="w-4 h-4 animate-spin" /> Checking backend...</p>}
                {apiHealth && (
                  <pre className="text-xs bg-slate-900 border border-slate-800 rounded-lg p-3 overflow-auto text-emerald-300">{JSON.stringify(apiHealth, null, 2)}</pre>
                )}
              </div>
            </div>
          </div>

          {/* Feature preview */}
          <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-4 mt-8">
            {[
              { icon: Code2, title: 'Code Analysis', desc: 'Tree-sitter parsing, functions & classes' },
              { icon: Shield, title: 'Security', desc: 'Semgrep findings & secrets' },
              { icon: BarChart3, title: 'Complexity', desc: 'Cyclomatic & maintainability' },
              { icon: Github, title: 'Import', desc: 'GitHub URL or ZIP upload' },
            ].map(f => (
              <div key={f.title} className="rounded-xl border border-slate-800 bg-slate-950 p-4">
                <f.icon className="w-5 h-5 text-indigo-400" />
                <div className="font-semibold text-sm mt-2">{f.title}</div>
                <div className="text-xs text-slate-500 mt-1">{f.desc}</div>
              </div>
            ))}
          </div>

          <div className="mt-8 flex flex-wrap gap-3">
            <a href="http://127.0.0.1:8000/docs" target="_blank" rel="noreferrer" className="px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white text-sm font-medium">Open API Docs →</a>
            <a href="http://127.0.0.1:8000/health" target="_blank" rel="noreferrer" className="px-4 py-2 rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 text-sm font-medium">Raw /health</a>
          </div>
        </div>

        <div className="mt-6 flex items-center justify-between text-xs text-slate-500">
          <span>Build → Test → Verify → Next</span>
          <span>Say <b className="text-slate-300">NEXT</b> when Phase 1 is verified to continue to Phase 2.</span>
        </div>
      </main>

      <footer className="border-t border-slate-800 py-4 text-center text-xs text-slate-500">
        AI Software Engineer — Codebase Intelligence Platform • Phase 1 Foundation • FastAPI + React + Vite + Tailwind
      </footer>
    </div>
  )
}
