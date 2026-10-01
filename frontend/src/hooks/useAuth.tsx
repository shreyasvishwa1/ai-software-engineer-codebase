import { useQueryClient } from '@tanstack/react-query'
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'

import * as authApi from '../services/auth'
import { clearToken, getToken, setToken, setUnauthorizedHandler } from '../services/session'
import type { User } from '../types/api'

type AuthStatus = 'loading' | 'authenticated' | 'anonymous'

interface AuthContextValue {
  status: AuthStatus
  user: User | null
  login: (email: string, password: string) => Promise<void>
  register: (email: string, password: string) => Promise<void>
  logout: () => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()
  const [user, setUser] = useState<User | null>(null)
  const [status, setStatus] = useState<AuthStatus>(() => (getToken() ? 'loading' : 'anonymous'))

  const signOut = useCallback(() => {
    clearToken()
    setUser(null)
    setStatus('anonymous')
    queryClient.clear()
  }, [queryClient])

  // Any 401 from the API ends the session; protected routes then redirect to /login.
  useEffect(() => {
    setUnauthorizedHandler(signOut)
    return () => setUnauthorizedHandler(null)
  }, [signOut])

  // Validate a token restored from sessionStorage.
  useEffect(() => {
    if (!getToken()) {
      return
    }
    let cancelled = false
    authApi
      .getCurrentUser()
      .then((current) => {
        if (!cancelled) {
          setUser(current)
          setStatus('authenticated')
        }
      })
      .catch(() => {
        if (!cancelled) {
          signOut()
        }
      })
    return () => {
      cancelled = true
    }
  }, [signOut])

  const login = useCallback(async (email: string, password: string) => {
    const { access_token } = await authApi.login(email, password)
    setToken(access_token)
    const current = await authApi.getCurrentUser()
    setUser(current)
    setStatus('authenticated')
  }, [])

  const register = useCallback(
    async (email: string, password: string) => {
      await authApi.register(email, password)
      await login(email, password)
    },
    [login],
  )

  const value = useMemo(
    () => ({ status, user, login, register, logout: signOut }),
    [status, user, login, register, signOut],
  )
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext)
  if (!context) {
    throw new Error('useAuth must be used inside <AuthProvider>')
  }
  return context
}
