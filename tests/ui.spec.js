const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

const RESULTS_FILE = path.join(__dirname, 'test_output.json');

function saveResult(key, value) {
  const existing = fs.existsSync(RESULTS_FILE)
    ? JSON.parse(fs.readFileSync(RESULTS_FILE, 'utf8'))
    : {};
  existing[key] = value;
  fs.writeFileSync(RESULTS_FILE, JSON.stringify(existing, null, 2));
}

// ── 1. Health endpoint ────────────────────────────────────────────────────────
test('health endpoint returns healthy JSON', async ({ request }) => {
  const res = await request.get('http://localhost:5000/health');
  expect(res.status()).toBe(200);
  const body = await res.json();
  expect(body.status).toBe('healthy');
  expect(body.service).toBe('QA-PDF-Extractor-API');
  saveResult('health', body);
  console.log('Health:', JSON.stringify(body));
});

// ── 2. Frontend loads ─────────────────────────────────────────────────────────
test('frontend loads and shows correct heading', async ({ page }) => {
  await page.goto('http://localhost:3000');
  const heading = page.locator('h1');
  await expect(heading).toContainText('QA PDF Extractor');
});

// ── 3. API Connected status indicator ────────────────────────────────────────
test('frontend shows API Connected status', async ({ page }) => {
  await page.goto('http://localhost:3000');
  const statusEl = page.locator('.api-status');
  await expect(statusEl).toBeVisible({ timeout: 10_000 });
  await expect(statusEl).toContainText('API Connected', { timeout: 10_000 });
  saveResult('apiStatusText', await statusEl.innerText());
});

// ── 4. Buttons / mode tabs are visible ────────────────────────────────────────
test('mode buttons are visible on page', async ({ page }) => {
  await page.goto('http://localhost:3000');
  const buttons = page.locator('button');
  await expect(buttons.first()).toBeVisible({ timeout: 8_000 });
  const count = await buttons.count();
  console.log(`Buttons found: ${count}`);
  expect(count).toBeGreaterThan(0);
  saveResult('buttonCount', count);
});

// ── 5. API returns JSON error when files are missing ──────────────────────────
test('extract endpoint returns JSON error on missing files', async ({ request }) => {
  const res = await request.post('http://localhost:5000/api/extract', {
    multipart: {},
  });
  expect(res.status()).toBe(400);
  const body = await res.json();
  expect(body).toHaveProperty('error');
  saveResult('missingFilesError', body);
  console.log('Error response:', JSON.stringify(body));
});
