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
  others: StatusEntry[]
  bringToFront: (id: number) => void
  findByKey: (key: string) => StatusEntry | null
  dismiss: (id: number) => void
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
  const [entries, setEntries] = useState<StatusEntry[]>([])
  const [frontId, setFrontId] = useState<number | null>(null)
  const nextId = useRef(0)
  const dismissTimers = useRef(new Map<number, number>())
  const clearTimerFor = (id: number) => {
    const t = dismissTimers.current.get(id)
    if (t !== undefined) {
      window.clearTimeout(t)
      dismissTimers.current.delete(id)
    }
  }
  const dismiss = useCallback((id: number) => {
    clearTimerFor(id)
    setEntries((es) => es.filter((e) => e.id !== id))
  }, [])
  const bringToFront = useCallback((id: number) => setFrontId(id), [])
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
      setEntries((es) => [...es, { ...base, kind: 'pending' as const }])
      setFrontId(id)
      const report: ProgressReporter = (percent, step) => {
        setEntries((es) =>
          es.map((e) =>
            e.id === id && e.kind === 'pending' ? { ...e, percent, detail: step ?? e.detail } : e,
          ),
        )
      }
      try {
        const result = await fn(report)
        setEntries((es) => es.map((e) => (e.id === id ? { ...base, kind: 'success' as const } : e)))
        const t = window.setTimeout(() => {
          setEntries((es) => es.filter((e) => e.id !== id))
          dismissTimers.current.delete(id)
        }, SUCCESS_DISMISS_MS)
        dismissTimers.current.set(id, t)
        return result
      } catch (e) {
        const detail = e instanceof Error ? e.message : 'Something went wrong.'
        setEntries((es) => es.map((en) => (en.id === id ? { ...base, kind: 'error' as const, detail } : en)))
        throw e
      }
    },
    [],
  )
  const current = entries.find((e) => e.id === frontId) ?? entries[entries.length - 1] ?? null
  const others = current ? entries.filter((e) => e.id !== current.id) : entries
  const findByKey = useCallback(
    (key: string) => {
      for (let i = entries.length - 1; i >= 0; i--) {
        if (entries[i].key === key) return entries[i]
      }
      return null
    },
    [entries],
  )
  return (
    <StatusBarContext.Provider value={{ current, others, bringToFront, findByKey, dismiss, run }}>
      {children}
    </StatusBarContext.Provider>
  )
}
export function useStatusBar(): StatusBarApi {
  const ctx = useContext(StatusBarContext)
  if (!ctx) throw new Error('useStatusBar() must be used within a StatusBarProvider.')
  return ctx
}
