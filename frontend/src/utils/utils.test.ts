import { describe, expect, it } from 'vitest'

import type { AnalysisJob } from '../types/api'
import { formatBytes, formatDateTime } from './format'
import { isJobActive, JOB_POLL_INTERVAL_MS, jobRefetchInterval } from './jobs'

function job(status: AnalysisJob['status']): AnalysisJob {
  return {
    id: 'j',
    project_id: 'p',
    repository_id: 'r',
    job_type: 'ingestion',
    status,
    progress: 0,
    error_message: null,
    created_at: '2026-10-01T00:00:00Z',
    started_at: null,
    completed_at: null,
  }
}

describe('formatBytes', () => {
  it('formats sizes', () => {
    expect(formatBytes(0)).toBe('0 B')
    expect(formatBytes(512)).toBe('512 B')
    expect(formatBytes(1536)).toBe('1.5 KB')
    expect(formatBytes(5 * 1024 * 1024)).toBe('5.0 MB')
  })
})

describe('formatDateTime', () => {
  it('handles missing and invalid values', () => {
    expect(formatDateTime(null)).toBe('—')
    expect(formatDateTime('not a date')).toBe('—')
    expect(formatDateTime('2026-10-01T00:00:00Z')).not.toBe('—')
  })
})

describe('job polling interval', () => {
  it('polls only while the latest job is pending or processing', () => {
    expect(jobRefetchInterval([job('pending')])).toBe(JOB_POLL_INTERVAL_MS)
    expect(jobRefetchInterval([job('processing'), job('failed')])).toBe(JOB_POLL_INTERVAL_MS)
    expect(jobRefetchInterval([job('completed'), job('processing')])).toBe(false)
    expect(jobRefetchInterval([job('failed')])).toBe(false)
    expect(jobRefetchInterval([])).toBe(false)
    expect(jobRefetchInterval(undefined)).toBe(false)
    expect(jobRefetchInterval([job('processing')], 25)).toBe(25)
    expect(isJobActive(undefined)).toBe(false)
  })
})
