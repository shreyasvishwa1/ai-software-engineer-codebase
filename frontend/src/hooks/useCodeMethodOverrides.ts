import { useMutation } from '@tanstack/react-query'

import { getCodeMethodOverrides } from '../services/codeMethodOverrides'
import type { DefinitionLocation } from '../types/api'

/** Find a method's direct overrides on demand only; results are never cached or stored. */
export function useCodeMethodOverrides(projectId: string, repositoryId: string) {
  return useMutation({
    mutationFn: (definition: DefinitionLocation) =>
      getCodeMethodOverrides(projectId, repositoryId, definition.path, definition.line),
  })
}
