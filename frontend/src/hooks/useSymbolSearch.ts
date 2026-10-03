import { useMutation } from '@tanstack/react-query'

import { searchSymbols } from '../services/symbolSearch'

/** Search Python symbols on demand only; results are never cached or stored. */
export function useSymbolSearch(projectId: string, repositoryId: string) {
  return useMutation({
    mutationFn: (query: string) => searchSymbols(projectId, repositoryId, query),
  })
}
