import type { TokenResponse, User } from '../types/api'
import { apiClient } from './client'

export async function register(email: string, password: string): Promise<User> {
  const { data } = await apiClient.post<User>('/auth/register', { email, password })
  return data
}

export async function login(email: string, password: string): Promise<TokenResponse> {
  const { data } = await apiClient.post<TokenResponse>('/auth/login', { email, password })
  return data
}

export async function getCurrentUser(): Promise<User> {
  const { data } = await apiClient.get<User>('/auth/me')
  return data
}
