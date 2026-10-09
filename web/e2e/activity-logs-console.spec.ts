import { expect, test } from '@playwright/test'
import type { Route } from '@playwright/test'
import { loginAs, mockGroupList, mockInstanceList } from './mocks'
const now = new Date().toISOString()
const scheduler = {
  running: true,
  last_tick: now,
  interval_seconds: 300,
  age_seconds: 42,
  instances_checked: 3,
  fired: 1,
  failed: 0,
  message: null,
}
test('activity page shows entries, scheduler status and follows new entries live', async ({ page }) => {
  await loginAs(page)
  await mockInstanceList(page, {})
  await mockGroupList(page)
  let polled = 0
  await page.route('**/activity?*', (r: Route) => {
    const url = new URL(r.request().url())
    if (url.searchParams.get('after_id')) {
      polled += 1
      return r.fulfill({
        json: {
          scheduler,
          entries:
            polled === 1
              ? [
                  {
                    id: 3,
                    timestamp: now,
                    level: 'error',
                    source: 'schedule',
                    instance_name: 'web-1',
                    action: 'stop',
                    actor: null,
                    message: 'stop: delete_failed -- boom',
                  },
                ]
              : [],
        },
      })
    }
    return r.fulfill({
      json: {
        scheduler,
        entries: [
          {
            id: 2,
            timestamp: now,
            level: 'warning',
            source: 'scheduler',
            instance_name: 'web-1',
            action: 'tick',
            actor: null,
            message: 'scheduler: waiting_on_dependency action=start',
          },
          {
            id: 1,
            timestamp: now,
            level: 'info',
            source: 'console',
            instance_name: null,
            action: 'console',
            actor: 'alice',
            message: '$ list',
          },
        ],
      },
    })
  })
  await page.goto('/ui/#/activity')
  const main = page.locator('main')
  await expect(main).toContainText('Scheduler running')
  await expect(main).toContainText('waiting_on_dependency')
  await expect(main).toContainText('$ list')
  await expect(main).toContainText('alice')
  await expect(main).toContainText('stop: delete_failed -- boom', { timeout: 10000 })
})
test('activity page warns when the scheduler stopped checking in', async ({ page }) => {
  await loginAs(page)
  await mockInstanceList(page, {})
  await page.route('**/activity?*', (r: Route) =>
    r.fulfill({ json: { entries: [], scheduler: { ...scheduler, running: false, age_seconds: 7200 } } }),
  )
  await page.goto('/ui/#/activity')
  await expect(page.locator('main')).toContainText('Scheduler not checking in')
})
test('logs page tails the selected service and appends new lines', async ({ page }) => {
  await loginAs(page)
  await page.route('**/logs', (r: Route) =>
    r.fulfill({
      json: {
        scheduler,
        services: [
          { name: 'scheduler', exists: true, size: 2048, modified: now },
          { name: 'api', exists: true, size: 100, modified: now },
          { name: 'backup', exists: false, size: 0, modified: null },
          { name: 'console', exists: false, size: 0, modified: null },
        ],
        database: { path: '/opt/x/state/instances.db', size: 4096, wal_size: 0, activity_retention_days: 30 },
      },
    }),
  )
  let tails = 0
  await page.route('**/logs/scheduler?*', (r: Route) => {
    const url = new URL(r.request().url())
    if (url.searchParams.has('offset')) {
      tails += 1
      return r.fulfill({
        json: {
          lines: tails === 1 ? ['2026-10-09T10:00:05Z ERR WARNING: tick overran'] : [],
          offset: 200,
          reset: false,
        },
      })
    }
    return r.fulfill({
      json: { lines: ['2026-10-09T10:00:00Z OUT Polling every 300s'], offset: 100, reset: false },
    })
  })
  await page.route('**/logs/api?*', (r: Route) =>
    r.fulfill({
      json: { lines: ['2026-10-09T10:00:00Z ERR INFO: Uvicorn running'], offset: 50, reset: false },
    }),
  )
  await page.goto('/ui/#/logs')
  const log = page.getByRole('log')
  await expect(log).toContainText('Polling every 300s')
  await expect(log).toContainText('tick overran', { timeout: 10000 })
  await expect(page.locator('main')).toContainText('/opt/x/state/instances.db')
  await page.getByRole('button', { name: /API \/ dashboard/ }).click()
  await expect(log).toContainText('Uvicorn running')
  await expect(log).not.toContainText('Polling every 300s')
})
test('console runs a command and streams its output', async ({ page }) => {
  await loginAs(page)
  await page.route('**/console/commands', (r: Route) => r.fulfill({ json: { commands: ['list', 'status'] } }))
  let listed = 0
  await page.route('**/console/runs', (r: Route) => {
    listed += 1
    return r.fulfill({ json: [] })
  })
  let sent = ''
  await page.route('**/console/run', (r: Route) => {
    sent = (
      r.request().postDataJSON() as {
        command: string
      }
    ).command
    return r.fulfill({
      json: {
        id: 'r1',
        command: 'list',
        actor: 'alice',
        status: 'running',
        exit_code: null,
        started_at: now,
        finished_at: null,
        lines: ['NAME   STATUS'],
        next_offset: 1,
        truncated: false,
      },
    })
  })
  await page.route('**/console/runs/r1?*', (r: Route) =>
    r.fulfill({
      json: {
        id: 'r1',
        command: 'list',
        actor: 'alice',
        status: 'done',
        exit_code: 0,
        started_at: now,
        finished_at: now,
        lines: ['web-1  running'],
        next_offset: 2,
        truncated: false,
      },
    }),
  )
  await page.goto('/ui/#/console')
  await page.getByLabel('Command', { exact: true }).fill('list')
  await page.getByLabel('Command', { exact: true }).press('Enter')
  const out = page.getByRole('log', { name: 'Command output' })
  await expect(out).toContainText('NAME   STATUS')
  await expect(out).toContainText('web-1  running', { timeout: 10000 })
  await expect(page.locator('main')).toContainText('exit 0')
  expect(sent).toBe('list')
  expect(listed).toBeGreaterThan(0)
})
test('console shows the server refusal for a command it does not allow', async ({ page }) => {
  await loginAs(page)
  await page.route('**/console/commands', (r: Route) => r.fulfill({ json: { commands: ['list'] } }))
  await page.route('**/console/runs', (r: Route) => r.fulfill({ json: [] }))
  await page.route('**/console/run', (r: Route) =>
    r.fulfill({ status: 400, json: { detail: "'bash' isn't available in the console." } }),
  )
  await page.goto('/ui/#/console')
  await page.getByLabel('Command', { exact: true }).fill('bash')
  await page.getByRole('button', { name: 'Run' }).click()
  await expect(page.locator('main')).toContainText("isn't available in the console")
})
