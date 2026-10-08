import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { ApiToken, ApiTokenScope } from '../api/types'
import { Button, Card, CardHeader, EmptyState, ErrorBanner, Spinner } from '../components/ui'
import { PageHeader } from './DashboardLayout'
const SCOPES: {
  value: ApiTokenScope
  label: string
  help: string
}[] = [
  { value: 'read', label: 'Read', help: 'list, status, history, savings' },
  { value: 'operate', label: 'Operate', help: 'start, stop, extend, group start/stop' },
  { value: 'configure', label: 'Configure', help: 'schedules, groups, dependencies, manual-only' },
  { value: 'admin', label: 'Admin', help: 'everything: hooks, onboard/offboard, tokens' },
]
function splitList(text: string): string[] | null {
  const items = text
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
  return items.length ? items : null
}
export function TokensPage() {
  const [tokens, setTokens] = useState<ApiToken[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [scopes, setScopes] = useState<ApiTokenScope[]>(['read', 'operate'])
  const [instances, setInstances] = useState('')
  const [groups, setGroups] = useState('')
  const [expiresDays, setExpiresDays] = useState('')
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  const [newToken, setNewToken] = useState<{
    name: string
    token: string
  } | null>(null)
  const [revoking, setRevoking] = useState<string | null>(null)
  const mountedRef = useRef(true)
  const reload = useCallback(() => {
    api
      .listTokens()
      .then((t) => {
        if (mountedRef.current) {
          setTokens(t)
          setError(null)
        }
      })
      .catch((e) => {
        if (mountedRef.current) setError(e instanceof ApiError ? e.message : 'Could not load tokens.')
      })
  }, [])
  useEffect(() => {
    mountedRef.current = true
    reload()
    return () => {
      mountedRef.current = false
    }
  }, [reload])
  const create = async () => {
    if (creating) return
    setCreating(true)
    setCreateError(null)
    setNewToken(null)
    try {
      const days = expiresDays.trim() ? Number(expiresDays) : null
      const created = await api.createToken({
        name: name.trim(),
        scopes,
        instances: splitList(instances),
        groups: splitList(groups),
        expires_days: days,
      })
      if (!mountedRef.current) return
      setNewToken({ name: created.name, token: created.token })
      setName('')
      setInstances('')
      setGroups('')
      setExpiresDays('')
      reload()
    } catch (e) {
      if (mountedRef.current)
        setCreateError(e instanceof ApiError ? e.message : 'Could not create the token.')
    } finally {
      if (mountedRef.current) setCreating(false)
    }
  }
  const revoke = async (tokenName: string) => {
    if (revoking) return
    setRevoking(tokenName)
    try {
      await api.revokeToken(tokenName)
      reload()
    } catch (e) {
      if (mountedRef.current) setError(e instanceof ApiError ? e.message : 'Could not revoke the token.')
    } finally {
      if (mountedRef.current) setRevoking(null)
    }
  }
  const toggleScope = (scope: ApiTokenScope) =>
    setScopes((cur) => (cur.includes(scope) ? cur.filter((s) => s !== scope) : [...cur, scope]))
  return (
    <>
      <PageHeader
        title="API tokens"
        subtitle="Credentials for scripts and automation. Send as: Authorization: Bearer <token>."
      />
      <div className="space-y-6 p-8">
        {error && <ErrorBanner message={error} />}
        <Card>
          <CardHeader
            title="Create a token"
            subtitle="Grant only what the script needs. The token is shown once."
          />
          <div className="space-y-4 px-5 py-4 text-sm">
            {createError && <ErrorBanner message={createError} />}
            {newToken && (
              <div className="rounded-md border border-emerald-200 bg-emerald-50 p-3">
                <p className="font-medium text-emerald-900">
                  Token “{newToken.name}” created. Copy it now; it won't be shown again.
                </p>
                <code
                  aria-label="New token"
                  className="mt-2 block break-all rounded bg-white p-2 text-xs text-slate-900"
                >
                  {newToken.token}
                </code>
              </div>
            )}
            <label className="block">
              <span className="text-slate-700">Name</span>
              <input
                aria-label="Token name"
                className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-1.5"
                placeholder="e.g. ci-deploy"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </label>
            <fieldset>
              <legend className="text-slate-700">Scopes</legend>
              <div className="mt-1 grid grid-cols-2 gap-2">
                {SCOPES.map((s) => (
                  <label key={s.value} className="flex items-start gap-2">
                    <input
                      type="checkbox"
                      checked={scopes.includes(s.value)}
                      onChange={() => toggleScope(s.value)}
                    />
                    <span>
                      <span className="font-medium text-slate-900">{s.label}</span>
                      <span className="block text-xs text-slate-500">{s.help}</span>
                    </span>
                  </label>
                ))}
              </div>
            </fieldset>
            <div className="grid grid-cols-3 gap-3">
              <label className="block">
                <span className="text-slate-700">Only these nodes</span>
                <input
                  aria-label="Allowed nodes"
                  className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-1.5"
                  placeholder="web-1, web-2"
                  value={instances}
                  onChange={(e) => setInstances(e.target.value)}
                />
              </label>
              <label className="block">
                <span className="text-slate-700">Only these groups</span>
                <input
                  aria-label="Allowed groups"
                  className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-1.5"
                  placeholder="dev"
                  value={groups}
                  onChange={(e) => setGroups(e.target.value)}
                />
              </label>
              <label className="block">
                <span className="text-slate-700">Expires after (days)</span>
                <input
                  aria-label="Expiry days"
                  className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-1.5"
                  placeholder="never"
                  value={expiresDays}
                  onChange={(e) => setExpiresDays(e.target.value)}
                />
              </label>
            </div>
            <p className="text-xs text-slate-500">
              Leave nodes and groups empty for a token that can act on everything its scopes allow.
            </p>
            <Button
              variant="primary"
              disabled={creating || !name.trim() || scopes.length === 0}
              onClick={() => void create()}
            >
              {creating ? 'Creating…' : 'Create token'}
            </Button>
          </div>
        </Card>
        <Card>
          <CardHeader title="Tokens" />
          {tokens === null ? (
            <div className="p-6">
              <Spinner />
            </div>
          ) : tokens.length === 0 ? (
            <EmptyState title="No API tokens yet" />
          ) : (
            <ul className="divide-y divide-slate-100">
              {tokens.map((t) => (
                <li key={t.name} className="flex items-center justify-between gap-4 px-5 py-3 text-sm">
                  <div>
                    <span className="font-medium text-slate-900">{t.name}</span>
                    <span className="ml-2 text-xs text-slate-400">{t.token_prefix}…</span>
                    <div className="text-xs text-slate-500">
                      {t.scopes.join(', ')}
                      {(t.instances || t.groups) &&
                        ` · limited to ${[...(t.instances ?? []), ...(t.groups ?? []).map((g) => `group ${g}`)].join(', ')}`}
                      {' · '}
                      {t.revoked_at
                        ? 'revoked'
                        : t.expires_at
                          ? `expires ${new Date(t.expires_at).toLocaleDateString()}`
                          : 'no expiry'}
                      {' · last used '}
                      {t.last_used_at ? new Date(t.last_used_at).toLocaleString() : 'never'}
                    </div>
                  </div>
                  {!t.revoked_at && (
                    <Button variant="danger" disabled={revoking !== null} onClick={() => void revoke(t.name)}>
                      {revoking === t.name ? 'Revoking…' : 'Revoke'}
                    </Button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </>
  )
}
