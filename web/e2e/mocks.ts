import type { Page, Route } from '@playwright/test'
import type { InstanceRecord, OperationStatus } from '../src/api/types'
const SESSION_STORAGE_KEY = 'linode-scheduler-session-token'
export async function loginAs(page: Page, token = 'e2e-test-token'): Promise<void> {
  await page.addInitScript(
    ({ key, value }: { key: string; value: string }) => {
      window.localStorage.setItem(key, value)
    },
    { key: SESSION_STORAGE_KEY, value: token },
  )
}
export function baseInstanceRecord(overrides: Partial<InstanceRecord> = {}): InstanceRecord {
  return {
    label: 'redis-1',
    region: 'us-east',
    network_interface_model: 'linode',
    network_config: null,
    authorized_keys: ['ssh-ed25519 AAAA... deploy'],
    tags: [],
    instance_attrs: null,
    network_helper_enabled: true,
    vpc_prefix: null,
    os_volume_id: 111,
    data_volumes: [],
    reserved_ip: '192.0.2.10',
    group_id: null,
    current_linode_id: 999,
    current_status: 'running',
    transitioning: false,
    manual_override_expires_at: null,
    ...overrides,
  }
}
export async function mockInstanceList(page: Page, records: Record<string, InstanceRecord>): Promise<void> {
  await page.route('**/instances', async (route: Route) => {
    if (route.request().method() !== 'GET') return route.fallback()
    await route.fulfill({ json: records })
  })
  for (const [name, record] of Object.entries(records)) {
    await page.route(`**/instances/${encodeURIComponent(name)}/status`, async (route: Route) => {
      await route.fulfill({ json: record })
    })
    await page.route(`**/instances/${encodeURIComponent(name)}/schedule`, async (route: Route) => {
      await route.fulfill({ json: null })
    })
    await page.route(`**/instances/${encodeURIComponent(name)}/savings*`, async (route: Route) => {
      await route.fulfill({
        json: {
          scheduled_savings_percent: null,
          actual_savings_percent: null,
          window_days: 7,
          schedule_state: 'none',
        },
      })
    })
    await page.route(`**/instances/${encodeURIComponent(name)}/history*`, async (route: Route) => {
      await route.fulfill({ json: [] })
    })
  }
}
export async function mockGroupList(
  page: Page,
  groups: Array<{
    id: number
    name: string
    timezone: string
    rules: unknown[]
    enabled: boolean
    member_count: number
  }> = [],
): Promise<void> {
  await page.route('**/groups', async (route: Route) => {
    if (route.request().method() !== 'GET') return route.fallback()
    await route.fulfill({ json: groups })
  })
}
export async function mockOperation(
  page: Page,
  name: string,
  action: 'start' | 'stop',
  steps: Array<{
    status?: number
    body: OperationStatus<unknown>
  }>,
): Promise<{
  firstPollLanded: Promise<void>
}> {
  const operationId = `op-${action}-${name}`
  let pollIndex = 0
  let resolveFirstPoll: () => void
  const firstPollLanded = new Promise<void>((resolve) => {
    resolveFirstPoll = resolve
  })
  await page.route(`**/instances/${encodeURIComponent(name)}/${action}`, async (route: Route) => {
    await route.fulfill({ json: { operation_id: operationId } })
  })
  await page.route(`**/operations/${encodeURIComponent(operationId)}`, async (route: Route) => {
    const step = steps[Math.min(pollIndex, steps.length - 1)]
    pollIndex += 1
    await route.fulfill({ status: step.status ?? 200, json: step.body })
    resolveFirstPoll()
  })
  return { firstPollLanded }
}
