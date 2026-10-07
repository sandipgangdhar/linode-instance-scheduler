import { expect, test } from '@playwright/test'
import type { Route } from '@playwright/test'
import { baseInstanceRecord, loginAs, mockGroupList, mockInstanceList } from './mocks'
test('a failed pre-stop hook shows its output and offers stopping without the hook', async ({ page }) => {
  await loginAs(page)
  await mockInstanceList(page, { 'pg-1': baseInstanceRecord({ label: 'pg-1' }) })
  await mockGroupList(page)
  const stopBodies: Array<Record<string, unknown>> = []
  await page.route('**/instances/pg-1/stop', async (route: Route) => {
    const body = route.request().postDataJSON() as Record<string, unknown>
    stopBodies.push(body)
    await route.fulfill({ json: { operation_id: body.skip_hooks ? 'op-stop-2' : 'op-stop-1' } })
  })
  await page.route('**/operations/op-stop-1', async (route: Route) => {
    await route.fulfill({
      json: {
        status: 'done',
        action: 'stop',
        percent: 100,
        current_step: null,
        warnings: [],
        result: {
          outcome: 'pre_stop_hook_failed',
          instance_id: 999,
          detail:
            "pre-stop hook exited with code 1. Stop aborted -- nothing was shut down or deleted; 'pg-1' is still running.",
          hook_output: 'pg_ctl: server does not shut down',
        },
      },
    })
  })
  await page.route('**/operations/op-stop-2', async (route: Route) => {
    await route.fulfill({
      json: {
        status: 'done',
        action: 'stop',
        percent: 100,
        current_step: null,
        warnings: [],
        result: { outcome: 'stopped', instance_id: 999, detail: null },
      },
    })
  })
  await page.goto('/ui/#/instances/pg-1')
  await page.getByRole('button', { name: /^Stop$/ }).click()
  await page.locator('input').first().fill('pg-1')
  await page.getByRole('button', { name: 'Confirm stop' }).click()
  const main = page.getByRole('main')
  await expect(main.getByText(/pg_ctl: server does not shut down/)).toBeVisible()
  await expect(main.getByText(/still running/)).toBeVisible()
  const stopAnyway = page.getByRole('button', { name: 'Stop anyway, without the pre-stop hook' })
  await expect(stopAnyway).toBeVisible()
  await stopAnyway.click()
  await expect.poll(() => stopBodies.length).toBe(2)
  expect(stopBodies[0].skip_hooks).toBe(false)
  expect(stopBodies[1].skip_hooks).toBe(true)
})
test('saving hooks sends the normalized config', async ({ page }) => {
  await loginAs(page)
  await mockInstanceList(page, { 'pg-1': baseInstanceRecord({ label: 'pg-1' }) })
  await mockGroupList(page)
  let putBody: Record<string, unknown> | null = null
  await page.route('**/instances/pg-1/hooks', async (route: Route) => {
    if (route.request().method() !== 'PUT') return route.fallback()
    putBody = route.request().postDataJSON() as Record<string, unknown>
    await route.fulfill({ json: { own: putBody } })
  })
  await page.goto('/ui/#/instances/pg-1')
  await page.getByLabel('Pre-stop command').fill('pg_ctlcluster 16 main stop -m fast')
  await page.getByLabel('Pre-stop failure policy').selectOption('continue')
  await page.getByLabel('Post-start command').fill('pg_isready -q')
  await page.getByLabel('Post-start timeout').fill('120')
  await page.getByRole('button', { name: 'Save hooks' }).click()
  await expect.poll(() => putBody).not.toBeNull()
  expect(putBody).toEqual({
    pre_stop: { command: 'pg_ctlcluster 16 main stop -m fast', timeout_s: 300, on_failure: 'continue' },
    post_start: { command: 'pg_isready -q', timeout_s: 120 },
  })
})
test('a hook can be an uploaded script file instead of a command', async ({ page }) => {
  await loginAs(page)
  await mockInstanceList(page, { 'pg-1': baseInstanceRecord({ label: 'pg-1' }) })
  await mockGroupList(page)
  let putBody: Record<string, unknown> | null = null
  await page.route('**/instances/pg-1/hooks', async (route: Route) => {
    if (route.request().method() !== 'PUT') return route.fallback()
    putBody = route.request().postDataJSON() as Record<string, unknown>
    await route.fulfill({ json: { own: putBody } })
  })
  await page.goto('/ui/#/instances/pg-1')
  await page.getByRole('radiogroup', { name: 'Pre-stop hook type' }).getByLabel('Upload a script').check()
  await page.getByLabel('Pre-stop script file').setInputFiles({
    name: 'before-stop.sh',
    mimeType: 'text/x-shellscript',
    buffer: Buffer.from('#!/bin/bash\npg_ctlcluster 16 main stop -m fast\n'),
  })
  await expect(page.getByLabel('Pre-stop script', { exact: true })).toHaveValue(/pg_ctlcluster 16 main stop/)
  await page.getByRole('button', { name: 'Save hooks' }).click()
  await expect.poll(() => putBody).not.toBeNull()
  expect(putBody).toEqual({
    pre_stop: {
      script: '#!/bin/bash\npg_ctlcluster 16 main stop -m fast\n',
      timeout_s: 300,
      on_failure: 'abort',
    },
    post_start: null,
  })
})
