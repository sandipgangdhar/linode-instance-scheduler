import type {
  DeregisterResult,
  InstanceRecord,
  LinodeRawInstance,
  MigrateResumeResult,
  MigrateStartResult,
  MigrateStatus,
  OffboardResult,
  OnboardResult,
  OperationStatus,
  PatchInstanceGroupResult,
  Savings,
  Schedule,
  ScheduleEvent,
  ScheduleGroup,
  ScheduleGroupSummary,
  StartResult,
  StopResult,
} from './types'
export class ApiError extends Error {
  status: number
  body: Record<string, unknown> | null
  constructor(status: number, detail: string, body: Record<string, unknown> | null = null) {
    super(detail)
    this.body = body
    this.status = status
  }
}
export function errorWarnings(e: unknown): string[] | null {
  if (e instanceof ApiError && Array.isArray(e.body?.warnings) && e.body.warnings.length > 0) {
    return e.body.warnings as string[]
  }
  return null
}
async function pollOperation<T>(
  operationId: string,
  onProgress?: (percent: number, currentStep: string | null) => void,
  onWarning?: (warnings: string[]) => void,
): Promise<T> {
  let lastReported: readonly [number, string | null] | null = null
  for (;;) {
    const op = await request<OperationStatus<T>>('GET', `/operations/${encodeURIComponent(operationId)}`)
    if (lastReported === null || lastReported[0] !== op.percent || lastReported[1] !== op.current_step) {
      onProgress?.(op.percent, op.current_step)
      lastReported = [op.percent, op.current_step]
    }
    if (op.status === 'done') {
      if (op.warnings.length > 0) onWarning?.(op.warnings)
      return op.result as T
    }
    await new Promise((resolve) => setTimeout(resolve, 800))
  }
}
const SESSION_STORAGE_KEY = 'linode-scheduler-session-token'
export function getStoredToken(): string | null {
  return localStorage.getItem(SESSION_STORAGE_KEY)
}
export function storeToken(token: string): void {
  localStorage.setItem(SESSION_STORAGE_KEY, token)
}
export function clearStoredToken(): void {
  localStorage.removeItem(SESSION_STORAGE_KEY)
}
async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const token = getStoredToken()
  const headers: Record<string, string> = {}
  if (token) headers.Authorization = `Bearer ${token}`
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  const res = await fetch(path, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (res.status === 401) {
    clearStoredToken()
  }
  const text = await res.text()
  let data: unknown = null
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      data = null
    }
  }
  if (!res.ok) {
    const obj = data && typeof data === 'object' ? (data as Record<string, unknown>) : null
    const fromBody =
      obj && (typeof obj.detail === 'string' ? obj.detail : typeof obj.error === 'string' ? obj.error : null)
    const detail =
      fromBody ??
      (obj === null && text ? text.slice(0, 200) : null) ??
      `request failed with status ${res.status}`
    throw new ApiError(res.status, detail, obj)
  }
  return data as T
}
export const api = {
  listInstances: () => request<Record<string, InstanceRecord>>('GET', '/instances'),
  listLinodeInstances: () => request<LinodeRawInstance[]>('GET', '/linode/instances'),
  reserveIp: (address: string) =>
    request<{
      reserved: boolean
    }>('POST', `/linode/ips/${encodeURIComponent(address)}/reserve`),
  checkSshReachable: (host: string, port: number) =>
    request<{
      reachable: boolean
      detail?: string
    }>('GET', `/linode/ssh-check?host=${encodeURIComponent(host)}&port=${port}`),
  onboardInstance: (body: {
    name: string
    instance_id: number
    vpc_id?: number | null
    force?: boolean
    ssh_private_key?: string | null
    ssh_password?: string | null
  }) =>
    request<OnboardResult>('POST', '/instances', {
      name: body.name,
      instance_id: body.instance_id,
      vpc_id: body.vpc_id ?? null,
      force: body.force ?? false,
      ssh_private_key: body.ssh_private_key ?? null,
      ssh_password: body.ssh_password ?? null,
    }),
  offboardInstance: (name: string, deleteVolumes: boolean) =>
    request<OffboardResult>('POST', `/instances/${encodeURIComponent(name)}/offboard`, {
      delete_volumes: deleteVolumes,
    }),
  deregisterInstance: (name: string) =>
    request<DeregisterResult>('POST', `/instances/${encodeURIComponent(name)}/deregister`),
  getInstance: (name: string) =>
    request<InstanceRecord>('GET', `/instances/${encodeURIComponent(name)}/status`),
  getHistory: (name: string, limit = 50) =>
    request<ScheduleEvent[]>('GET', `/instances/${encodeURIComponent(name)}/history?limit=${limit}`),
  startInstance: async (
    name: string,
    overrideWindowHours?: number,
    onProgress?: (percent: number, currentStep: string | null) => void,
    onWarning?: (warnings: string[]) => void,
  ) => {
    const kickoff = await request<{
      operation_id: string
    }>('POST', `/instances/${encodeURIComponent(name)}/start`, {
      override_window_hours: overrideWindowHours ?? null,
    })
    return pollOperation<StartResult>(kickoff.operation_id, onProgress, onWarning)
  },
  stopInstance: async (
    name: string,
    opts?: {
      skipPrecapture?: boolean
      force?: boolean
    },
    onProgress?: (percent: number, currentStep: string | null) => void,
    onWarning?: (warnings: string[]) => void,
  ) => {
    const kickoff = await request<{
      operation_id: string
    }>('POST', `/instances/${encodeURIComponent(name)}/stop`, {
      skip_precapture: opts?.skipPrecapture ?? false,
      force: opts?.force ?? false,
    })
    return pollOperation<StopResult>(kickoff.operation_id, onProgress, onWarning)
  },
  migrateStart: async (
    name: string,
    instanceId: number,
    creds?: {
      ssh_private_key: string | null
      ssh_password: string | null
    },
    onProgress?: (percent: number, currentStep: string | null) => void,
    onWarning?: (warnings: string[]) => void,
  ) => {
    const kickoff = await request<{
      operation_id: string
    }>('POST', `/instances/${encodeURIComponent(name)}/migrate-start`, {
      instance_id: instanceId,
      ssh_private_key: creds?.ssh_private_key ?? null,
      ssh_password: creds?.ssh_password ?? null,
    })
    return pollOperation<MigrateStartResult>(kickoff.operation_id, onProgress, onWarning)
  },
  getMigrateStatus: (name: string) =>
    request<MigrateStatus>('GET', `/instances/${encodeURIComponent(name)}/migrate-status`),
  migrateResume: async (
    name: string,
    onProgress?: (percent: number, currentStep: string | null) => void,
    onWarning?: (warnings: string[]) => void,
  ) => {
    const kickoff = await request<{
      operation_id: string
    }>('POST', `/instances/${encodeURIComponent(name)}/migrate-resume`)
    return pollOperation<MigrateResumeResult>(kickoff.operation_id, onProgress, onWarning)
  },
  extendOverride: (name: string, hours?: number) =>
    request<{
      manual_override_expires_at: string
    }>('POST', `/instances/${encodeURIComponent(name)}/extend`, { hours: hours ?? null }),
  getSchedule: (name: string) =>
    request<Schedule | null>('GET', `/instances/${encodeURIComponent(name)}/schedule`),
  setSchedule: (name: string, schedule: Schedule) =>
    request<Schedule>('POST', `/instances/${encodeURIComponent(name)}/schedule`, schedule),
  clearSchedule: (name: string) =>
    request<{
      cleared: boolean
      warnings?: string[]
    }>('DELETE', `/instances/${encodeURIComponent(name)}/schedule`),
  patchInstanceGroup: (
    name: string,
    body: {
      group_name: string | null
      copy_group_rules_as_individual?: boolean
    },
  ) => request<PatchInstanceGroupResult>('PATCH', `/instances/${encodeURIComponent(name)}`, body),
  getInstanceSavings: (name: string, days = 7) =>
    request<Savings>('GET', `/instances/${encodeURIComponent(name)}/savings?days=${days}`),
  listGroups: () => request<ScheduleGroupSummary[]>('GET', '/groups'),
  getGroup: (name: string) => request<ScheduleGroup>('GET', `/groups/${encodeURIComponent(name)}`),
  createGroup: (name: string, timezone: string) =>
    request<ScheduleGroup>('POST', '/groups', { name, timezone }),
  deleteGroup: (name: string) =>
    request<{
      deleted: boolean
    }>('DELETE', `/groups/${encodeURIComponent(name)}`),
  setGroupSchedule: (name: string, schedule: Schedule) =>
    request<ScheduleGroup>('POST', `/groups/${encodeURIComponent(name)}/schedule`, schedule),
  getGroupSavings: (name: string, days = 7) =>
    request<Savings>('GET', `/groups/${encodeURIComponent(name)}/savings?days=${days}`),
  logout: () =>
    request<{
      ok: boolean
    }>('POST', '/logout'),
}
