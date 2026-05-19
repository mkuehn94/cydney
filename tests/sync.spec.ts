import { expect, test } from '@playwright/test'

test.use({ baseURL: 'http://127.0.0.1:5176' })
test.describe.configure({ mode: 'serial' })

test('browser mirrors the shared session', async ({ page, context }) => {
  const created = page.waitForResponse(r => r.url().endsWith('/api/device/sessions'))
  await page.goto('/')
  const session = await (await created).json()
  expect(session.shared).toBe(true)
  await expect(page.locator('.device-screen img')).toBeVisible()
  await expect(page.locator('.keyboard-controls button').first()).toBeEnabled()
  const mirror = await context.newPage()
  const attached = mirror.waitForResponse(r => r.url().endsWith('/api/device/sessions'))
  await mirror.goto('/?device=1')
  expect((await (await attached).json()).id).toBe(session.id)
  await expect(mirror.locator('.device-screen img')).toBeVisible()
  const action = page.waitForResponse(r => new URL(r.url()).pathname.endsWith('/action'))
  await page.getByRole('button', { name: /^controls/i }).click()
  const bundle = await (await action).json()
  expect(bundle.page).toBe('menu')
  await expect(mirror.locator('.device-screen img')).toHaveAttribute('src', bundle.image)
  const reattached = page.waitForResponse(r => r.url().endsWith('/api/device/sessions'))
  await page.reload()
  expect((await (await reattached).json()).id).toBe(session.id)
  await expect(page.locator('.device-screen img')).toHaveAttribute('alt', /CYD menu frame/)
})

test('reconnect after expired session', async ({ page }) => {
  let expire = false
  await page.route('**/frame.json', async route => {
    if (expire) {
      expire = false
      await route.fulfill({ status: 404, json: { detail: 'Device session expired or does not exist' } })
    } else await route.continue()
  })
  const created = page.waitForResponse(r => r.url().endsWith('/api/device/sessions'))
  await page.goto('/')
  const session = await (await created).json()
  await expect(page.locator('.device-screen img')).toBeVisible()
  const reconnected = page.waitForResponse(r => r.url().endsWith('/api/device/sessions'))
  expire = true
  expect((await (await reconnected).json()).id).toBe(session.id)
  await expect(page.locator('.device-screen img')).toBeVisible()
  await expect(page.getByRole('alert')).toHaveCount(0)
})
