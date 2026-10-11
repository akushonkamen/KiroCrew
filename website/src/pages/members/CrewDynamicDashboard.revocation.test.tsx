// @vitest-environment jsdom
/**
 * WITHDRAWING SCRIPT HAS TO TAKE EFFECT ON THE FRAME THAT IS MOUNTED.
 *
 * The server decides whether an owner-written page may run script and says so per read
 * (`scripts`). Withdrawing it is a governance act, so it cannot wait on anything the
 * PAGE does -- and the readiness beacon this component promotes a new layout on is
 * exactly that: a message the page sends, with script.
 *
 * Two ways that trapped a revocation, each leaving `allow-scripts` on the document a
 * reader is looking at:
 *
 *  - a NEW version answering `scripts: false` became a probating candidate, mounted
 *    with an empty sandbox to prove itself, so it could never send the beacon; the
 *    timeout only drew the "not the newest" band, and the scripted page it was meant to
 *    replace stayed mounted;
 *  - the SAME version answering `scripts: false` was dropped by the in-place swap, which
 *    compares the html and keeps the held document when it has not changed -- so the
 *    one thing that DID change never reached the frame.
 *
 * So the grant the frame is mounted with follows the server's latest answer, and a page
 * that may not run script is promoted without being asked to prove it came up.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { api, type DashboardManifest } from '../../api/client'
import { renderWithProviders } from '../../test/helpers'
import CrewDynamicDashboard from './CrewDynamicDashboard'

vi.mock('../../hooks/useSandboxDoc', () => ({
  useSandboxDoc: (srcdoc: string | null) => ({
    url: srcdoc ? `/sandbox-doc/${srcdoc.length}` : null,
    pending: false,
    failed: false,
    stalled: false,
    retry: () => {},
  }),
}))

const MANIFEST = { id: 'atlas-tree', version: 1, title: 'Tree', fields: [] } as unknown as DashboardManifest

/** One read. `scripts` left out means allowed, which is what an older gateway sends. */
function page(over: Record<string, unknown> = {}) {
  return {
    instance_version: 1,
    template: { id: 'atlas-tree', version: 1 },
    html: '<!doctype html><p>stored</p>',
    rendered_html: '<!doctype html><p>v1</p>',
    manifest: MANIFEST,
    state: 'live' as const,
    ...over,
  }
}

const TARGET = { kind: 'member', slug: 'atlas', member: 'Atlas' } as const
const DASHBOARD_KEY = ['member-dashboard', 'atlas']

const mount = () =>
  renderWithProviders(<CrewDynamicDashboard target={TARGET} displayName="Atlas" />)

describe('withdrawing script from a page already on screen', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('strips allow-scripts when a NEW version answers scripts: false', async () => {
    // The trapped case. Promotion on the beacon alone would leave the scripted
    // document mounted for as long as the tab stays open, because the page that was
    // meant to replace it has no script with which to beacon.
    vi.spyOn(api, 'memberDashboard')
      .mockResolvedValueOnce(page())
      .mockResolvedValue(
        page({ instance_version: 2, rendered_html: '<!doctype html><p>v2</p>', scripts: false }),
      )
    const { queryClient } = mount()
    const frame = await screen.findByTestId('crew-dashboard-iframe')
    expect(frame).toHaveAttribute('sandbox', 'allow-scripts')

    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })

    await waitFor(() => {
      const grants = screen
        .getAllByTestId('crew-dashboard-iframe')
        .map((f) => f.getAttribute('sandbox'))
      expect(grants).not.toContain('allow-scripts')
    })
  })

  it('strips allow-scripts when the SAME version answers scripts: false', async () => {
    // No new layout at all -- only the flag moves. The in-place swap compares the html,
    // so nothing about the DOCUMENT changed and the revocation has to travel on its own.
    vi.spyOn(api, 'memberDashboard')
      .mockResolvedValueOnce(page())
      .mockResolvedValue(page({ scripts: false }))
    const { queryClient } = mount()
    const frame = await screen.findByTestId('crew-dashboard-iframe')
    expect(frame).toHaveAttribute('sandbox', 'allow-scripts')

    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })

    await waitFor(() => {
      const grants = screen
        .getAllByTestId('crew-dashboard-iframe')
        .map((f) => f.getAttribute('sandbox'))
      expect(grants).not.toContain('allow-scripts')
    })
  })

  it('shows the newer static page rather than keeping the old scripted one', async () => {
    // The other half of the same fix: a static candidate is PROMOTED, not merely
    // de-scripted. Withholding it behind a beacon it cannot send would freeze the tab
    // on the previous layout for good.
    vi.spyOn(api, 'memberDashboard')
      .mockResolvedValueOnce(page())
      .mockResolvedValue(
        page({ instance_version: 2, rendered_html: '<!doctype html><p>v2</p>', scripts: false }),
      )
    const { queryClient } = mount()
    await screen.findByTestId('crew-dashboard-iframe')

    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })

    // The kept-page band is what a never-promoted candidate leaves behind, so its
    // ABSENCE is how the promotion shows from outside.
    await waitFor(() => {
      const grants = screen
        .getAllByTestId('crew-dashboard-iframe')
        .map((f) => f.getAttribute('sandbox'))
      expect(grants).not.toContain('allow-scripts')
    })
    expect(screen.queryByTestId('crew-dashboard-kept-band')).not.toBeInTheDocument()
  })

  it('keeps allow-scripts while the server still permits it', async () => {
    // The revocation path must not become "static whenever anything changes".
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(page())
    mount()
    const frame = await screen.findByTestId('crew-dashboard-iframe')
    expect(frame).toHaveAttribute('sandbox', 'allow-scripts')
  })
})
