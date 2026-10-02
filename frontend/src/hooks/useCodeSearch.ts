import { useMutation } from '@tanstack/react-query'

import { searchCode } from '../services/codeSearch'

/** Search the repository on demand only; results are never cached or stored. */
export function useCodeSearch(projectId: string, repositoryId: string) {
  return useMutation({
    mutationFn: (query: string) => searchCode(projectId, repositoryId, query),
  })
}
