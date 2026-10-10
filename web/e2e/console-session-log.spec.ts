import { expect, test } from '@playwright/test'
import type { Route } from '@playwright/test'
import { loginAs, mockGroupList, mockInstanceList } from './mocks'
const SCHEDULER = { running: true, last_tick: null, interval_seconds: 15, message: null }
test('The Logs page lists migration console sessions and opens one from ?log=', async ({ page }) => {
  await loginAs(page)
  await mockInstanceList(page, {})
  await mockGroupList(page)
  await page.route('**/logs', (r: Route) =>
    r.fulfill({
      json: {
        services: [{ name: 'scheduler', exists: true, size: 10, modified: null }],
        console_sessions: [
          { name: 'lish-web1', instance: 'web1', exists: true, size: 2048, modified: '2026-10-10T10:00:00Z' },
        ],
        database: { path: '/x/instances.db', size: 1, wal_size: 0, activity_retention_days: 30 },
        scheduler: SCHEDULER,
      },
    }),
  )
  let requested = ''
  await page.route('**/logs/lish-web1*', (r: Route) => {
    requested = r.request().url()
    return r.fulfill({
      json: {
        lines: ['2026-10-10T10:00:01Z Copying /dev/sdg -> /dev/sda', '2026-10-10T10:04:12Z COPY_DONE'],
        offset: 100,
        reset: false,
      },
    })
  })
  await page.goto('/ui/#/logs?log=lish-web1')
  await expect(page.getByText('Migration console sessions')).toBeVisible()
  await expect(page.getByText(/COPY_DONE/)).toBeVisible()
  await expect(page.getByText(/Console session: web1/)).toBeVisible()
  expect(requested).toContain('/logs/lish-web1')
})
test('While the assisted copy runs, the migration page links to its console session', async ({ page }) => {
  await loginAs(page)
  await mockInstanceList(page, {})
  await mockGroupList(page)
  await page.route('**/instances/web1/migrate-status', (r: Route) =>
    r.fulfill({
      json: {
        in_progress: true,
        phase: 'awaiting_manual_dd',
        instance_id: 111,
        dest_volume_size_gb: 30,
        local_disk_size_mb: 25088,
      },
    }),
  )
  await page.route('**/instances/web1/migrate-copy', async (r: Route) => {
    if (r.request().method() === 'POST') return r.fulfill({ json: { operation_id: 'op-l', total_steps: 12 } })
    return r.fulfill({
      json: {
        available: true,
        reasons: [],
        username: 'alice',
        gateway: 'lish-in-bom-2.linode.com',
        public_key: 'ssh-ed25519 AAA k',
        key_registered: true,
        keys_allowed: true,
        backup_kept: true,
        instance_id: 111,
      },
    })
  })
  await page.route('**/operations/op-l', (r: Route) =>
    r.fulfill({
      json: {
        status: 'running',
        action: 'migrate_copy',
        percent: 25,
        current_step: 'Copied 6.4 GB so far (95 MB/s).',
        warnings: [],
      },
    }),
  )
  await page.goto('/ui/#/migrate?name=web1&instanceId=111')
  await page.getByRole('button', { name: 'Run the copy for me' }).click()
  const link = page.getByRole('link', { name: 'Watch the console session' })
  await expect(link).toBeVisible()
  await expect(link).toHaveAttribute('href', '#/logs?log=lish-web1')
  await expect(page.getByText('Copied 6.4 GB so far (95 MB/s).').first()).toBeVisible()
})
