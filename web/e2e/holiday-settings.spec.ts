import { expect, test } from '@playwright/test'
import type { Route } from '@playwright/test'
import { baseInstanceRecord, loginAs, mockGroupList, mockInstanceList } from './mocks'
test('A group can run on account-wide holidays and add its own holiday range', async ({ page }) => {
  await loginAs(page)
  const rules = [{ days_of_week: ['mon'], start_time: '09:00', stop_time: '18:00' }]
  let mode: 'follow' | 'ignore' = 'follow'
  await mockGroupList(page, [{ id: 1, name: 'prod', timezone: 'UTC', rules, enabled: true, member_count: 0 }])
  await page.route('**/groups/prod', (route: Route) =>
    route.fulfill({
      json: {
        id: 1,
        name: 'prod',
        timezone: 'UTC',
        rules,
        enabled: true,
        members: [],
        depends_on: [],
        dependents: [],
        account_holidays: mode,
      },
    }),
  )
  await page.route('**/groups/prod/savings*', (route: Route) =>
    route.fulfill({
      json: {
        scheduled_savings_percent: null,
        actual_savings_percent: null,
        window_days: 7,
        schedule_state: 'active',
      },
    }),
  )
  await page.route('**/groups/prod/hooks', (route: Route) => route.fulfill({ json: { hooks: null } }))
  await page.route('**/instances', (route: Route) => route.fulfill({ json: {} }))
  let put: unknown = null
  await page.route('**/groups/prod/holiday-setting', async (route: Route) => {
    put = route.request().postDataJSON()
    mode = (
      put as {
        account_holidays: 'follow' | 'ignore'
      }
    ).account_holidays
    await route.fulfill({ json: { account_holidays: mode } })
  })
  let posted: unknown = null
  await page.route('**/holidays*', async (route: Route) => {
    if (route.request().method() === 'POST') {
      posted = route.request().postDataJSON()
      return route.fulfill({ json: { added: ['2026-12-30', '2026-12-31'], warnings: [] } })
    }
    return route.fulfill({
      json: [{ date: '2026-12-25', scope: 'all', target: null, note: 'Xmas', created_at: 'x' }],
    })
  })
  await page.goto('/ui/#/groups/prod')
  await expect(page.getByText('2026-12-25 — account-wide (Xmas)')).toBeVisible()
  await page.getByLabel('Run on account-wide holidays').check()
  await expect.poll(() => put).toEqual({ account_holidays: 'ignore' })
  await expect(page.getByText(/account-wide \(Xmas\) · not applied/)).toBeVisible()
  await page.getByLabel('Group holiday date').fill('2026-12-30')
  await page.getByLabel('Group holiday end date').fill('2026-12-31')
  await page.getByLabel('Group holiday note').fill('Freeze')
  await page.getByRole('button', { name: 'Add for this group' }).click()
  await expect(page.getByText('Added for this group: 2026-12-30, 2026-12-31.')).toBeVisible()
  expect(posted).toEqual({ date: '2026-12-30', to: '2026-12-31', group_name: 'prod', note: 'Freeze' })
})
test('A node can override its group and run on account-wide holidays', async ({ page }) => {
  await loginAs(page)
  const record = baseInstanceRecord({
    current_status: 'running',
    account_holidays: { own: null, effective: 'follow', source: 'default' },
  })
  await mockInstanceList(page, { 'redis-1': record })
  await mockGroupList(page)
  let put: unknown = null
  await page.route('**/instances/redis-1/holiday-setting', async (route: Route) => {
    put = route.request().postDataJSON()
    await route.fulfill({ json: { own: 'ignore', effective: 'ignore', source: 'instance' } })
  })
  await page.goto('/#/instances/redis-1')
  await page.getByLabel('Account-wide holidays for this instance').selectOption('ignore')
  await expect.poll(() => put).toEqual({ account_holidays: 'ignore' })
})
