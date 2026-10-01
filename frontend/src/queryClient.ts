import { QueryClient } from '@tanstack/react-query'
import axios from 'axios'

/** Retry transient (network/5xx) failures once; never retry 4xx responses. */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: (failureCount, error) => {
          const status = axios.isAxiosError(error) ? error.response?.status : undefined
          return failureCount < 1 && (status === undefined || status >= 500)
        },
        refetchOnWindowFocus: false,
      },
    },
  })
}
