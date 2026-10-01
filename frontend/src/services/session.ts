// The access token lives only in sessionStorage (cleared when the tab closes). It is never
// logged, rendered or placed in a URL.

const TOKEN_KEY = 'ai_se_access_token'

export function getToken(): string | null {
  try {
    return window.sessionStorage.getItem(TOKEN_KEY)
  } catch {
    return null
  }
}

export function setToken(token: string): void {
  try {
    window.sessionStorage.setItem(TOKEN_KEY, token)
  } catch {
    // Storage unavailable (e.g. blocked): the session simply does not persist.
  }
}

export function clearToken(): void {
  try {
    window.sessionStorage.removeItem(TOKEN_KEY)
  } catch {
    // Nothing to clear.
  }
}

type UnauthorizedHandler = () => void
let unauthorizedHandler: UnauthorizedHandler | null = null

/** Registered by the auth provider; called after any 401 response. */
export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  unauthorizedHandler = handler
}

export function notifyUnauthorized(): void {
  unauthorizedHandler?.()
}
