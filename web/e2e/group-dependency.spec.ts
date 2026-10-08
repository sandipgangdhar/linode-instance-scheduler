import { expect, test } from '@playwright/test'
import type { Route } from '@playwright/test'
import { loginAs, mockGroupList } from './mocks'
test('setting several group dependencies sends one PATCH and shows the result', async ({ page }) => {
  await loginAs(page)
  await mockGroupList(page, [
    { id: 1, name: 'app', timezone: 'UTC', rules: [], enabled: true, member_count: 0 },
    { id: 2, name: 'db', timezone: 'UTC', rules: [], enabled: true, member_count: 0 },
    { id: 3, name: 'cache', timezone: 'UTC', rules: [], enabled: true, member_count: 0 },
  ])
  let dependsOn: string[] = []
  const patches: unknown[] = []
  const group = () => ({
    id: 1,
    name: 'app',
    timezone: 'UTC',
    rules: [],
    enabled: true,
    members: [],
    depends_on: dependsOn,
    dependents: [],
  })
  await page.route('**/groups/app', async (route: Route) => {
    const req = route.request()
    if (req.method() === 'PATCH') {
      const body = req.postDataJSON() as {
        depends_on: string[]
      }
      patches.push(body)
      dependsOn = body.depends_on
    }
    await route.fulfill({ json: group() })
  })
  await page.route('**/groups/app/savings*', async (route: Route) => {
    await route.fulfill({
      json: {
        scheduled_savings_percent: null,
        actual_savings_percent: null,
        window_days: 7,
        schedule_state: 'none',
      },
    })
  })
  await page.route('**/groups/app/hooks', async (route: Route) => {
    await route.fulfill({ json: { hooks: null } })
  })
  await page.route('**/instances', async (route: Route) => {
    await route.fulfill({ json: {} })
  })
  await page.goto('/ui/#/groups/app')
  const db = page.getByLabel('Depends on db')
  const cache = page.getByLabel('Depends on cache')
  await expect(db).toBeEnabled()
  const save = page.getByRole('button', { name: 'Save start order' })
  await expect(save).toBeDisabled()
  await db.check()
  await cache.check()
  await save.click()
  await expect.poll(() => patches).toEqual([{ depends_on: ['cache', 'db'] }])
  await expect(db).toBeChecked()
  await expect(cache).toBeChecked()
  await expect(page.getByText('Starts after')).toContainText('cache, db')
})
