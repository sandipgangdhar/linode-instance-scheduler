import { expect, test } from '@playwright/test'
import type { Route } from '@playwright/test'
import { loginAs, mockGroupList, mockInstanceList } from './mocks'
const STATUS = {
  in_progress: true,
  phase: 'awaiting_manual_dd',
  instance_id: 111,
  dest_volume_size_gb: 30,
  local_disk_size_mb: 25088,
}
test('Without a kept backup the assisted copy panel is not offered', async ({ page }) => {
  await loginAs(page)
  await mockInstanceList(page, {})
  await mockGroupList(page)
  await page.route('**/instances/web1/migrate-status', (r: Route) => r.fulfill({ json: STATUS }))
  await page.route('**/instances/web1/migrate-copy', (r: Route) =>
    r.fulfill({
      json: {
        available: false,
        reasons: ['no pre-migration backup is kept'],
        username: 'alice',
        gateway: 'lish-in-bom-2.linode.com',
        public_key: 'ssh-ed25519 AAA deploy',
        key_registered: true,
        keys_allowed: true,
        backup_kept: false,
        instance_id: 111,
      },
    }),
  )
  await page.goto('/ui/#/migrate?name=web1&instanceId=111')
  await expect(page.getByText('Step 1 of 3')).toBeVisible()
  await expect(page.getByText('Let the tool run the copy for you')).toHaveCount(0)
})
test('Setup instructions show the key until it is registered, then Run the copy for me finishes and onboards', async ({
  page,
}) => {
  await loginAs(page)
  await mockInstanceList(page, {})
  await mockGroupList(page)
  await page.route('**/instances/web1/migrate-status', (r: Route) => r.fulfill({ json: STATUS }))
  let registered = false
  let copyPosted = false
  await page.route('**/instances/web1/migrate-copy', async (r: Route) => {
    if (r.request().method() === 'POST') {
      copyPosted = true
      return r.fulfill({ json: { operation_id: 'op-c', total_steps: 12 } })
    }
    return r.fulfill({
      json: {
        available: registered,
        reasons: registered ? [] : ["this deployment's public key is not one of the profile's Lish keys"],
        username: 'alice',
        gateway: 'lish-in-bom-2.linode.com',
        public_key: 'ssh-ed25519 AAAATEST deploy@host',
        key_registered: registered,
        keys_allowed: true,
        backup_kept: true,
        instance_id: 111,
      },
    })
  })
  await page.route('**/operations/op-c', (r: Route) =>
    r.fulfill({
      json: {
        status: 'done',
        action: 'migrate_copy',
        percent: 100,
        current_step: null,
        warnings: [],
        result: {
          outcome: 'resumed',
          detail: null,
          resume: {
            outcome: 'resumed',
            instance_id: 111,
            os_volume_id: 200,
            reserved_ip: '203.0.113.5',
            previous_attempts_count: 0,
            detail: null,
            fstab_entries_disabled: [],
          },
        },
      },
    }),
  )
  let onboarded: unknown = null
  await page.route('**/instances', async (r: Route) => {
    if (r.request().method() === 'POST') {
      onboarded = r.request().postDataJSON()
      return r.fulfill({ json: { warnings: [] } })
    }
    return r.fallback()
  })
  await page.goto('/ui/#/migrate?name=web1&instanceId=111')
  await expect(page.getByText('Let the tool run the copy for you')).toBeVisible()
  await expect(page.getByText('ssh-ed25519 AAAATEST deploy@host')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Run the copy for me' })).toHaveCount(0)
  registered = true
  await page.getByRole('button', { name: 'Check again' }).click()
  await page.getByRole('button', { name: 'Run the copy for me' }).click()
  await expect.poll(() => copyPosted).toBe(true)
  await expect.poll(() => onboarded).toMatchObject({ name: 'web1', instance_id: 111 })
})
