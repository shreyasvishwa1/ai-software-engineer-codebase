import { useQuery } from '@tanstack/react-query'
import {
  CheckCircle2,
  Circle,
  FileCode2,
  Files,
  Languages,
  Package,
  ShieldAlert,
  ShieldCheck,
  XCircle,
  type LucideIcon,
} from 'lucide-react'
import type { ReactNode } from 'react'
import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from 'recharts'

import { queryKeys } from '../hooks/queryKeys'
import { useCodeMetrics } from '../hooks/useCodeMetrics'
import { useDependencies } from '../hooks/useDependencies'
import { useSecretScan } from '../hooks/useSecretScan'
import { listJobs } from '../services/jobs'
import type {
  CodeMetrics,
  DependencyInventory,
  FileMetric,
  LanguageStatistic,
  Repository,
  RepositoryLanguages,
  RepositoryTree,
  SecretScan,
} from '../types/api'
import { formatBytes } from '../utils/format'
import { largestFiles } from './CodeMetricsPanel'
import { languageLabel } from './LanguageStats'

/** Categorical order (validated on the dark surface); "Other" is always the neutral last slot. */
const SERIES = ['#6366f1', '#0891b2', '#ec4899', '#d97706', '#059669']
const OTHER = '#5c6694'
const LONG_FILE_LINES = 1000
const SIZE_BUCKETS: { label: string; max: number }[] = [
  { label: 'Under 100 lines', max: 99 },
  { label: '100–499 lines', max: 499 },
  { label: '500–999 lines', max: 999 },
  { label: '1,000+ lines', max: Number.POSITIVE_INFINITY },
]

const PANEL = 'rounded-2xl border border-slate-800 bg-slate-900/70 p-5 shadow-glow backdrop-blur'
const PANEL_TITLE = 'text-sm font-semibold text-slate-100'
const PANEL_NOTE = 'mt-1 text-xs text-slate-400'

const number = (value: number) => value.toLocaleString('en-US')
const basename = (path: string) => path.slice(path.lastIndexOf('/') + 1)

type CheckState = 'pass' | 'fail' | 'unknown'

/** What to show for a query without data: still loading, or the request failed. */
type Pending = { isPending: boolean }
const waiting = (query: Pending, loading: string) => (query.isPending ? loading : 'Unavailable right now')

interface HealthCheck {
  label: string
  detail: string
  state: CheckState
}

function healthChecks(
  repository: Repository,
  metrics: CodeMetrics | undefined,
  inventory: DependencyInventory | undefined,
  scan: SecretScan | undefined,
  queries: { metrics: Pending; inventory: Pending; scan: Pending },
): HealthCheck[] {
  const analyzed = metrics?.summary ? metrics.files.filter((f) => f.line_count !== null) : null
  const longFiles = analyzed?.filter((f) => (f.line_count ?? 0) >= LONG_FILE_LINES).length
  const brokenManifests = inventory
    ? inventory.summary.invalid + inventory.summary.too_large + inventory.summary.unavailable
    : null
  return [
    {
      label: 'Snapshot ingested',
      detail: repository.status === 'ready' ? 'Files are stored and verified' : `Status: ${repository.status}`,
      state: repository.status === 'ready' ? 'pass' : repository.status === 'failed' ? 'fail' : 'unknown',
    },
    {
      label: 'Code metrics up to date',
      detail: metrics
        ? metrics.summary
          ? 'The current snapshot is analyzed'
          : 'Run "Analyze code" below'
        : waiting(queries.metrics, 'Loading…'),
      state: metrics === undefined ? 'unknown' : metrics.summary ? 'pass' : 'fail',
    },
    {
      label: 'No likely secrets committed',
      detail: scan
        ? `${number(scan.summary.findings)} finding${scan.summary.findings === 1 ? '' : 's'} in ${number(scan.summary.files_scanned)} files`
        : waiting(queries.scan, 'Scanning…'),
      state: scan === undefined ? 'unknown' : scan.summary.findings === 0 ? 'pass' : 'fail',
    },
    {
      label: 'Manifests parse cleanly',
      detail: inventory
        ? inventory.summary.manifests === 0
          ? 'No dependency manifests found'
          : `${number(brokenManifests ?? 0)} of ${number(inventory.summary.manifests)} could not be read`
        : waiting(queries.inventory, 'Checking…'),
      state: brokenManifests === null ? 'unknown' : brokenManifests === 0 ? 'pass' : 'fail',
    },
    {
      label: `No files over ${number(LONG_FILE_LINES)} lines`,
      detail:
        longFiles === undefined
          ? metrics
            ? 'Needs code metrics'
            : waiting(queries.metrics, 'Loading…')
          : `${number(longFiles)} file${longFiles === 1 ? '' : 's'} at ${number(LONG_FILE_LINES)}+ lines`,
      state: longFiles === undefined ? 'unknown' : longFiles === 0 ? 'pass' : 'fail',
    },
  ]
}

