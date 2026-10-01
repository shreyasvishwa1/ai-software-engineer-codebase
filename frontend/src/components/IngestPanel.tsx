import { useState, type FormEvent } from 'react'

import { useIngest } from '../hooks/useRepository'
import { getErrorMessage } from '../services/client'
import type { Repository } from '../types/api'
import { ErrorMessage } from './ErrorMessage'

export function IngestPanel({ repository }: { repository: Repository }) {
  const ingest = useIngest(repository.project_id, repository.id)
  const [file, setFile] = useState<File | null>(null)
  const [uploadPercent, setUploadPercent] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [queuedJobId, setQueuedJobId] = useState<string | null>(null)
  const isGithub = repository.source_type === 'github'

  async function start(selected?: File) {
    setError(null)
    setQueuedJobId(null)
    setUploadPercent(selected ? 0 : null)
    try {
      const job = await ingest.mutateAsync({ file: selected, onProgress: setUploadPercent })
      setQueuedJobId(job.job_id)
      setFile(null)
    } catch (err) {
      setError(getErrorMessage(err, 'Could not start ingestion'))
    } finally {
      setUploadPercent(null)
    }
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault()
    if (isGithub) {
      void start()
    } else if (file) {
      void start(file)
    } else {
      setError('Choose a ZIP archive to upload')
    }
  }

  return (
    <form onSubmit={onSubmit} className="space-y-3" aria-label="Ingest repository">
      <h2 className="text-sm font-semibold text-slate-200">Ingestion</h2>
      {isGithub ? (
        <p className="text-sm text-slate-400">
          Downloads the public archive for the repository URL shown above.
        </p>
      ) : (
        <label className="block text-sm">
          <span className="text-slate-400">ZIP archive</span>
          <input
            type="file"
            accept=".zip,application/zip"
            className="mt-1 block w-full text-sm text-slate-300"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
        </label>
      )}
      {uploadPercent !== null && (
        <div aria-label="Upload progress" className="text-xs text-slate-400">
          Uploading… {uploadPercent}%
          <div className="mt-1 h-1.5 rounded bg-slate-800">
            <div className="h-1.5 rounded bg-indigo-500" style={{ width: `${uploadPercent}%` }} />
          </div>
        </div>
      )}
      <ErrorMessage message={error} />
      {queuedJobId && (
        <p className="text-sm text-emerald-300" role="status">
          Ingestion queued.
        </p>
      )}
      <button
        type="submit"
        disabled={ingest.isPending}
        className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
      >
        {isGithub ? 'Ingest from GitHub' : 'Upload and ingest'}
      </button>
    </form>
  )
}
