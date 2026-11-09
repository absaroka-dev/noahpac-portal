import { expect, test } from '@playwright/test';

const PAGE = '/usaf-assistant/web/';

test.describe('USAF assistant workspace', () => {
  test.beforeEach(async ({ page }) => {
    await page.route('**/usaf-assistant/api/library', async route => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          built: '2026-11-08T20:55:02',
          documents: [{
            short: 'MSD', title: 'Medical Standards Directory', category: 'standards',
            version: '5 Jun 2026', chunks: 40, file: 'MSD.pdf',
          }],
        }),
      });
    });
    await page.goto(PAGE);
  });

  test('presents a guided workflow without changing assistant behavior', async ({ page }) => {
    await expect(page.getByRole('heading', { name: 'Med Standards Assistant' })).toBeVisible();
    await expect(page.getByText('De-identified input only')).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Choose what you’re working on' })).toBeVisible();
    await expect(page.locator('.mode-tab')).toHaveCount(4);
    await expect(page.locator('#new-conversation')).toBeHidden();
    await expect(page.locator('#empty-state')).toBeVisible();
    await expect(page.locator('#library-summary')).toContainText('Loaded publications (1)');
  });

  test('mode selector supports pointer and arrow-key navigation', async ({ page }) => {
    const standards = page.locator('[data-mode="standards"]');
    const narsum = page.locator('[data-mode="narsum"]');
    await standards.focus();
    await page.keyboard.press('ArrowRight');
    await expect(narsum).toBeFocused();
    await expect(narsum).toHaveAttribute('aria-selected', 'true');
    await expect(page.locator('#question-input')).toHaveAttribute('placeholder', /Clinical facts for the NARSUM/);

    await page.locator('[data-mode="waiver"]').click();
    await expect(page.locator('#mode-help')).toContainText('required-items checklist');
  });

  test('mobile layout has touch-sized controls and no horizontal overflow', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    const metrics = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    }));
    expect(metrics.scrollWidth).toBeLessThanOrEqual(metrics.clientWidth);
    for (const button of await page.locator('.mode-tab').all()) {
      expect((await button.boundingBox())?.height).toBeGreaterThanOrEqual(44);
    }
    await expect(page.locator('#ask-form')).toHaveCSS('position', 'static');
  });

  test('manual dark theme keeps the workspace on dark surfaces', async ({ page }) => {
    await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'));
    const colors = await page.locator('.workflow-card').evaluate(node => {
      const style = getComputedStyle(node);
      return { background: style.backgroundColor, color: style.color };
    });
    expect(colors.background).not.toBe('rgb(255, 255, 255)');
    expect(colors.color).not.toBe('rgb(26, 31, 36)');
  });
});
