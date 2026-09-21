import { expect, test } from '@playwright/test'
import { baseInstanceRecord, loginAs, mockGroupList, mockInstanceList } from './mocks'
test('an unauthenticated visit redirects to the login page', async ({ page }) => {
  await page.goto('/ui/#/instances')
  await expect(page).toHaveURL(/#\/login/)
})
test('the instances list renders every onboarded instance and links to its detail page', async ({ page }) => {
  await loginAs(page)
  await mockInstanceList(page, {
    'redis-1': baseInstanceRecord({ current_status: 'running' }),
    'redis-2': baseInstanceRecord({ current_status: 'stopped', current_linode_id: null }),
  })
  await mockGroupList(page)
  await page.goto('/ui/#/instances')
  await expect(page.getByRole('link', { name: /redis-1/ })).toBeVisible()
  await expect(page.getByRole('link', { name: /redis-2/ })).toBeVisible()
  await page.getByRole('link', { name: /redis-1/ }).click()
  await expect(page).toHaveURL(/#\/instances\/redis-1/)
  await expect(page.getByText('running', { exact: true })).toBeVisible()
})
