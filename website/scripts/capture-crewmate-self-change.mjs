/**
 * Capture harness for a crewmate's own change (`update_self`) in its DM thread.
 *
 * Shoots a RUNNING pod (`kirocrew pod up <worktree>`), not a fixture server: the
 * rows under test are written by the gateway when the real agent calls the real
 * tool, so the frames show the product end to end -- the user's ask, the tool
 * row, one `self` row per change (name only, description only, face) with the
 * old face, the new face and Undo, and the crewmate's reply -- then a row after
 * Undo was pressed, and the one state the pod cannot reach on demand: Undo
 * refused because the gateway holds no record any more (answered 410 by a
 * route stub on the undo request alone; everything else on the page is live).
 *
 * The pod and the crewmate are prepared outside this script (a `scout` crewmate
 * whose thread already holds three applied changes: name, description, face);
 * this script only opens the thread and shoots. ONE browser context throughout:
 * the pod token signs in once, and a second sign-in with the same token revokes
 * the first.
 *
 * Usage:
 *   POD_URL=http://127.0.0.1:7840 POD_TOKEN=<token> \
 *     node scripts/capture-crewmate-self-change.mjs [outDir] [member=scout]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const OUT = process.argv[2] || '../temp-screenshots/crewmate-self-change'
const MEMBER = process.argv[3] || 'scout'
const BASE = process.env.POD_URL
const TOKEN = process.env.POD_TOKEN
if (!BASE || !TOKEN) {
  console.error('POD_URL and POD_TOKEN are required (from `kirocrew pod up`)')
  process.exit(2)
}
mkdirSync(OUT, { recursive: true })

const ROW = '[data-testid="self-change-row"]'
const UNDO = '[data-testid="self-change-undo"]'
const THREAD = `/members?member=${encodeURIComponent(MEMBER)}`

/** Navigate the way the sidebar does, in-app: a full load of the member route
 *  right after sign-in is aborted by the shell's own token redirect. */
async function goInApp(page, url) {
  await page.evaluate(u => {
    window.history.pushState({}, '', u)
    window.dispatchEvent(new PopStateEvent('popstate'))
  }, url)
}

async function settle(page) {
  await page.waitForSelector(ROW, { timeout: 30_000 })
  // Let the faces (data-URI SVGs) and the thread's scroll settle.
  await page.waitForTimeout(800)
}

async function setTheme(page, mode) {
  await page.emulateMedia({ colorScheme: mode })
  await page.evaluate(m => localStorage.setItem('mc-theme', m), mode)
  await page.reload({ waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(500)
  await goInApp(page, THREAD)
  await settle(page)
}

const browser = await chromium.launch()
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 860 }, deviceScaleFactor: 2 })
  await page.goto(`${BASE}/?token=${encodeURIComponent(TOKEN)}`, { waitUntil: 'domcontentloaded' })
  // A pod is a fresh install: its first-run chapters (import, privacy) and the
  // Crewmates page's own "Meet CrewMates" intro would sit over the thread. The
  // same keys the e2e auth setup seeds mark them done.
  await page.evaluate(() => {
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-crewmates-onboarded', '1')
    localStorage.removeItem('mc-crewmates-pending')
  })
  await page.waitForTimeout(800)

  for (const scheme of ['light', 'dark']) {
    await setTheme(page, scheme)
    await page.screenshot({ path: `${OUT}/thread-after-change-${scheme}.png` })
    // One frame per row kind, in the order the changes were made.
    const rows = page.locator(ROW)
    const n = await rows.count()
    for (let i = 0; i < n; i++) {
      await rows.nth(i).screenshot({ path: `${OUT}/row-${i + 1}-applied-${scheme}.png` })
    }
  }

  // Undo refused: the gateway no longer holds the record (a restart, or past the
  // newest 256). Only the undo request is stubbed; the row, the thread and the
  // rest of the page are the live pod's.
  await setTheme(page, 'light')
  await page.route(/\/self-changes\/[^/]+\/undo$/, route => route.fulfill({
    status: 410,
    contentType: 'application/json',
    body: JSON.stringify({ error: 'this change can no longer be undone here', code: 'undo_unavailable' }),
  }))
  await page.locator(UNDO).first().click()
  await page.waitForSelector('[data-testid="self-change-undo-error"]', { timeout: 15_000 })
  await page.waitForTimeout(500)
  await page.locator(ROW).first().screenshot({ path: `${OUT}/row-undo-unavailable-light.png` })
  await page.unroute(/\/self-changes\/[^/]+\/undo$/)

  // Undo once for real, on the last row, and shoot the row that now reads "undone".
  await page.locator(UNDO).last().click()
  await page.waitForSelector(`${ROW}[data-self-status="undone"]`, { timeout: 15_000 })
  await page.waitForTimeout(800)
  await page.screenshot({ path: `${OUT}/thread-after-undo-light.png` })
  await page.locator(`${ROW}[data-self-status="undone"]`).last().screenshot({ path: `${OUT}/row-undone-light.png` })
  console.log(`wrote ${OUT}`)
} finally {
  await browser.close()
}
