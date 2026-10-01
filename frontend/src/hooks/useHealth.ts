import { useQuery } from '@tanstack/react-query'

import { getHealth } from '../services/health'
import { queryKeys } from './queryKeys'

export function useHealth() {
  return useQuery({
    queryKey: queryKeys.health,
    queryFn: getHealth,
    refetchInterval: 30_000,
    retry: false,
  })
}
