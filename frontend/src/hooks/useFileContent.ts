import { useQuery } from '@tanstack/react-query'

import { getFileContent } from '../services/fileContent'
import { queryKeys } from './queryKeys'

/** The selected file's content; no request is made while no file is selected. */
export function useFileContent(
  projectId: string,
  repositoryId: string,
  selectedPath: string | null | undefined,
) {
  return useQuery({
    queryKey: queryKeys.fileContent(projectId, repositoryId, selectedPath ?? ''),
    queryFn: () => getFileContent(projectId, repositoryId, selectedPath as string),
    enabled: Boolean(selectedPath),
  })
}
