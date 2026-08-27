import { createContext, useCallback, useContext, useRef, useState } from 'react'
import type { ReactNode } from 'react'
type StatusKind = 'pending' | 'success' | 'error'
interface StatusEntry {
  id: number
  label: string
  kind: StatusKind
  detail?: string
  percent?: number
  onNavigate?: () => void
  key?: string
  phase?: string
}
type ProgressReporter = (percent: number, step: string | null) => void
interface StatusBarApi {
  current: StatusEntry | null
  dismiss: () => void
  run: <T>(
    label: string,
    fn: (report: ProgressReporter) => Promise<T>,
    opts?: {
      onNavigate?: () => void
      key?: string
      phase?: string
    },
  ) => Promise<T>
}
const StatusBarContext = createContext<StatusBarApi | null>(null)
const SUCCESS_DISMISS_MS = 4000
export function StatusBarProvider({ children }: { children: ReactNode }) {
  const [current, setCurrent] = useState<StatusEntry | null>(null)
  const nextId = useRef(0)
  const dismissTimer = useRef<number | null>(null)
  const clearTimer = () => {
    if (dismissTimer.current !== null) {
      window.clearTimeout(dismissTimer.current)
      dismissTimer.current = null
    }
  }
  const dismiss = useCallback(() => {
    clearTimer()
    setCurrent(null)
  }, [])
  const run = useCallback(
    async <T,>(
      label: string,
      fn: (report: ProgressReporter) => Promise<T>,
      opts?: {
        onNavigate?: () => void
        key?: string
        phase?: string
      },
    ): Promise<T> => {
      const id = ++nextId.current
      const base = { id, label, onNavigate: opts?.onNavigate, key: opts?.key, phase: opts?.phase }
      clearTimer()
      setCurrent({ ...base, kind: 'pending' })
      const report: ProgressReporter = (percent, step) => {
        setCurrent((c) =>
          c?.id === id && c.kind === 'pending' ? { ...c, percent, detail: step ?? c.detail } : c,
        )
      }
      try {
        const result = await fn(report)
        setCurrent((c) => (c?.id === id ? { ...base, kind: 'success' } : c))
        dismissTimer.current = window.setTimeout(() => {
          setCurrent((c) => (c?.id === id ? null : c))
        }, SUCCESS_DISMISS_MS)
        return result
      } catch (e) {
        const detail = e instanceof Error ? e.message : 'Something went wrong.'
        setCurrent((c) => (c?.id === id ? { ...base, kind: 'error', detail } : c))
        throw e
      }
    },
    [],
  )
  return <StatusBarContext.Provider value={{ current, dismiss, run }}>{children}</StatusBarContext.Provider>
}
export function useStatusBar(): StatusBarApi {
  const ctx = useContext(StatusBarContext)
  if (!ctx) throw new Error('useStatusBar() must be used within a StatusBarProvider.')
  return ctx
}
