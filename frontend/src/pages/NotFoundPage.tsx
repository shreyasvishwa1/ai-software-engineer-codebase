import { Link } from 'react-router-dom'

export function NotFoundPage() {
  return (
    <div className="mx-auto mt-24 max-w-sm space-y-4 px-6 text-center">
      <h1 className="text-2xl font-bold">Page not found</h1>
      <Link to="/projects" className="text-indigo-300 hover:underline">
        Go to projects
      </Link>
    </div>
  )
}
