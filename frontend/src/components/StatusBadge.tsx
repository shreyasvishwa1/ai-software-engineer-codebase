import type { AnalysisJobStatus, RepositoryStatus } from '../types/api'

const STYLES: Record<RepositoryStatus | AnalysisJobStatus, { badge: string; dot: string }> = {
  pending: { badge: 'bg-slate-800 text-slate-300 border-slate-700', dot: 'bg-slate-400' },
  processing: { badge: 'bg-amber-500/10 text-amber-300 border-amber-500/30', dot: 'bg-amber-400 animate-pulse' },
  ready: { badge: 'bg-emerald-500/10 text-emerald-300 border-emerald-500/30', dot: 'bg-emerald-400' },
  completed: { badge: 'bg-emerald-500/10 text-emerald-300 border-emerald-500/30', dot: 'bg-emerald-400' },
  failed: { badge: 'bg-red-500/10 text-red-300 border-red-500/30', dot: 'bg-red-400' },
}

export function StatusBadge({ status }: { status: RepositoryStatus | AnalysisJobStatus }) {
  const style = STYLES[status]
  return (
    <span
      data-testid="status-badge"
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium capitalize ${style.badge}`}
    >
      <span aria-hidden="true" className={`h-1.5 w-1.5 rounded-full ${style.dot}`} />
      {status}
    </span>
  )
}
