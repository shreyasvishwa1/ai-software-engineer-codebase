import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { TreeDirectory } from '../types/api'
import { FileTree } from './FileTree'

const MALICIOUS = '<img src=x onerror=alert(1)>.py'

const TREE: TreeDirectory = {
  type: 'directory',
  name: '',
  path: '',
  children: [
    {
      type: 'directory',
      name: 'src',
      path: 'src',
      children: [
        {
          type: 'directory',
          name: 'utils',
          path: 'src/utils',
          children: [
            {
              type: 'file',
              name: 'helper.py',
              path: 'src/utils/helper.py',
              extension: 'py',
              language: 'Python',
              size_bytes: 22,
            },
          ],
        },
        {
          type: 'file',
          name: 'app.py',
          path: 'src/app.py',
          extension: 'py',
          language: 'Python',
          size_bytes: 2048,
        },
      ],
    },
    {
      type: 'file',
      name: MALICIOUS,
      path: MALICIOUS,
      extension: 'py',
      language: 'Python',
      size_bytes: 5,
    },
    {
      type: 'file',
      name: 'LICENSE',
      path: 'LICENSE',
      extension: null,
      language: null,
      size_bytes: 10,
    },
  ],
}

describe('FileTree', () => {
  it('renders top-level entries in server order with language and size', () => {
    render(<FileTree tree={TREE} />)
    const list = screen.getByRole('navigation', { name: 'File tree' }).querySelector(':scope > ul')
    const top = Array.from(list?.children ?? []) // top-level entries only
    expect(top.map((item) => item.textContent)).toEqual([
      expect.stringContaining('src'),
      expect.stringContaining(MALICIOUS),
      expect.stringContaining('LICENSE'),
    ])
    expect(screen.getByText('LICENSE').parentElement?.textContent).toContain('Unknown')
    expect(screen.getByText('10 B')).toBeTruthy()
  })

  it('expands and collapses directories', () => {
    render(<FileTree tree={TREE} />)
    const src = screen.getByRole('button', { name: /src/ })
    expect(src.getAttribute('aria-expanded')).toBe('true') // top level is open
    expect(screen.getByText('app.py')).toBeTruthy()
    expect(screen.getByText('2.0 KB')).toBeTruthy()
    const utils = screen.getByRole('button', { name: /utils/ })
    expect(utils.getAttribute('aria-expanded')).toBe('false') // nested start collapsed
    expect(screen.queryByText('helper.py')).toBeNull()
    fireEvent.click(utils)
    expect(screen.getByText('helper.py')).toBeTruthy()
    fireEvent.click(src)
    expect(screen.queryByText('app.py')).toBeNull()
  })

  it('renders file names as text, never as HTML', () => {
    const { container } = render(<FileTree tree={TREE} />)
    expect(screen.getByText(MALICIOUS)).toBeTruthy()
    expect(container.querySelector('img')).toBeNull()
  })

  it('shows an empty state', () => {
    render(<FileTree tree={{ type: 'directory', name: '', path: '', children: [] }} />)
    expect(screen.getByText('No files have been ingested yet.')).toBeTruthy()
  })
})
