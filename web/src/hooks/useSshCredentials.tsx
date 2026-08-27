import { useRef, useState } from 'react'
import { api, ApiError } from '../api/client'
import { Button } from '../components/ui'
export function useSshCredentials(ipv4: string | undefined) {
  const [credMode, setCredMode] = useState<'none' | 'key' | 'password'>('none')
  const [credOpen, setCredOpen] = useState(false)
  const [sshPrivateKey, setSshPrivateKey] = useState('')
  const [sshPassword, setSshPassword] = useState('')
  const [sshPort, setSshPort] = useState(22)
  const [checkingSsh, setCheckingSsh] = useState(false)
  const [sshCheckResult, setSshCheckResult] = useState<{
    reachable: boolean
    detail?: string
    port: number
  } | null>(null)
  const checkedIpv4Ref = useRef<string | undefined>(undefined)
  const checkedPortRef = useRef<number | undefined>(undefined)
  const reset = () => {
    setCredMode('none')
    setSshPrivateKey('')
    setSshPassword('')
    setSshCheckResult(null)
    setCheckingSsh(false)
    checkedIpv4Ref.current = undefined
    checkedPortRef.current = undefined
  }
  const checkReachability = async () => {
    if (!ipv4) return
    const requestedIpv4 = ipv4
    const requestedPort = sshPort
    checkedIpv4Ref.current = requestedIpv4
    checkedPortRef.current = requestedPort
    const isStale = () => checkedIpv4Ref.current !== requestedIpv4 || checkedPortRef.current !== requestedPort
    setCheckingSsh(true)
    setSshCheckResult(null)
    try {
      const result = await api.checkSshReachable(requestedIpv4, requestedPort)
      if (isStale()) return
      setSshCheckResult({ ...result, port: requestedPort })
    } catch (e) {
      if (isStale()) return
      setSshCheckResult({
        reachable: false,
        detail: e instanceof ApiError ? e.message : 'Check failed.',
        port: requestedPort,
      })
    } finally {
      if (!isStale()) setCheckingSsh(false)
    }
  }
  const openIfLikelySshFailure = (message: string) => {
    if (credMode === 'none' && /ssh|permission denied/i.test(message)) setCredOpen(true)
  }
  const payload = {
    ssh_private_key: credMode === 'key' ? sshPrivateKey : null,
    ssh_password: credMode === 'password' ? sshPassword : null,
  }
  const isValid = (credMode !== 'key' || !!sshPrivateKey.trim()) && (credMode !== 'password' || !!sshPassword)
  const node = (
    <div className="rounded-md border border-slate-200">
      <button
        type="button"
        onClick={() => setCredOpen((v) => !v)}
        className="flex w-full items-center justify-between px-3 py-2 text-left text-xs font-medium text-slate-600 hover:bg-slate-50"
      >
        <span>
          SSH credentials{credMode !== 'none' && ' (set)'}
          {' — only needed if this node doesn’t already trust our default key'}
        </span>
        <span className="text-slate-400">{credOpen ? '▲' : '▼'}</span>
      </button>
      {credOpen && (
        <div className="space-y-3 border-t border-slate-200 px-3 py-3">
          <p className="rounded-md bg-slate-50 px-2.5 py-2 text-xs text-slate-600">
            We do <strong>not</strong> store this anywhere. It's used only to SSH into this one node for this
            one attempt, held in memory for the duration of the request, and deleted from our system the
            moment it finishes — whether it succeeds or fails.
          </p>
          <p className="rounded-md bg-amber-50 px-2.5 py-2 text-xs text-amber-800 ring-1 ring-inset ring-amber-200">
            This must be <strong>root's own</strong> password or private key — not a limited user. Every
            future automated step also connects as root, so a lower-privilege account genuinely can't do this
            job, not just right now.
          </p>
          <div className="rounded-md bg-slate-50 px-2.5 py-2 text-xs text-slate-600">
            <p className="mb-1.5">
              If your instances restrict SSH to specific source IPs, make sure this deployment's own outbound
              address is allowed through first — otherwise every attempt fails before credentials are even
              checked.
            </p>

            <div className="flex items-center gap-2">
              <span>Test a port</span>
              <input
                type="number"
                min={1}
                max={65535}
                value={sshPort}
                onChange={(e) => {
                  setSshPort(Number(e.target.value) || 22)
                  setSshCheckResult(null)
                  checkedPortRef.current = undefined
                  setCheckingSsh(false)
                }}
                className="w-16 rounded-md border-0 py-1 px-2 text-xs text-slate-900 ring-1 ring-inset ring-slate-300 focus:ring-2 focus:ring-indigo-600"
              />
              <Button
                variant="secondary"
                className="!px-2.5 !py-1 text-xs"
                disabled={checkingSsh || !ipv4}
                onClick={checkReachability}
              >
                {checkingSsh ? 'Checking…' : 'Test reachability'}
              </Button>
              {sshCheckResult && (
                <span className={sshCheckResult.reachable ? 'text-emerald-700' : 'text-red-700'}>
                  {sshCheckResult.reachable
                    ? `✓ Port ${sshCheckResult.port} is reachable`
                    : `✗ ${sshCheckResult.detail ?? 'Not reachable'}`}
                </span>
              )}
            </div>
            <p className="mt-1.5 text-slate-400">
              Connectivity check only — onboarding and migration always connect over the standard SSH port
              (22); a non-standard port isn't supported yet.
            </p>
          </div>
          <div className="flex gap-3 text-xs">
            {(['none', 'key', 'password'] as const).map((mode) => (
              <label key={mode} className="flex items-center gap-1.5">
                <input
                  type="radio"
                  name="ssh-cred-mode"
                  checked={credMode === mode}
                  onChange={() => setCredMode(mode)}
                />
                {mode === 'none'
                  ? 'Use default key'
                  : mode === 'key'
                    ? 'Root’s SSH private key'
                    : 'Root’s password'}
              </label>
            ))}
          </div>
          {credMode === 'key' && (
            <div className="space-y-2">
              <input
                type="file"
                accept=".pem,.key,text/plain"
                onChange={(e) => {
                  const file = e.target.files?.[0]
                  if (!file) return
                  const reader = new FileReader()
                  reader.onload = () => setSshPrivateKey(String(reader.result ?? ''))
                  reader.readAsText(file)
                }}
                className="block w-full text-xs text-slate-600 file:mr-2 file:rounded-md file:border-0 file:bg-slate-100 file:px-2.5 file:py-1.5 file:text-xs file:font-medium file:text-slate-700 hover:file:bg-slate-200"
              />
              <textarea
                className="block w-full rounded-md border-0 py-1.5 px-3 font-mono text-xs text-slate-900 ring-1 ring-inset ring-slate-300 focus:ring-2 focus:ring-indigo-600"
                rows={4}
                placeholder="-----BEGIN OPENSSH PRIVATE KEY-----&#10;…&#10;-----END OPENSSH PRIVATE KEY-----"
                value={sshPrivateKey}
                onChange={(e) => setSshPrivateKey(e.target.value)}
              />
            </div>
          )}
          {credMode === 'password' && (
            <input
              type="password"
              className="block w-full rounded-md border-0 py-1.5 px-3 text-sm text-slate-900 ring-1 ring-inset ring-slate-300 focus:ring-2 focus:ring-indigo-600"
              placeholder="root's password"
              value={sshPassword}
              onChange={(e) => setSshPassword(e.target.value)}
              autoComplete="off"
            />
          )}
        </div>
      )}
    </div>
  )
  return { node, payload, isValid, reset, openIfLikelySshFailure }
}
