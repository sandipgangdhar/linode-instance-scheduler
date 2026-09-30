import { expect, test } from '@playwright/test'
import type { Route } from '@playwright/test'
import { baseInstanceRecord, loginAs, mockGroupList, mockInstanceList } from './mocks'
test('clicking Extend then Clear schedule in quick succession fires both real requests', async ({ page }) => {
  await loginAs(page)
  await mockInstanceList(page, {
    'redis-1': baseInstanceRecord({
      current_status: 'running',
      manual_override_expires_at: '2026-01-01T00:00:00Z',
    }),
  })
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
  let extendCallCount = 0
  let clearCallCount = 0
  await page.route('**/instances/redis-1/extend', async (route: Route) => {
    extendCallCount += 1
    await route.fulfill({ json: { manual_override_expires_at: null } })
  })
  await page.route('**/instances/redis-1/schedule', async (route: Route) => {
    if (route.request().method() !== 'DELETE') return route.fallback()
    clearCallCount += 1
    await route.fulfill({ json: { cleared: true, warnings: [] } })
  })
  await page.goto('/ui/#/instances/redis-1')
  await page.getByRole('button', { name: 'Extend override' }).click()
  await page.getByRole('button', { name: 'Clear' }).click()
  await page.waitForTimeout(1000)
  expect(extendCallCount).toBe(1)
  expect(clearCallCount).toBe(1)
})
