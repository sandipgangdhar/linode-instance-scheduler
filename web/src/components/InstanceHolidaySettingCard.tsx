import { useRef, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { AccountHolidaySetting } from '../api/types'
import { Card, CardHeader, ErrorBanner, WarningBanner } from './ui'
type Choice = 'inherit' | 'follow' | 'ignore'
export function InstanceHolidaySettingCard({
  name,
  setting,
  groupName,
  onChanged,
}: {
  name: string
  setting: AccountHolidaySetting | undefined
  groupName: string | null
  onChanged: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [warnings, setWarnings] = useState<string[]>([])
  const busyRef = useRef(false)
  const current: Choice = setting?.own ?? 'inherit'
  const inheritLabel = groupName
    ? `Same as group "${groupName}"${setting && setting.source !== 'instance' ? ` (${setting.effective === 'ignore' ? 'runs on them' : 'skips them'})` : ''}`
    : 'Default (skip them)'
  async function change(choice: Choice) {
    if (busyRef.current || choice === current) return
    busyRef.current = true
    setBusy(true)
    setError(null)
    setWarnings([])
    try {
      const r = await api.setInstanceHolidaySetting(name, choice)
      setWarnings(r.warnings ?? [])
      onChanged()
    } catch (e) {
      setError(e instanceof ApiError || e instanceof Error ? e.message : String(e))
    } finally {
      busyRef.current = false
      setBusy(false)
    }
  }
  return (
    <Card>
      <CardHeader
        title="Account-wide holidays"
        subtitle={
          setting
            ? setting.effective === 'ignore'
              ? 'This instance runs on account-wide holidays.'
              : "Account-wide holidays skip this instance's scheduled starts."
            : "Whether account-wide holidays skip this instance's scheduled starts."
        }
      />
      <div className="space-y-2 px-5 py-4 text-sm">
        <select
          aria-label="Account-wide holidays for this instance"
          className="rounded-md border border-slate-300 px-2 py-1.5"
          value={current}
          disabled={busy}
          onChange={(e) => change(e.target.value as Choice)}
        >
          <option value="inherit">{inheritLabel}</option>
          <option value="follow">Skip them (follow the account-wide calendar)</option>
          <option value="ignore">Run on them</option>
        </select>
        <p className="text-xs text-slate-500">
          This instance's own choice overrides its group's. Holidays added for this instance or its group
          always apply.
        </p>
        {warnings.length > 0 && <WarningBanner messages={warnings} />}
        {error && <ErrorBanner message={error} />}
      </div>
    </Card>
  )
}
