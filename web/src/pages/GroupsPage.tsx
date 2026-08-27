import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import type { ScheduleGroupSummary } from '../api/types'
import { TimezoneSelect } from '../components/TimezoneSelect'
import { Button, Card, EmptyState, ErrorBanner, Spinner } from '../components/ui'
import { usePolling } from '../hooks/usePolling'
import { useStatusBar } from '../status/StatusBarContext'
import { PageHeader } from './DashboardLayout'
const POLL_INTERVAL_MS = 8000
export function GroupsPage() {
  const statusBar = useStatusBar()
  const [groups, setGroups] = useState<ScheduleGroupSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [showCreate, setShowCreate] = useState(false)
  const [name, setName] = useState('')
  const [timezone, setTimezone] = useState('UTC')
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  const latestRequestId = useRef(0)
  const reload = useCallback(() => {
    const requestId = ++latestRequestId.current
    api
      .listGroups()
      .then((r) => {
        if (requestId !== latestRequestId.current) return
        setGroups(r)
        setError(null)
      })
      .catch((e) => {
        if (requestId !== latestRequestId.current) return
        setError(e instanceof ApiError ? e.message : 'Could not load groups.')
      })
  }, [])
  useEffect(reload, [reload])
  usePolling(reload, POLL_INTERVAL_MS, !creating)
  const create = async () => {
    setCreating(true)
    setCreateError(null)
    try {
      await statusBar.run(`Creating group “${name}”`, () => api.createGroup(name, timezone))
      setName('')
      setTimezone('UTC')
      setShowCreate(false)
      reload()
    } catch (e) {
      setCreateError(e instanceof ApiError ? e.message : 'Could not create group.')
    } finally {
      setCreating(false)
    }
  }
  return (
    <div>
      <PageHeader
        title="Groups"
        subtitle="Share one schedule across several instances at once."
        action={
          <Button variant="primary" onClick={() => setShowCreate((v) => !v)}>
            {showCreate ? 'Cancel' : 'Create group'}
          </Button>
        }
      />
      <div className="space-y-6 p-8">
        {showCreate && (
          <Card>
            <div className="space-y-4 px-5 py-5">
              {createError && <ErrorBanner message={createError} />}
              <div className="grid grid-cols-2 gap-4">
                <label className="block">
                  <span className="text-sm font-medium text-slate-700">Name</span>
                  <input
                    className="mt-1 block w-full rounded-md border-0 py-1.5 px-3 text-sm ring-1 ring-inset ring-slate-300 focus:ring-2 focus:ring-indigo-600"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="Dev Environment"
                  />
                </label>
                <label className="block">
                  <span className="text-sm font-medium text-slate-700">Timezone</span>
                  <TimezoneSelect value={timezone} onChange={setTimezone} />
                </label>
              </div>
              <Button variant="primary" disabled={!name || creating} onClick={create}>
                {creating ? 'Creating…' : 'Create'}
              </Button>
            </div>
          </Card>
        )}

        {error && <ErrorBanner message={error} />}
        {!error && groups === null && (
          <div className="flex justify-center py-16">
            <Spinner className="text-indigo-600" />
          </div>
        )}
        {groups && groups.length === 0 && (
          <Card>
            <EmptyState
              title="No schedule groups yet"
              subtitle="Create one to share a schedule across several instances."
            />
          </Card>
        )}
        {groups && groups.length > 0 && (
          <Card className="overflow-hidden">
            <table className="min-w-full divide-y divide-slate-200">
              <thead className="bg-slate-50">
                <tr>
                  {['Name', 'Timezone', 'Rules', 'Members', 'Status'].map((h) => (
                    <th
                      key={h}
                      className="px-5 py-3 text-left text-xs font-semibold uppercase tracking-wide text-slate-500"
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 bg-white">
                {groups.map((g) => (
                  <tr key={g.id} className="hover:bg-slate-50">
                    <td className="px-5 py-3 text-sm font-medium">
                      <Link
                        to={`/groups/${encodeURIComponent(g.name)}`}
                        className="text-indigo-600 hover:underline"
                      >
                        {g.name}
                      </Link>
                    </td>
                    <td className="px-5 py-3 text-sm text-slate-600">{g.timezone}</td>
                    <td className="px-5 py-3 text-sm text-slate-600">{g.rules.length}</td>
                    <td className="px-5 py-3 text-sm text-slate-600">{g.member_count}</td>
                    <td className="px-5 py-3 text-sm text-slate-600">
                      {!g.enabled ? 'Disabled' : g.rules.length === 0 ? 'No schedule yet' : 'Enabled'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
        )}
      </div>
    </div>
  )
}
