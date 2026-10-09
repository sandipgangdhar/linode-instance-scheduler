import { useEffect, useRef, useState } from 'react'
import { ApiError, api } from '../api/client'
import { Button, Card, CardHeader, ErrorBanner, WarningBanner } from './ui'
type Iface = Record<string, unknown>
export function vpcAddresses(networkConfig: unknown, model: string | null | undefined): string[] {
  if (!Array.isArray(networkConfig)) return []
  const out: string[] = []
  for (const raw of networkConfig as Iface[]) {
    if (model === 'legacy_config') {
      const ipv4 = raw.ipv4 as
        | {
            vpc?: string
          }
        | undefined
      if (raw.purpose === 'vpc' && ipv4?.vpc) out.push(ipv4.vpc)
    } else if (raw.vpc && typeof raw.vpc === 'object') {
      const addrs = (
        (raw.vpc as Iface).ipv4 as
          | {
              addresses?: {
                address: string
                primary?: boolean
              }[]
            }
          | undefined
      )?.addresses
      if (addrs && addrs.length) out.push((addrs.find((a) => a.primary) ?? addrs[0]).address)
    }
  }
  return out
}
const inputClass =
  'block w-full rounded-md border-0 py-1.5 px-2 text-sm text-slate-900 ring-1 ring-inset ring-slate-300 focus:ring-2 focus:ring-indigo-600'
export function VpcAddressCard({
  name,
  addresses,
  stopped,
  onChanged,
}: {
  name: string
  addresses: string[]
  stopped: boolean
  onChanged: () => void
}) {
  const mountedRef = useRef(true)
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])
  const [editing, setEditing] = useState<string | null>(null)
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [warnings, setWarnings] = useState<string[]>([])
  const [done, setDone] = useState<string | null>(null)
  if (addresses.length === 0) return null
  const save = async () => {
    if (editing === null || busy) return
    setBusy(true)
    setError(null)
    setDone(null)
    try {
      const result = await api.setVpcAddress(name, value.trim(), addresses.length > 1 ? editing : null)
      if (!mountedRef.current) return
      setWarnings(result.warnings ?? [])
      setDone(`Moves from ${result.previous_address} to ${result.address} on its next start.`)
      setEditing(null)
      onChanged()
    } catch (e) {
      if (!mountedRef.current) return
      setError(e instanceof ApiError ? e.message : 'Could not change the VPC address.')
    } finally {
      if (mountedRef.current) setBusy(false)
    }
  }
  return (
    <Card>
      <CardHeader
        title="VPC address"
        subtitle="Linode can't reserve a VPC address, so while this instance is stopped a new instance can be given it. If that happens, the next start moves it to a free address automatically; to choose the address yourself, change it here while it's stopped."
      />
      <div className="space-y-3 px-5 pb-5">
        {error && <ErrorBanner message={error} />}
        {warnings.length > 0 && <WarningBanner messages={warnings} />}
        {done && (
          <div className="rounded-md bg-emerald-50 px-3 py-2 text-sm text-emerald-800 ring-1 ring-inset ring-emerald-200">
            {done}
          </div>
        )}
        {addresses.map((addr) => (
          <div key={addr} className="flex flex-wrap items-center gap-2 text-sm">
            <span className="font-mono text-slate-900">{addr}</span>
            {editing === addr ? (
              <>
                <input
                  aria-label="New VPC address"
                  className={`${inputClass} max-w-[12rem]`}
                  placeholder="e.g. 10.0.0.50"
                  value={value}
                  onChange={(e) => setValue(e.target.value)}
                />
                <Button variant="primary" disabled={busy || !value.trim()} onClick={save}>
                  Save
                </Button>
                <Button variant="secondary" disabled={busy} onClick={() => setEditing(null)}>
                  Cancel
                </Button>
              </>
            ) : (
              <Button
                variant="secondary"
                className="!px-2.5 !py-1 text-xs"
                disabled={!stopped}
                onClick={() => {
                  setEditing(addr)
                  setValue('')
                  setDone(null)
                  setError(null)
                }}
              >
                Change
              </Button>
            )}
          </div>
        ))}
        {!stopped && <p className="text-xs text-slate-500">Stop the instance to change its VPC address.</p>}
      </div>
    </Card>
  )
}
