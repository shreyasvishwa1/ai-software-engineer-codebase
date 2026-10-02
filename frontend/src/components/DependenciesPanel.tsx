import { useState } from 'react'

import { useDependencies } from '../hooks/useDependencies'
import { getErrorMessage } from '../services/client'
import type { DependencyManifest, DependencySummary, ManifestStatus } from '../types/api'
import { ErrorMessage } from './ErrorMessage'

const NOTE = 'text-sm text-slate-400'
export const TRUNCATED_NOTICE =
  'Showing a limited set (at most 50 manifests and 5,000 dependencies).'

const SUMMARY_ROWS: [keyof DependencySummary, string][] = [
  ['manifests', 'Manifests'],
  ['parsed', 'Parsed'],
  ['invalid', 'Invalid'],
  ['too_large', 'Too large'],
  ['unavailable', 'Unavailable'],
  ['dependencies', 'Dependencies'],
]

const STATUS_MESSAGES: Record<Exclude<ManifestStatus, 'parsed'>, string> = {
  invalid: 'Could not be parsed.',
  too_large: 'Too large to analyze (limit 1 MiB).',
  unavailable: 'Not available right now; try again later.',
}

const ECOSYSTEMS: Record<DependencyManifest['ecosystem'], string> = {
  npm: 'npm',
  python: 'Python',
}

function count(manifest: DependencyManifest): string {
  const n = manifest.dependencies.length
  return `${n} ${n === 1 ? 'dependency' : 'dependencies'}`
}

/** One manifest; its dependency table is rendered only while expanded. */
function ManifestRow({
  manifest,
  onSelectFile,
}: {
  manifest: DependencyManifest
  onSelectFile?: (path: string) => void
}) {
  const [open, setOpen] = useState(false)
  // Separate from the expand toggle (never nested in it); opens the file in the viewer.
  const viewButton = onSelectFile && (
    <button
      type="button"
      aria-label={`Open ${manifest.path} in the viewer`}
      onClick={() => onSelectFile(manifest.path)}
      className="shrink-0 rounded border border-slate-700 px-2 py-0.5 text-xs text-slate-200 hover:bg-slate-800"
    >
      View file
    </button>
  )
  const details = (
    <>
      <span className="break-all font-mono text-xs text-slate-200">{manifest.path}</span>
      <span className="text-xs text-slate-400">{ECOSYSTEMS[manifest.ecosystem] ?? manifest.ecosystem}</span>
      <span className="text-xs text-slate-400">{manifest.status}</span>
      <span className="ml-auto text-xs text-slate-500">{count(manifest)}</span>
    </>
  )
  if (manifest.status !== 'parsed') {
    return (
      <li className="space-y-1 py-1">
        <div className="flex flex-wrap items-center gap-2 px-1">
          {details}
          {viewButton}
        </div>
        <p className="px-1 text-xs text-slate-500">{STATUS_MESSAGES[manifest.status]}</p>
      </li>
    )
  }
  const toggle = (
    <button
      type="button"
      aria-expanded={open}
      onClick={() => setOpen((value) => !value)}
      className="flex w-full flex-wrap items-center gap-2 rounded px-1 py-0.5 text-left hover:bg-slate-800"
    >
      <span aria-hidden="true" className="text-slate-400">
        {open ? '▾' : '▸'}
      </span>
      {details}
    </button>
  )
  return (
    <li className="py-1">
      {viewButton ? (
        <div className="flex items-center gap-2">
          {toggle}
          {viewButton}
        </div>
      ) : (
        toggle
      )}
      {open && (
        <table className="ml-5 mt-1 w-[calc(100%-1.25rem)] text-xs" aria-label={`Dependencies of ${manifest.path}`}>
          <thead>
            <tr className="text-left text-slate-500">
              <th className="py-1 font-normal">Name</th>
              <th className="py-1 font-normal">Specifier</th>
              <th className="py-1 font-normal">Scope</th>
            </tr>
          </thead>
          <tbody>
            {manifest.dependencies.map((dependency, index) => (
              <tr key={`${dependency.scope}:${dependency.name}:${index}`} className="border-t border-slate-800">
                <td className="break-all py-1 font-mono text-slate-200">{dependency.name}</td>
                <td className="break-all py-1 font-mono text-slate-300">{dependency.specifier}</td>
                <td className="py-1 text-slate-400">{dependency.scope}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </li>
  )
}

/** Dependencies declared in the repository's manifests. All server values are plain text. */
export function DependenciesPanel({
  projectId,
  repositoryId,
  onSelectFile,
}: {
  projectId: string
  repositoryId: string
  /** When given, every manifest gets a "View file" button that opens it in the viewer. */
  onSelectFile?: (path: string) => void
}) {
  const inventory = useDependencies(projectId, repositoryId)
  if (inventory.isPending) {
    return <p className={NOTE}>Loading dependencies…</p>
  }
  if (inventory.isError) {
    return <ErrorMessage message={getErrorMessage(inventory.error, 'Could not load dependencies')} />
  }
  const { manifests, summary, truncated } = inventory.data
  if (manifests.length === 0) {
    return <p className={NOTE}>No dependency manifests found.</p>
  }
  return (
    <div className="space-y-4">
      <table className="w-full text-sm" aria-label="Dependency summary">
        <tbody>
          {SUMMARY_ROWS.map(([key, label]) => (
            <tr key={key} className="border-b border-slate-800 last:border-0">
              <th scope="row" className="py-1 text-left font-normal text-slate-400">
                {label}
              </th>
              <td className="py-1 text-right text-slate-200">{summary[key]}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {truncated && <p className={NOTE}>{TRUNCATED_NOTICE}</p>}
      <ul className="divide-y divide-slate-800" aria-label="Manifests">
        {manifests.map((manifest) => (
          <ManifestRow key={manifest.path} manifest={manifest} onSelectFile={onSelectFile} />
        ))}
      </ul>
    </div>
  )
}
