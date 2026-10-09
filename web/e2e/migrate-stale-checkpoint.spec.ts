import { expect, test } from '@playwright/test'
import type { Route } from '@playwright/test'
import { loginAs, mockGroupList, mockInstanceList } from './mocks'
test('a checkpoint for a different instance offers a new migration, sent with force', async ({ page }) => {
  await loginAs(page)
  await mockInstanceList(page, {})
  await mockGroupList(page)
  await page.route('**/instances/db-scheduler/migrate-status', (r: Route) =>
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
  let body: Record<string, unknown> | null = null
  await page.route('**/instances/db-scheduler/migrate-start', async (r: Route) => {
    body = r.request().postDataJSON()
    await r.fulfill({ json: { operation_id: 'op-s', total_steps: 6 } })
  })
  await page.route('**/operations/op-s', (r: Route) =>
    r.fulfill({
      json: {
        status: 'running',
        action: 'migrate_start',
        percent: 17,
        current_step: 'Running pre-flight checks',
        warnings: [],
      },
    }),
  )
  await page.goto('/ui/#/migrate?name=db-scheduler&instanceId=222')
  await expect(page.locator('main')).toContainText('it was for instance 111, not this one (222)')
  await expect(page.locator('main')).not.toContainText('Step 1 of 3')
  await page.getByRole('button', { name: 'Start a new migration for this instance' }).click()
  await expect(page.locator('main')).toContainText('replaces the earlier migration attempt for instance 111')
  await page.locator('input').last().fill('db-scheduler')
  await page.getByRole('button', { name: /Start migration now/ }).click()
  await expect.poll(() => body).not.toBeNull()
  expect(body).toMatchObject({ instance_id: 222, force: true })
})
test('an interrupted setup can be started over from the dashboard', async ({ page }) => {
  await loginAs(page)
  await mockInstanceList(page, {})
  await mockGroupList(page)
  await page.route('**/instances/web1/migrate-status', (r: Route) =>
    r.fulfill({ json: { in_progress: true, phase: 'volume_created', instance_id: 5 } }),
  )
  let body: Record<string, unknown> | null = null
  await page.route('**/instances/web1/migrate-start', async (r: Route) => {
    body = r.request().postDataJSON()
    await r.fulfill({ json: { operation_id: 'op-i', total_steps: 6 } })
  })
  await page.route('**/operations/op-i', (r: Route) =>
    r.fulfill({
      json: { status: 'running', action: 'migrate_start', percent: 17, current_step: null, warnings: [] },
    }),
  )
  await page.goto('/ui/#/migrate?name=web1&instanceId=5')
  await page.getByRole('button', { name: 'Start over' }).click()
  await page.locator('input').last().fill('web1')
  await page.getByRole('button', { name: /Start migration now/ }).click()
  await expect.poll(() => body).not.toBeNull()
  expect(body).toMatchObject({ instance_id: 5, force: true })
})
