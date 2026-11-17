import { expect, test } from '@playwright/test'

/**
 * 调度批次清单与待下发任务清单的验收用例。
 * 需要后端与前端 dev server 已经启动（E2E_BASE_URL 指向前端）。
 */
test('调度批次序列：生成、合并触发、生命周期推进与任务清单', async ({ page }) => {
  test.setTimeout(120_000)
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))
  page.on('response', (r) => {
    if (r.url().includes('/api/') && r.status() >= 400) errors.push(`${r.status()} ${r.url()}`)
  })

  await page.goto('/schedule')
  const panel = page.getByTestId('batch-panel')
  await expect(panel).toBeVisible()

  await page.getByTestId('plan-select').selectOption('3')
  await page.getByTestId('batch-count').fill('2')
  await page.getByTestId('generate-batches').click()
  await expect(page.getByTestId('batch-message')).toContainText('调度批次')

  const rows = page.locator('tbody tr[data-testid^="batch-row-"]')
  await expect(rows).toHaveCount(2)
  await expect(rows.first()).toHaveAttribute('data-status', '待执行')

  await page.getByTestId('generate-manual-duplicate').click()
  await expect(page.getByTestId('batch-message')).toContainText('未重复写入')
  await expect(rows).toHaveCount(2)
  await expect(page.getByTestId('merged-tag')).toHaveCount(2)

  const firstId = (await rows.first().getAttribute('data-testid'))!.replace('batch-row-', '')
  await page.getByTestId(`batch-action-${firstId}-标记完成`).click()
  await expect(page.getByTestId('batch-error')).toContainText('依次推进')
  await page.getByTestId(`batch-action-${firstId}-开始执行`).click()
  await expect(rows.first()).toHaveAttribute('data-status', '执行中')
  await page.getByTestId(`batch-action-${firstId}-标记完成`).click()
  await expect(rows.first()).toHaveAttribute('data-status', '已完成')
  await page.getByTestId(`batch-action-${firstId}-重试执行`).click()
  await expect(page.getByTestId('batch-error')).toContainText('不允许回退')

  const batchDownload = page.waitForEvent('download')
  await page.getByTestId('export-batches').click()
  expect((await batchDownload).suggestedFilename()).toBe('schedule-batches.csv')

  await page.goto('/crawler')
  await expect(page.getByTestId('dispatch-panel')).toBeVisible()
  await page.getByTestId('dispatch-plan-select').selectOption('3')
  const tasks = page.locator('tbody tr[data-testid^="dispatch-row-"]')
  await expect(tasks.first()).toBeVisible()
  const windowText = await tasks.first().locator('td').nth(2).innerText()
  const dispatchText = await tasks.first().locator('td').nth(3).innerText()
  const [windowStart, windowEnd] = windowText.split(' ~ ')
  expect(dispatchText >= windowStart && dispatchText <= windowEnd).toBeTruthy()

  const taskDownload = page.waitForEvent('download')
  await page.getByTestId('export-dispatches').click()
  expect((await taskDownload).suggestedFilename()).toBe('crawler-dispatches.csv')

  expect(errors).toEqual([])
})
