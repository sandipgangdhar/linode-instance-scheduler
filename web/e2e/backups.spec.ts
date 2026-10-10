import { expect, test } from '@playwright/test'
import type { Route } from '@playwright/test'
import { baseInstanceRecord, loginAs, mockGroupList, mockInstanceList } from './mocks'
const BACKUP = {
  name: 'web-1',
  status: 'kept',
  created_at: '2026-10-10T10:00:00Z',
  updated_at: '2026-10-10T10:05:00Z',
  original_instance_id: 100,
  original_label: 'web-1',
  region: 'in-bom-2',
  plan: 'g6-nanode-1',
  public_ipv4: ['203.0.113.10'],
  backup_instance_id: 200,
  backup_label: 'web-1-backup-1a2b3c4d',
  backup_volumes: [{ slot: 'sdc', volume_id: 300, size: 10, source_volume_id: 299 }],
  estimated_monthly_cost: {
    instance_monthly: 5,
    volumes_monthly: 1,
    total_monthly: 6,
    plan: 'g6-nanode-1',
    region: 'in-bom-2',
    volume_gb: 10,
  },
  restored_at: null,
}
test('Backups page lists a kept backup and rolls back only after typing the name', async ({ page }) => {
  await loginAs(page)
  await mockInstanceList(page, {})
  await mockGroupList(page)
  await page.route('**/backups', (route: Route) => route.fulfill({ json: [BACKUP] }))
  let rollbackBody: unknown = null
  await page.route('**/backups/web-1/rollback', async (route: Route) => {
    rollbackBody = route.request().postDataJSON()
    await route.fulfill({ json: { operation_id: 'op-rb', total_steps: 8 } })
  })
  await page.route('**/operations/op-rb', (route: Route) =>
    route.fulfill({
      json: {
        status: 'done',
        percent: 100,
        current_step: null,
        warnings: [],
        result: {
          outcome: 'restored',
          backup_instance_id: 200,
          label: 'web-1',
          public_ipv4: ['203.0.113.10'],
          original_deleted_instance_id: null,
          reachable: true,
          warnings: [],
        },
      },
    }),
  )
  await page.goto('/#/backups')
  await expect(page.getByText('web-1-backup-1a2b3c4d')).toBeVisible()
  await expect(page.getByText(/still billable: about \$6\.00\/month/)).toBeVisible()
  await page.getByRole('button', { name: 'Roll back' }).click()
  const confirmButton = page.getByRole('button', { name: 'Roll back now' })
  await expect(confirmButton).toBeDisabled()
  await page.getByLabel(/Type “web-1” to confirm/).fill('web-1')
  await expect(confirmButton).toBeEnabled()
  await confirmButton.click()
  await expect(page.getByText(/Restored: instance 200/)).toBeVisible()
  expect(rollbackBody).toEqual({ boot: true })
})
test('System backup page shows the settings without the secret and runs a backup', async ({ page }) => {
  await loginAs(page)
  await mockInstanceList(page, {})
  await mockGroupList(page)
  await page.route('**/system/backup', (route: Route) =>
    route.fulfill({
      json: {
        object_storage: {
          configured: true,
          bucket: 'my-bkt',
          endpoint: 'https://in-maa-1.linodeobjects.com',
          access_key_hint: 'AKEY...XX',
          secret_key_set: true,
        },
        local_backup_dir: '/opt/scheduler/backups',
        last_backup: {
          finished_at: '2026-10-10T10:00:00Z',
          ok: true,
          trigger: 'cli',
          object_storage_key: 'full-backups/x.db',
          local_path: null,
          problems: [],
        },
      },
    }),
  )
  let ran = false
  await page.route('**/system/backup/run', async (route: Route) => {
    ran = true
    await route.fulfill({ json: { operation_id: 'op-sb', total_steps: 6 } })
  })
  await page.route('**/operations/op-sb', (route: Route) =>
    route.fulfill({
      json: {
        status: 'done',
        percent: 100,
        current_step: null,
        warnings: [],
        result: { ok: true, problems: [], object_storage_key: 'k', local_path: null },
      },
    }),
  )
  await page.goto('/#/system-backup')
  await expect(page.getByText('my-bkt')).toBeVisible()
  await expect(page.getByText(/Access key AKEY\.\.\.XX/)).toBeVisible()
  await page.getByRole('button', { name: 'Back up now' }).click()
  await expect(page.getByText('Backup completed.')).toBeVisible()
  expect(ran).toBe(true)
})
test('A running scheduled instance can be kept running past today’s stop', async ({ page }) => {
  await loginAs(page)
  await mockInstanceList(page, { 'redis-1': baseInstanceRecord({ current_status: 'running' }) })
  await mockGroupList(page)
  await page.route('**/instances/redis-1/schedule', async (route: Route) => {
    if (route.request().method() !== 'GET') return route.fallback()
    await route.fulfill({
      json: {
        timezone: 'UTC',
        rules: [{ days_of_week: ['mon'], start_time: '09:00', stop_time: '18:00' }],
        enabled: true,
      },
    })
  })
  let extendBody: unknown = null
  await page.route('**/instances/redis-1/extend', async (route: Route) => {
    extendBody = route.request().postDataJSON()
    await route.fulfill({ json: { manual_override_expires_at: '2026-10-10T21:00:00Z' } })
  })
  await page.goto('/#/instances/redis-1')
  await page.getByLabel('Extend by hours').selectOption('3')
  await page.getByRole('button', { name: 'Keep running past today’s stop' }).click()
  await expect.poll(() => extendBody).toEqual({ hours: 3 })
})
