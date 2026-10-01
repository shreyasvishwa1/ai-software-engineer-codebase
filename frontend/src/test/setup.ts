import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

import { setUnauthorizedHandler } from '../services/session'
import { resetApiMock } from './mockApi'

// Recharts' ResponsiveContainer needs ResizeObserver, which jsdom does not provide.
class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
globalThis.ResizeObserver ??= ResizeObserverStub as unknown as typeof ResizeObserver

afterEach(() => {
  cleanup()
  resetApiMock()
  setUnauthorizedHandler(null)
  window.sessionStorage.clear()
})
