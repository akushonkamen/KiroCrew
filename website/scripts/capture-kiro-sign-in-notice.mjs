/**
 * Screenshot harness for the app-level Kiro sign-in notice.
 *
 * Runs the REAL built SPA behind `serveDist`, every /api/** call answered from
 * fixtures by `stubDashboardApi`. No gateway, no kiro-cli, no credential: the
 * `/api/kas-login` answers are the token-free status bodies the gateway serves.
 * The config names KAS as the agent backend, the one backend the notice
 * reports for.
 *
 * Each frame ASSERTS the notice's state before it writes the PNG, so a stale
 * bundle fails loudly instead of producing a plausible picture:
 *
 *   01 expired, dark, chat page        -> status notice, two controls
 *   02 refused, light, schedule page   -> ErrorNotice under its own title
 *   03 healthy, dark                   -> no notice
 *   04 click "Sign in again in Settings" -> lands on Settings > Agent Harness,
 *                                         where the notice is not shown
 *   05/06 phone width (390px)          -> the full notice, text and button
 *   07 status read fails (HTTP 500)    -> ErrorNotice with the agent hand-off
 *   08 320px, Spanish                  -> label wraps, dismiss stays in view
 *
 * `--before` captures the expired state against a build WITHOUT the notice
 * (pass that build with `--dist`) and asserts it is absent there.
 * `--verify-only` runs the assertions and writes nothing.
 *
 * Usage: node scripts/capture-kiro-sign-in-notice.mjs [outDir] [--dist <dir>] [--before] [--verify-only]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { KIROCREW_CONFIG_FIXTURE, logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

// The node toolchain injects its own libstdc++ on LD_LIBRARY_PATH, which the
// bundled Chromium then loads in preference to the system one and fails on.
delete process.env.LD_LIBRARY_PATH

const args = process.argv.slice(2)
const VERIFY_ONLY = args.includes('--verify-only')
const BEFORE = args.includes('--before')
const distAt = args.indexOf('--dist')
const DIST = distAt >= 0 ? args[distAt + 1] : DEFAULT_DIST
const OUT = args.find((a, i) => !a.startsWith('--') && args[i - 1] !== '--dist') || '../temp-screenshots/kiro-sign-in-notice'
if (!VERIFY_ONLY) mkdirSync(OUT, { recursive: true })

const KAS_CONFIG = { ...KIROCREW_CONFIG_FIXTURE, agent: { ...KIROCREW_CONFIG_FIXTURE.agent, acp_backend: 'kas' } }
const HEALTHY = {
  authenticated: true, provider: 'Google', identity: 'social', transport: 'device',
  expires_at: '2099-01-01T00:00:00Z', expired: false, has_refresh_token: true,
  refresh_rejected: false, usable: true,
}
const EXPIRED = { ...HEALTHY, expires_at: '2000-01-01T00:00:00Z', expired: true, has_refresh_token: false, usable: false }
const REFUSED = { ...HEALTHY, refresh_rejected: true }
const FAILED = Symbol('status read fails')
const NOW_ISO = new Date(Date.now() - 60_000).toISOString()
const SLOT = { key: 'chat-1', title: 'Release notes draft', messages: 4, running: false, agent: '', mode: '', tags: [], last_ts: NOW_ISO, last_turn_ts: NOW_ISO }
const KAS_ROW = {
  id: 'kas', policy_id: 'kas', selectable: true, independent_setup: false,
  installed: 'installed', missing_components: [], install_command: '', restart_required: false,
  capabilities: [{ id: 'crew_tools', available: true }],
  security_notes: [], operator_notes: [], tool_approval: 'agent_spec', offered_by_build: true,
  mcp: { per_tool_deny: 'settings-file', costs_whole_server: false, ineffective: [] },
}

async function main() {
  const { srv, base } = await serveDist(DIST)
  const browser = await chromium.launch()
  const results = []

  async function frame(name, { status, theme = 'dark', want, click = false, width = 1280, route = '/', lang = null }) {
    const context = await browser.newContext({ viewport: { width, height: 900 }, deviceScaleFactor: 2 })
    const page = await context.newPage()
    logPageProblems(page)
    const extra = async (path, route) => {
      if (path === '/api/config/kirocrew') { await json(route, KAS_CONFIG); return true }
      if (path === '/api/kas-login') {
        // A failed read answers the way the handler does when the store cannot be opened.
        if (status === FAILED) await json(route, { error: 'token_store_failed: the sign-in store could not be read', code: 'token_store_failed' }, 500)
        else await json(route, status)
        return true
      }
      // The shell's agent pickers need a real roster to boot past the app-shell
      // boundary (the catch-all's `[]` guess is not one).
      if (path === '/api/agents' || path === '/api/chat/agents' || path === '/api/agents/catalog') {
        await json(route, { agents: [{ name: 'kirocrew', source: 'builtin' }], default_agent: 'kirocrew' })
        return true
      }
      if (path.startsWith('/api/chat/slots/')) {
        await json(route, { running: false, has_more: false, total: 1, queue: [], messages: [{ role: 'user', ts: Date.now() / 1000 - 300, content: 'hello' }] })
        return true
      }
      // Settings > Agent Harness, where the click lands: one KAS row, trimmed to
      // what the picker reads, so the ringed sign-in card renders.
      if (path === '/api/acp-backends') { await json(route, { backends: [KAS_ROW] }); return true }
      if (path === '/api/config/schema') {
        await json(route, { entries: [{ path: 'agent.acp_backend', type: 'enum', enumValues: ['', 'kas'] }] })
        return true
      }
      return false
    }
    await stubDashboardApi(page, { slots: [SLOT], theme, extra, localStorageEntries: { 'mc-active-slot': SLOT.key, ...(lang ? { 'mc-lang': lang } : {}) } })
    await page.goto(base + route, { waitUntil: 'domcontentloaded' })
    await page.locator('header').first().waitFor()

    const notice = page.getByTestId(want?.testId ?? 'kiro-sign-in-notice')
    let got = null
    if (want) {
      await notice.waitFor({ timeout: 20_000 }).catch(() => {})
      // Let the notice's rise-in animation finish so the frame is not mid-fade.
      await page.waitForTimeout(800)
    } else {
      // Long enough for the config and status reads to land and a notice to mount.
      await page.waitForTimeout(2500)
    }
    if (await notice.count()) {
      const open = notice.getByTestId('kiro-sign-in-notice-open')
      const dismiss = notice.getByRole('button', { name: want?.dismissName ?? 'Dismiss' })
      const dismissBox = await dismiss.count() ? await dismiss.boundingBox() : null
      got = {
        visible: await notice.isVisible(),
        text: (await notice.innerText()).replace(/\s+/g, ' ').trim(),
        buttons: await notice.getByRole('button').count(),
        open: await open.count() ? await open.isVisible() : false,
        // The dismiss is fully inside the window, not clipped at its edge.
        dismissInside: !!dismissBox && dismissBox.x >= 0 && dismissBox.x + dismissBox.width <= width,
      }
    }
    const ok = !want
      ? got === null
      : !!got && got.visible && got.text.startsWith(want.title) && got.buttons === want.buttons
        && got.open === want.open && got.dismissInside
    const row = { name, ok, notice: got }

    if (!VERIFY_ONLY) {
      // The top of the window: the bar, and the notice under it when present.
      const box = got ? await notice.boundingBox() : await page.locator('header').first().boundingBox()
      await page.screenshot({ path: `${OUT}/${name}.png`, clip: { x: 0, y: 0, width, height: Math.ceil(box.y + box.height + 16) } })
      console.log('wrote', `${OUT}/${name}.png`)
    }
    if (click && got) {
      await notice.getByTestId('kiro-sign-in-notice-open').click()
      await page.waitForURL(/\/settings\/agent/, { timeout: 10_000 }).catch(() => {})
      row.landedOn = new URL(page.url()).pathname + new URL(page.url()).search
      await page.waitForTimeout(1500)
      row.noticeOnCardPage = await notice.count()
      row.ok = row.ok && row.landedOn.startsWith('/settings/agent') && row.landedOn.includes('kiro-sign-in') && row.noticeOnCardPage === 0
      if (!VERIFY_ONLY) {
        await page.screenshot({ path: `${OUT}/${name}-landed.png` })
        console.log('wrote', `${OUT}/${name}-landed.png`)
      }
    }
    results.push(row)
    await context.close()
  }

  // The refused renewal is an ErrorNotice: the hand-off and dismiss on its
  // first row, the sign-in button on its own row below.
  const expired = { title: 'Kiro sign-in expired', buttons: 2, open: true }
  const refused = { testId: 'kiro-sign-in-refused', title: 'Kiro refused to renew your sign-in', buttons: 3, open: true }
  const failed = { testId: 'kiro-sign-in-status-error', title: 'We could not check sign-in status', buttons: 2, open: false }
  const expiredEs = { title: 'Inicio de sesión en Kiro caducado', buttons: 2, open: true, dismissName: 'Descartar' }
  if (BEFORE) {
    await frame('00-before-expired-dark', { status: EXPIRED, want: null })
  } else {
    await frame('01-expired-dark', { status: EXPIRED, want: expired })
    await frame('02-refused-light', { status: REFUSED, theme: 'light', route: '/schedule', want: refused })
    await frame('03-healthy-dark', { status: HEALTHY, want: null })
    await frame('04-click-opens-sign-in', { status: EXPIRED, want: expired, click: true })
    await frame('05-phone-expired-dark', { status: EXPIRED, width: 390, want: expired })
    await frame('06-phone-refused-light', { status: REFUSED, theme: 'light', width: 390, route: '/schedule', want: refused })
    await frame('07-status-read-failed-dark', { status: FAILED, want: failed })
    // The narrowest supported width in a long locale: the button label wraps
    // and the dismiss stays inside the window.
    await frame('08-320-spanish-expired-dark', { status: EXPIRED, width: 320, route: '/schedule', lang: 'es', want: expiredEs })
  }

  await browser.close()
  srv.close()
  console.log('--- assertions ---')
  for (const r of results) console.log(JSON.stringify(r))
  if (!results.every(r => r.ok)) {
    console.error('FAIL: the dashboard did not show the expected Kiro sign-in notice state')
    process.exit(1)
  }
  console.log('OK')
}

main().catch(err => { console.error(err); process.exit(1) })
