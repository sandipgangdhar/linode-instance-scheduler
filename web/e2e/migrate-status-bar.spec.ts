import { expect, test } from '@playwright/test'
import type { Route } from '@playwright/test'
import { loginAs, mockGroupList, mockInstanceList } from './mocks'
test('status bar offers "click to view" only away from the migration page, and it works', async ({
  page,
}) => {
  await loginAs(page)
  await mockInstanceList(page, {})
  await mockGroupList(page)
  await page.route('**/instances/web1/migrate-status', (r: Route) =>
    r.fulfill({ json: { in_progress: false } }),
  )
  await page.route('**/linode/instances*', (r: Route) => r.fulfill({ json: [] }))
  await page.route('**/instances/web1/migrate-start', (r: Route) =>
    r.fulfill({ json: { operation_id: 'op1', total_steps: 6 } }),
  )
  await page.route('**/operations/op1', (r: Route) =>
    r.fulfill({
      json: {
        status: 'running',
        action: 'migrate_start',
        percent: 17,
        warnings: [],
        current_step: 'Running pre-flight checks (cloud-init version, datasource, networking)...',
      },
    }),
  )
  await page.goto('/ui/#/migrate?name=web1&instanceId=123')
  await page.locator('input').last().fill('web1')
  await page.getByRole('button', { name: /Start migration now/ }).click()
  const bar = page.getByRole('status')
  await expect(bar).toContainText('Running pre-flight checks')
  await expect(bar).not.toContainText('click to view')
  await page.goto('/ui/#/instances')
  await expect(bar).toContainText('click to view')
  await bar.click()
  await expect(page).toHaveURL(/#\/migrate\?name=web1&instanceId=123/)
  await expect(page.locator('main')).toContainText('already in progress')
  await expect(page.locator('main')).toContainText('Running pre-flight checks')
  await expect(bar).not.toContainText('click to view')
})
