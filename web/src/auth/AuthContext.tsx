import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { api, clearStoredToken, getStoredToken, storeToken } from '../api/client'
interface AuthState {
  isAuthenticated: boolean
  login: () => void
  logout: () => void
}
const AuthContext = createContext<AuthState | null>(null)
function captureSessionFromRedirect(): boolean {
  const params = new URLSearchParams(window.location.search)
  const token = params.get('session_token')
  if (!token) return false
  storeToken(token)
  window.history.replaceState({}, '', window.location.pathname + window.location.hash)
  return true
}
export function AuthProvider({ children }: { children: ReactNode }) {
  const [isAuthenticated, setIsAuthenticated] = useState(
    () => captureSessionFromRedirect() || getStoredToken() !== null,
  )
  useEffect(() => {
    const interval = window.setInterval(() => {
      const stillHasToken = getStoredToken() !== null
      setIsAuthenticated((current) => (current !== stillHasToken ? stillHasToken : current))
    }, 2000)
    return () => window.clearInterval(interval)
  }, [])
  const login = () => {
    window.location.href = '/login'
  }
  const logout = () => {
    api.logout().catch(() => {})
    clearStoredToken()
    setIsAuthenticated(false)
  }
  return <AuthContext.Provider value={{ isAuthenticated, login, logout }}>{children}</AuthContext.Provider>
}
export function useAuth(): AuthState {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth() must be used inside <AuthProvider>')
  return ctx
}
