import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

import type { LanguageStatistic } from '../types/api'
import { formatBytes } from '../utils/format'

const UNKNOWN = 'Unknown'

export function languageLabel(language: string | null): string {
  return language ?? UNKNOWN
}

export function LanguageStats({ languages }: { languages: LanguageStatistic[] }) {
  if (languages.length === 0) {
    return <p className="text-sm text-slate-400">No language statistics yet.</p>
  }
  const chartData = languages.map((row) => ({
    language: languageLabel(row.language),
    percentage: row.percentage,
  }))
  return (
    <section aria-label="Language statistics" className="space-y-4">
      <div className="h-56">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={chartData} layout="vertical" margin={{ left: 24 }}>
            <XAxis type="number" unit="%" stroke="#94a3b8" />
            <YAxis type="category" dataKey="language" width={110} stroke="#94a3b8" />
            <Tooltip />
            <Bar dataKey="percentage" fill="#6366f1" />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <table className="w-full text-left text-sm">
        <thead className="text-xs text-slate-400">
          <tr>
            <th className="py-1">Language</th>
            <th className="py-1">Files</th>
            <th className="py-1">Size</th>
            <th className="py-1">Share</th>
          </tr>
        </thead>
        <tbody>
          {languages.map((row) => (
            <tr key={languageLabel(row.language)} className="border-t border-slate-800">
              <td className="py-1">{languageLabel(row.language)}</td>
              <td className="py-1">{row.file_count}</td>
              <td className="py-1">{formatBytes(row.total_bytes)}</td>
              <td className="py-1">{row.percentage.toFixed(2)}%</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}