function scoreTone(score: number) {
  if (score >= 80) return { ring: '#34d399', pill: 'bg-emerald-500/15 text-emerald-300', label: 'Healthy' }
  if (score >= 50) return { ring: '#fbbf24', pill: 'bg-amber-500/15 text-amber-300', label: 'Attention' }
  return { ring: '#fb7185', pill: 'bg-rose-500/15 text-rose-300', label: 'At risk' }
}

function ScoreRing({ score, color }: { score: number; color: string }) {
  const radius = 42
  const circumference = 2 * Math.PI * radius
  return (
    <svg viewBox="0 0 100 100" className="h-28 w-28 shrink-0 -rotate-90" aria-hidden="true">
      <circle cx="50" cy="50" r={radius} fill="none" stroke="#232b4e" strokeWidth="9" />
      <circle
        cx="50"
        cy="50"
        r={radius}
        fill="none"
        stroke={color}
        strokeWidth="9"
        strokeLinecap="round"
        strokeDasharray={`${(score / 100) * circumference} ${circumference}`}
        className="transition-all duration-700"
      />
    </svg>
  )
}

const CHECK_ICONS: Record<CheckState, { icon: LucideIcon; className: string; text: string }> = {
  pass: { icon: CheckCircle2, className: 'text-emerald-400', text: 'Passing' },
  fail: { icon: XCircle, className: 'text-rose-400', text: 'Needs attention' },
  unknown: { icon: Circle, className: 'text-slate-500', text: 'Not run' },
}

function HealthPanel({ checks }: { checks: HealthCheck[] }) {
  const known = checks.filter((c) => c.state !== 'unknown')
  const passed = known.filter((c) => c.state === 'pass').length
  const score = known.length === 0 ? null : Math.round((passed / known.length) * 100)
  const tone = score === null ? null : scoreTone(score)
  return (
    <section className={PANEL} aria-label="Repository health">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className={PANEL_TITLE}>Repository health</h3>
          <p className={PANEL_NOTE}>Share of the checks below that pass for this snapshot.</p>
        </div>
        {tone && (
          <span className={`rounded-md px-2 py-0.5 text-xs font-semibold ${tone.pill}`}>{tone.label}</span>
        )}
      </div>
      <div className="mt-4 flex items-center gap-5">
        <div className="relative">
          <ScoreRing score={score ?? 0} color={tone?.ring ?? '#5c6694'} />
          <span className="absolute inset-0 flex flex-col items-center justify-center">
            <span className="text-2xl font-extrabold tabular-nums text-white">
              {score === null ? '—' : `${score}%`}
            </span>
          </span>
        </div>
        <p className="text-sm text-slate-300">
          <span className="text-lg font-bold text-white">{passed}</span> of {known.length} checks passing
          {known.length < checks.length && (
            <span className="block text-xs text-slate-500">
              {checks.length - known.length} not run yet
            </span>
          )}
        </p>
      </div>
      <ul className="mt-4 space-y-2.5">
        {checks.map((check) => {
          const { icon: Icon, className, text } = CHECK_ICONS[check.state]
          return (
            <li key={check.label} className="flex items-start gap-2.5 text-sm">
              <Icon className={`mt-0.5 h-4 w-4 shrink-0 ${className}`} aria-label={text} />
              <span>
                <span className="text-slate-200">{check.label}</span>
                <span className="block text-xs text-slate-500">{check.detail}</span>
              </span>
            </li>
          )
        })}
      </ul>
    </section>
  )
}

