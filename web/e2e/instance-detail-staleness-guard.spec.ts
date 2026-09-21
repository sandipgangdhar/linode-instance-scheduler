import { expect, test } from '@playwright/test'
import type { Route } from '@playwright/test'
import { baseInstanceRecord, loginAs, mockGroupList, mockInstanceList } from './mocks'
test('a slow Start with a genuine host-key mismatch never paints its result onto a different instance navigated to before it resolves', async ({
  page,
}) => {
  await loginAs(page)
  await mockInstanceList(page, {
    'redis-1': baseInstanceRecord({ current_status: 'stopped', current_linode_id: null }),
    'redis-2': baseInstanceRecord({ current_status: 'running' }),
  })
  await mockGroupList(page)
  await page.route('**/instances/redis-1/start', async (route: Route) => {
    await route.fulfill({ json: { operation_id: 'op-start-redis-1' } })
  })
  await page.route('**/operations/op-start-redis-1', async (route: Route) => {
    await new Promise((r) => setTimeout(r, 1200))
    await route.fulfill({
      json: {
        status: 'done',
        action: 'start',
        percent: 100,
        current_step: null,
        warnings: [],
        result: {
          outcome: 'reachability_check_failed',
          instance_id: 999,
          reserved_ip: '192.0.2.10',
          live_status: 'running',
          security_warning: true,
          detail: 'a real, different ED25519 key than the one on record',
          manual_override_expires_at: null,
        },
      },
    })
  })
  await page.goto('/ui/#/instances/redis-1')
  await page.getByRole('button', { name: /^Start$/ }).click()
  await expect(page.getByRole('button', { name: /Starting/ })).toBeVisible()
  await page.goto('/ui/#/instances/redis-2')
  await expect(page.getByText('running', { exact: true })).toBeVisible()
  await page.waitForTimeout(1600)
  await expect(page).toHaveURL(/#\/instances\/redis-2/)
  await expect(page.locator('p', { hasText: 'Security warning' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: /^Start$/ })).toBeDisabled()
  await expect(page.getByText('running', { exact: true })).toBeVisible()
})
test('the SAME instance genuinely shows the SecurityWarningBanner when the operator stays on its own page', async ({
  page,
}) => {
  await loginAs(page)
  await mockInstanceList(page, {
    'redis-1': baseInstanceRecord({ current_status: 'stopped', current_linode_id: null }),
  })
  await mockGroupList(page)
  await page.route('**/instances/redis-1/start', async (route: Route) => {
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
          outcome: 'reachability_check_failed',
          instance_id: 999,
          reserved_ip: '192.0.2.10',
          live_status: 'running',
          security_warning: true,
          detail: 'a real, different ED25519 key than the one on record',
          manual_override_expires_at: null,
        },
      },
    })
  })
  await page.goto('/ui/#/instances/redis-1')
  await page.getByRole('button', { name: /^Start$/ }).click()
  const banner = page.locator('div.border-red-600')
  await expect(banner).toBeVisible()
  await expect(banner.getByText(/different SSH host key/i)).toBeVisible()
})
