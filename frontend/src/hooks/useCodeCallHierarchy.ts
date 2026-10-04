import { useMutation } from '@tanstack/react-query'

import { getCodeCallHierarchy } from '../services/codeCallHierarchy'
import type { DefinitionLocation } from '../types/api'

/** Find a definition's call hierarchy on demand only; results are never cached or stored. */
export function useCodeCallHierarchy(projectId: string, repositoryId: string) {
  return useMutation({
    mutationFn: (definition: DefinitionLocation) =>
      getCodeCallHierarchy(projectId, repositoryId, definition.path, definition.line),
  })
}
