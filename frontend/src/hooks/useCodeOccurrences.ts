import { useMutation } from '@tanstack/react-query'

import type { CodePosition } from '../services/codeDefinition'
import { getCodeOccurrences } from '../services/codeOccurrences'

/** Find a name's occurrences on demand only: every click requests afresh; nothing is cached. */
export function useCodeOccurrences(projectId: string, repositoryId: string) {
  return useMutation({
    mutationFn: ({ path, position }: { path: string; position: CodePosition }) =>
      getCodeOccurrences(projectId, repositoryId, path, position),
  })
}
