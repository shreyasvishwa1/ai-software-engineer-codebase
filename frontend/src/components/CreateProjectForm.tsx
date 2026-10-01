import { useState, type FormEvent } from 'react'

import { useCreateProject } from '../hooks/useProjects'
import { getErrorMessage } from '../services/client'
import { ErrorMessage } from './ErrorMessage'

const MAX_NAME_LENGTH = 100

export function CreateProjectForm() {
  const createProject = useCreateProject()
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [error, setError] = useState<string | null>(null)

  async function onSubmit(event: FormEvent) {
    event.preventDefault()
    const trimmed = name.trim()
    if (!trimmed) {
      setError('Project name is required')
      return
    }
    if (trimmed.length > MAX_NAME_LENGTH) {
      setError(`Project name must be at most ${MAX_NAME_LENGTH} characters`)
      return
    }
    setError(null)
    try {
      await createProject.mutateAsync({ name: trimmed, description: description.trim() || null })
      setName('')
      setDescription('')
    } catch (err) {
      setError(getErrorMessage(err, 'Could not create the project'))
    }
  }

  return (
    <form onSubmit={onSubmit} className="space-y-3" aria-label="Create project">
      <h2 className="text-sm font-semibold text-slate-200">New project</h2>
      <label className="block text-sm">
        <span className="text-slate-400">Name</span>
        <input
          className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <label className="block text-sm">
        <span className="text-slate-400">Description (optional)</span>
        <textarea
          className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
      </label>
      <ErrorMessage message={error} />
      <button
        type="submit"
        disabled={createProject.isPending}
        className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
      >
        {createProject.isPending ? 'Creating…' : 'Create project'}
      </button>
    </form>
  )
}
