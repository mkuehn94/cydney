import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'

async function ready(page: Page, url = '/') {
  await page.goto(url)
  await expect(page.locator('.device-screen img')).toBeVisible()
  if (!url.includes('device=1')) await expect(page.locator('.keyboard-controls button').first()).toBeEnabled()
}

async function tap(page: Page, x: number, y: number, scale = 1) {
  const response = page.waitForResponse(r => new URL(r.url()).pathname.endsWith('/touch') && r.request().method() === 'POST')
  await page.getByTestId('device-screen').click({ position: { x: x * scale, y: y * scale } })
  const state = await (await response).json()
  await expect(page.locator('.device-screen img')).toHaveAttribute('src', state.image)
  return state
}

test('touch navigation in both orientations', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await ready(page)
  for (const [width, height] of [[320, 240], [240, 320]]) {
    if (width === 240) {
      await page.getByRole('button', { name: '240 × 320', exact: true }).click()
      await expect(page.locator('.device-screen img')).toHaveAttribute('width', '240')
      await expect(page.locator('.keyboard-controls button').first()).toBeEnabled()
    }
    expect(await page.locator('.device-screen img').evaluate((e: HTMLImageElement) => [e.naturalWidth, e.naturalHeight])).toEqual([width, height])
    expect((await tap(page, width - 20, 20)).page).toBe('menu')
    expect((await tap(page, 60, 76)).page).toBe('feed')
  }
  expect(errors).toEqual([])
})

for (const scale of [1, 2, 3]) {
  test(`marker card at ${scale}x`, async ({ page }) => {
    await ready(page)
    await page.getByRole('button', { name: `${scale} times preview magnification`, exact: true }).click()
    const box = await page.getByTestId('device-screen').boundingBox()
    expect([box?.width, box?.height]).toEqual([320 * scale, 240 * scale])
    let state = await tap(page, 288, 131, scale)
    expect(state).toMatchObject({ page: 'map', card_open: true, selected_object: { id: 'bus-m29' } })
    await expect(page.locator('.device-screen img')).toHaveAttribute('alt', /selected M29 toward Hermannplatz/)
    state = await tap(page, 290, 55, scale)
    expect(state).toMatchObject({ page: 'map', card_open: false })
    expect((await tap(page, 300, 20, scale)).page).toBe('menu')
  })
}

test('frame only mode', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 240 })
  await ready(page, '/?device=1')
  expect(await page.evaluate(() => [document.documentElement.scrollWidth, document.documentElement.scrollHeight])).toEqual([320, 240])
  await expect(page.locator('.preview-header, .preview-intro, .keyboard-controls')).toHaveCount(0)
  expect(await tap(page, 60, 60)).toMatchObject({ page: 'map', card_open: false, selected_object: null })
  await page.mouse.move(200, 100)
  const previous = await page.locator('.device-screen img').getAttribute('src')
  const action = page.waitForResponse(r => new URL(r.url()).pathname.endsWith('/action'))
  await page.mouse.wheel(0, -120)
  expect((await (await action).json()).zoom).toBe(16)
  await expect(page.locator('.device-screen img')).not.toHaveAttribute('src', previous!)
  expect((await tap(page, 300, 20)).page).toBe('menu')
})

test('wheel zoom at 2x', async ({ page }) => {
  await ready(page)
  await page.getByRole('button', { name: '2 times preview magnification', exact: true }).click()
  const box = (await page.getByTestId('device-screen').boundingBox())!
  await page.mouse.move(box.x + 480, box.y + 160)
  const scroll = await page.evaluate(() => window.scrollY)
  const response = page.waitForResponse(r => new URL(r.url()).pathname.endsWith('/action') && r.request().postDataJSON().kind === 'zoom')
  await page.mouse.wheel(0, -120)
  const result = await response
  expect(result.request().postDataJSON()).toMatchObject({ value: 'in' })
  const { x, y } = result.request().postDataJSON()
  expect(Math.abs(x - 240)).toBeLessThanOrEqual(1)
  expect(Math.abs(y - 80)).toBeLessThanOrEqual(1)
  expect((await result.json()).zoom).toBe(16)
  expect(await page.evaluate(() => window.scrollY)).toBe(scroll)
})

test('offline server shows error', async ({ page }) => {
  await page.route('**/api/device/sessions', route => route.abort())
  await page.goto('/')
  await expect(page.getByRole('alert')).toBeVisible()
  await expect(page.getByText('SERVER OFFLINE', { exact: true })).toBeVisible()
  await expect(page.locator('.device-screen img')).toHaveCount(0)
})
