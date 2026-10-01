import type { AnalysisJobStatus, RepositoryStatus } from '../types/api'

const STYLES: Record<RepositoryStatus | AnalysisJobStatus, string> = {
  pending: 'bg-slate-800 text-slate-300 border-slate-700',
  processing: 'bg-amber-500/10 text-amber-300 border-amber-500/30',
  ready: 'bg-emerald-500/10 text-emerald-300 border-emerald-500/30',
  completed: 'bg-emerald-500/10 text-emerald-300 border-emerald-500/30',
  failed: 'bg-red-500/10 text-red-300 border-red-500/30',
}

export function StatusBadge({ status }: { status: RepositoryStatus | AnalysisJobStatus }) {
  return (
    <span
      data-testid="status-badge"
      className={`inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-medium ${STYLES[status]}`}
    >
      {status}
    </span>
  )
}