function KpiTile({
  icon: Icon,
  accent,
  label,
  value,
  caption,
}: {
  icon: LucideIcon
  accent: string
  label: string
  value: string
  caption: ReactNode
}) {
  return (
    <div className="flex items-start gap-3 rounded-2xl border border-slate-800 bg-slate-900/70 p-4 shadow-glow">
      <span
        aria-hidden="true"
        className={`inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br ${accent}`}
      >
        <Icon className="h-5 w-5 text-white" />
      </span>
      <div className="min-w-0">
        <p className="text-xs font-medium uppercase tracking-wider text-slate-400">{label}</p>
        <p className="text-2xl font-extrabold tabular-nums text-white">{value}</p>
        <p className="truncate text-xs text-slate-400">{caption}</p>
      </div>
    </div>
  )
}

interface Slice {
  name: string
  share: number
  files: number
  color: string
}

function languageSlices(languages: LanguageStatistic[]): Slice[] {
  const top = languages.slice(0, SERIES.length)
  const rest = languages.slice(SERIES.length)
  const slices = top.map((row, index) => ({
    name: languageLabel(row.language),
    share: row.percentage,
    files: row.file_count,
    color: SERIES[index],
  }))
  if (rest.length > 0) {
    slices.push({
      name: 'Other',
      share: rest.reduce((sum, row) => sum + row.percentage, 0),
      files: rest.reduce((sum, row) => sum + row.file_count, 0),
      color: OTHER,
    })
  }
  return slices
}

