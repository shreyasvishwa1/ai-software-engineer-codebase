import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { LanguageStats } from './LanguageStats'

describe('LanguageStats', () => {
  it('lists languages with counts, sizes and percentages; null is Unknown', () => {
    render(
      <LanguageStats
        languages={[
          { language: 'Python', file_count: 2, total_bytes: 1100, percentage: 89.14 },
          { language: '<b>Evil</b>', file_count: 1, total_bytes: 100, percentage: 8.1 },
          { language: null, file_count: 1, total_bytes: 34, percentage: 2.76 },
        ]}
      />,
    )
    const rows = within(screen.getByRole('table'))
      .getAllByRole('row')
      .slice(1) as HTMLTableRowElement[]
    expect(rows.map((row) => Array.from(row.cells).map((cell) => cell.textContent))).toEqual([
      ['Python', '2', '1.1 KB', '89.14%'],
      ['<b>Evil</b>', '1', '100 B', '8.10%'],
      ['Unknown', '1', '34 B', '2.76%'],
    ])
    expect(document.querySelector('b')).toBeNull() // rendered as text
  })

  it('shows an empty state', () => {
    render(<LanguageStats languages={[]} />)
    expect(screen.getByText('No language statistics yet.')).toBeTruthy()
  })
})
