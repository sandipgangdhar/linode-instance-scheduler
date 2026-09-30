import { expect, test } from '@playwright/test'
import type { Route } from '@playwright/test'
import { baseInstanceRecord, loginAs, mockGroupList, mockInstanceList } from './mocks'
test('a rapid double-click on Start fires exactly one real request', async ({ page }) => {
  await loginAs(page)
  await mockInstanceList(page, {
    'redis-1': baseInstanceRecord({ current_status: 'stopped', current_linode_id: null }),
  })
  await mockGroupList(page)
  let kickoffCallCount = 0
  await page.route('**/instances/redis-1/start', async (route: Route) => {
    kickoffCallCount += 1
    await route.fulfill({ json: { operation_id: 'op-start-redis-1' } })
  })
  await page.route('**/operations/op-start-redis-1', async (route: Route) => {
    await route.fulfill({
      json: {
        status: 'done',
        action: 'start',
        percent: 100,
        current_step: null,
        warnings: [],
        result: {
          outcome: 'started',
          instance_id: 999,
          reserved_ip: '192.0.2.10',
          manual_override_expires_at: null,
        },
      },
    })
  })
  await page.goto('/ui/#/instances/redis-1')
  const button = page.getByRole('button', { name: /^Start$/ })
  await button.click()
  await button.click({ force: true, timeout: 500 }).catch(() => {})
  await page.waitForTimeout(1000)
  expect(kickoffCallCount).toBe(1)
})