function DistributionPanel({ languages, totalFiles }: { languages: LanguageStatistic[]; totalFiles: number }) {
  const slices = languageSlices(languages)
  return (
    <section className={PANEL} aria-label="Code distribution">
      <h3 className={PANEL_TITLE}>Code distribution</h3>
      <p className={PANEL_NOTE}>Share of the repository's bytes by language.</p>
      {slices.length === 0 ? (
        <p className="mt-6 text-sm text-slate-400">Ingest the repository to see its languages.</p>
      ) : (
        <div className="mt-2 flex flex-col items-center gap-4 sm:flex-row lg:flex-col xl:flex-row">
          <div className="relative h-44 w-44 shrink-0">
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Pie
                  data={slices}
                  dataKey="share"
                  nameKey="name"
                  innerRadius="68%"
                  outerRadius="100%"
                  paddingAngle={1}
                  stroke="#121935"
                  strokeWidth={2}
                  isAnimationActive={false}
                >
                  {slices.map((slice) => (
                    <Cell key={slice.name} fill={slice.color} />
                  ))}
                </Pie>
                <Tooltip
                  formatter={(value: number) => `${value.toFixed(1)}%`}
                  contentStyle={{ background: '#121935', border: '1px solid #343c63', borderRadius: 8 }}
                  itemStyle={{ color: '#e0e7ff' }}
                />
              </PieChart>
            </ResponsiveContainer>
            <span className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
              <span className="text-2xl font-extrabold tabular-nums text-white">{number(totalFiles)}</span>
              <span className="text-[10px] font-semibold uppercase tracking-widest text-slate-400">files total</span>
            </span>
          </div>
          <ul className="w-full space-y-1.5 text-sm">
            {slices.map((slice) => (
              <li key={slice.name} className="flex items-center gap-2">
                <span aria-hidden="true" className="h-2.5 w-2.5 shrink-0 rounded-sm" style={{ background: slice.color }} />
                <span className="flex-1 truncate text-slate-200">{slice.name}</span>
                <span className="tabular-nums text-slate-400">{slice.share.toFixed(1)}%</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}

function LargestFilesPanel({
  files,
  analyzed,
  onSelectFile,
}: {
  files: FileMetric[]
  analyzed: boolean
  onSelectFile?: (path: string) => void
}) {
  const largest = largestFiles(files, 6)
  const max = largest[0]?.line_count ?? 1
  return (
    <section className={PANEL} aria-label="Largest files">
      <h3 className={PANEL_TITLE}>Largest files</h3>
      <p className={PANEL_NOTE}>Files with the most lines; large files are harder to review and change.</p>
      {!analyzed ? (
        <p className="mt-6 text-sm text-slate-400">Run "Analyze code" to measure file sizes.</p>
      ) : (
        <ul className="mt-4 space-y-3">
          {largest.map((file) => {
            const lines = file.line_count ?? 0
            const long = lines >= LONG_FILE_LINES
            const label = (
              <>
                <span className="flex items-baseline justify-between gap-3 text-sm">
                  <span className="truncate text-slate-200" title={file.path}>
                    {basename(file.path)}
                  </span>
                  <span className="shrink-0 tabular-nums text-slate-400">{number(lines)}</span>
                </span>
                <span className="mt-1 block h-2 rounded-full bg-slate-800">
                  <span
                    className={`block h-2 rounded-full ${long ? 'bg-gradient-to-r from-fuchsia-500 to-rose-400' : 'bg-gradient-to-r from-indigo-500 to-cyan-400'}`}
                    style={{ width: `${Math.max(3, (lines / max) * 100)}%` }}
                  />
                </span>
              </>
            )
            return (
              <li key={file.path}>
                {onSelectFile ? (
                  <button
                    type="button"
                    onClick={() => onSelectFile(file.path)}
                    aria-label={`Open ${file.path} (${number(lines)} lines)`}
                    className="block w-full rounded-lg text-left hover:opacity-90"
                  >
                    {label}
                  </button>
                ) : (
                  label
                )}
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

function SizeDistributionPanel({ files, analyzed }: { files: FileMetric[]; analyzed: boolean }) {
  const counted = files.filter((f) => f.line_count !== null)
  const buckets = SIZE_BUCKETS.map((bucket, index) => {
    const min = index === 0 ? 0 : SIZE_BUCKETS[index - 1].max + 1
    return {
      label: bucket.label,
      count: counted.filter((f) => (f.line_count ?? 0) >= min && (f.line_count ?? 0) <= bucket.max).length,
    }
  })
  const max = Math.max(1, ...buckets.map((b) => b.count))
  return (
    <section className={PANEL} aria-label="File size distribution">
      <h3 className={PANEL_TITLE}>File size distribution</h3>
      <p className={PANEL_NOTE}>How many analyzed files fall into each size band.</p>
      {!analyzed ? (
        <p className="mt-6 text-sm text-slate-400">Run "Analyze code" to see the distribution.</p>
      ) : (
        <ul className="mt-4 space-y-3">
          {buckets.map((bucket, index) => (
            <li key={bucket.label}>
              <span className="flex items-baseline justify-between text-sm">
                <span className="text-slate-300">{bucket.label}</span>
                <span className="tabular-nums text-slate-400">{number(bucket.count)} {bucket.count === 1 ? 'file' : 'files'}</span>
              </span>
              <span className="mt-1 block h-2 rounded-full bg-slate-800">
                <span
                  className="block h-2 rounded-full"
                  style={{
                    width: `${bucket.count === 0 ? 0 : Math.max(3, (bucket.count / max) * 100)}%`,
                    background: index === SIZE_BUCKETS.length - 1 ? '#ec4899' : SERIES[0],
                    opacity: index === SIZE_BUCKETS.length - 1 ? 1 : 0.55 + index * 0.15,
                  }}
                />
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

const SCOPES: { key: string; label: string; color: string }[] = [
  { key: 'runtime', label: 'Runtime', color: SERIES[0] },
  { key: 'dev', label: 'Development', color: SERIES[1] },
  { key: 'other', label: 'Peer / optional', color: SERIES[2] },
]

function DependencyPanel({ inventory }: { inventory: DependencyInventory | undefined }) {
  const ecosystems = (['npm', 'python'] as const).map((ecosystem) => {
    const counts: Record<string, number> = { runtime: 0, dev: 0, other: 0 }
    for (const manifest of inventory?.manifests ?? []) {
      if (manifest.ecosystem !== ecosystem) continue
      for (const dependency of manifest.dependencies) {
        const key = dependency.scope === 'runtime' || dependency.scope === 'dev' ? dependency.scope : 'other'
        counts[key] += 1
      }
    }
    return { ecosystem, counts, total: counts.runtime + counts.dev + counts.other }
  })
  const max = Math.max(1, ...ecosystems.map((e) => e.total))
  return (
    <section className={PANEL} aria-label="Dependencies by ecosystem">
      <h3 className={PANEL_TITLE}>Dependencies by ecosystem</h3>
      <p className={PANEL_NOTE}>Declared dependencies from package.json, pyproject.toml and requirements files.</p>
      {!inventory ? (
        <p className="mt-6 text-sm text-slate-400">Dependency inventory not available yet.</p>
      ) : (
        <>
          <ul className="mt-4 space-y-4">
            {ecosystems.map(({ ecosystem, counts, total }) => (
              <li key={ecosystem}>
                <span className="flex items-baseline justify-between text-sm">
                  <span className="font-medium text-slate-200">{ecosystem === 'npm' ? 'npm' : 'Python'}</span>
                  <span className="tabular-nums text-slate-400">{number(total)} declared</span>
                </span>
                <span
                  className="mt-1 flex h-3 gap-0.5 overflow-hidden rounded-full bg-slate-800"
                  style={{ width: `${total === 0 ? 100 : Math.max(6, (total / max) * 100)}%` }}
                >
                  {SCOPES.map((scope) =>
                    counts[scope.key] > 0 ? (
                      <span
                        key={scope.key}
                        title={`${scope.label}: ${counts[scope.key]}`}
                        style={{ flexGrow: counts[scope.key], background: scope.color }}
                      />
                    ) : null,
                  )}
                </span>
              </li>
            ))}
          </ul>
          <ul className="mt-4 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-400">
            {SCOPES.map((scope) => (
              <li key={scope.key} className="flex items-center gap-1.5">
                <span aria-hidden="true" className="h-2.5 w-2.5 rounded-sm" style={{ background: scope.color }} />
                {scope.label}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  )
}

function ActivityPanel({ projectId, repositoryId }: { projectId: string; repositoryId: string }) {
  // Same key as useJobs, so this shares the job list (and its polling) instead of fetching again.
  const jobs = useQuery({
    queryKey: queryKeys.jobs(projectId, repositoryId),
    queryFn: () => listJobs(projectId, repositoryId),
  })
  const list = jobs.data ?? []
  const completed = list.filter((job) => job.status === 'completed').length
  const failed = list.filter((job) => job.status === 'failed').length
  const durations = list
    .filter((job) => job.status === 'completed' && job.started_at && job.completed_at)
    .map((job) => (new Date(job.completed_at as string).getTime() - new Date(job.started_at as string).getTime()) / 1000)
  const average = durations.length ? durations.reduce((a, b) => a + b, 0) / durations.length : null
  const stats = [
    { label: 'Jobs run', value: number(list.length) },
    { label: 'Succeeded', value: number(completed) },
    { label: 'Failed', value: number(failed) },
    { label: 'Average run', value: average === null ? '—' : `${average < 10 ? average.toFixed(1) : Math.round(average)} s` },
  ]
  return (
    <section className={PANEL} aria-label="Job activity">
      <h3 className={PANEL_TITLE}>Job activity</h3>
      <p className={PANEL_NOTE}>Ingestion and analysis jobs for this repository.</p>
      <dl className="mt-4 grid grid-cols-2 gap-3">
        {stats.map((stat) => (
          <div key={stat.label} className="rounded-xl border border-slate-800 bg-slate-950/40 p-3">
            <dt className="text-xs text-slate-400">{stat.label}</dt>
            <dd className="text-xl font-bold tabular-nums text-white">{stat.value}</dd>
          </div>
        ))}
      </dl>
      {list.length > 0 && (
        <div className="mt-4" aria-hidden="true">
          <div className="flex h-2 gap-0.5 overflow-hidden rounded-full bg-slate-800">
            {completed > 0 && <span style={{ flexGrow: completed }} className="bg-emerald-500" />}
            {failed > 0 && <span style={{ flexGrow: failed }} className="bg-rose-500" />}
            {list.length - completed - failed > 0 && (
              <span style={{ flexGrow: list.length - completed - failed }} className="bg-amber-500" />
            )}
          </div>
        </div>
      )}
    </section>
  )
}

/**
 * At-a-glance dashboard for one repository. It adds no requests of its own: the tree and
 * languages come from the page (which loads them before the repository resolves), and the other
 * queries are shared by key with the panels below, which mount in the same render.
 */
export function RepositoryDashboard({
  projectId,
  repositoryId,
  repository,
  tree,
  languages,
  onSelectFile,
}: {
  projectId: string
  repositoryId: string
  repository: Repository
  tree: RepositoryTree | undefined
  languages: RepositoryLanguages | undefined
  onSelectFile?: (path: string) => void
}) {
  const metrics = useCodeMetrics(projectId, repositoryId)
  const inventory = useDependencies(projectId, repositoryId)
  const scan = useSecretScan(projectId, repositoryId)

  const summary = metrics.data?.summary ?? null
  const languageRows = languages?.languages ?? []
  const namedLanguages = languageRows.filter((row) => row.language !== null)
  const findings = scan.data?.summary.findings
  const checks = healthChecks(repository, metrics.data, inventory.data, scan.data, { metrics, inventory, scan })

  return (
    <section aria-label="Repository dashboard" className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
        <KpiTile
          icon={Files}
          accent="from-indigo-500 to-violet-500"
          label="Files"
          value={tree ? number(tree.file_count) : '—'}
          caption={tree ? `${formatBytes(tree.total_bytes)} stored` : 'Not ingested yet'}
        />
        <KpiTile
          icon={FileCode2}
          accent="from-cyan-500 to-sky-500"
          label="Lines of code"
          value={summary ? number(summary.total_lines - summary.blank_lines) : '—'}
          caption={
            summary
              ? `${number(summary.total_lines)} lines in total`
              : metrics.data
                ? 'Run "Analyze code"'
                : waiting(metrics, 'Loading…')
          }
        />
        <KpiTile
          icon={Languages}
          accent="from-fuchsia-500 to-pink-500"
          label="Languages"
          value={languages ? number(namedLanguages.length) : '—'}
          caption={
            namedLanguages[0]
              ? `Mostly ${namedLanguages[0].language} (${namedLanguages[0].percentage.toFixed(0)}%)`
              : 'No languages detected'
          }
        />
        <KpiTile
          icon={Package}
          accent="from-amber-500 to-orange-500"
          label="Dependencies"
          value={inventory.data ? number(inventory.data.summary.dependencies) : '—'}
          caption={
            inventory.data
              ? `From ${number(inventory.data.summary.manifests)} manifest${inventory.data.summary.manifests === 1 ? '' : 's'}`
              : waiting(inventory, 'Checking…')
          }
        />
        <KpiTile
          icon={findings ? ShieldAlert : ShieldCheck}
          accent={findings ? 'from-rose-500 to-fuchsia-500' : 'from-emerald-500 to-teal-500'}
          label="Secret findings"
          value={findings === undefined ? '—' : number(findings)}
          caption={
            findings === undefined
              ? waiting(scan, 'Scanning…')
              : findings === 0
                ? 'None found'
                : 'See Secrets below'
          }
        />
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <HealthPanel checks={checks} />
        <DistributionPanel languages={languageRows} totalFiles={languages?.total_files ?? 0} />
        <LargestFilesPanel files={metrics.data?.files ?? []} analyzed={summary !== null} onSelectFile={onSelectFile} />
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <SizeDistributionPanel files={metrics.data?.files ?? []} analyzed={summary !== null} />
        <DependencyPanel inventory={inventory.data} />
        <ActivityPanel projectId={projectId} repositoryId={repositoryId} />
      </div>
    </section>
  )
}
