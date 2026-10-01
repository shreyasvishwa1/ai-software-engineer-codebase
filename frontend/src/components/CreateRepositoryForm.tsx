import { useState, type FormEvent } from 'react'

import { useCreateRepository } from '../hooks/useRepository'
import { getErrorMessage } from '../services/client'
import type { RepositoryCreate, RepositorySourceType } from '../types/api'
import { ErrorMessage } from './ErrorMessage'

export function CreateRepositoryForm({ projectId }: { projectId: string }) {
  const createRepository = useCreateRepository(projectId)
  const [name, setName] = useState('')
  const [sourceType, setSourceType] = useState<RepositorySourceType>('zip')
  const [sourceUrl, setSourceUrl] = useState('')
  const [defaultBranch, setDefaultBranch] = useState('')
  const [error, setError] = useState<string | null>(null)

  async function onSubmit(event: FormEvent) {
    event.preventDefault()
    const body: RepositoryCreate = { name: name.trim(), source_type: sourceType }
    if (!body.name) {
      setError('Repository name is required')
      return
    }
    if (sourceType === 'github') {
      if (!sourceUrl.trim()) {
        setError('GitHub repository URL is required')
        return
      }
      body.source_url = sourceUrl.trim()
      if (defaultBranch.trim()) {
        body.default_branch = defaultBranch.trim()
      }
    }
    setError(null)
    try {
      await createRepository.mutateAsync(body)
      setName('')
      setSourceUrl('')
      setDefaultBranch('')
    } catch (err) {
      setError(getErrorMessage(err, 'Could not create the repository'))
    }
  }

  return (
    <form onSubmit={onSubmit} className="space-y-3" aria-label="Create repository">
      <h2 className="text-sm font-semibold text-slate-200">New repository</h2>
      <label className="block text-sm">
        <span className="text-slate-400">Name</span>
        <input
          className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <label className="block text-sm">
        <span className="text-slate-400">Source</span>
        <select
          className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2"
          value={sourceType}
          onChange={(e) => setSourceType(e.target.value as RepositorySourceType)}
        >
          <option value="zip">ZIP upload</option>
          <option value="github">Public GitHub repository</option>
        </select>
      </label>
      {sourceType === 'github' && (
        <>
          <label className="block text-sm">
            <span className="text-slate-400">GitHub URL (https://github.com/owner/repository)</span>
            <input
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2"
              value={sourceUrl}
              onChange={(e) => setSourceUrl(e.target.value)}
            />
          </label>
          <label className="block text-sm">
            <span className="text-slate-400">Branch (optional)</span>
            <input
              className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2"
              value={defaultBranch}
              onChange={(e) => setDefaultBranch(e.target.value)}
            />
          </label>
        </>
      )}
      <ErrorMessage message={error} />
      <button
        type="submit"
        disabled={createRepository.isPending}
        className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
      >
        {createRepository.isPending ? 'Creating…' : 'Create repository'}
      </button>
    </form>
  )
}
