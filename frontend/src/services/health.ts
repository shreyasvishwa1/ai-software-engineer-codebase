import type { Health } from '../types/api'
import { apiClient } from './client'

export async function getHealth(): Promise<Health> {
  const { data } = await apiClient.get<Health>('/health')
  return data
}
