import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { act, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react'

import { renderWithProviders } from './helpers'
import { namedCeiling } from './namedCeiling'
import KiroSignInNotice, { KIRO_SIGN_IN_NOTICE_POLL_MS } from '../components/KiroSignInNotice'
import { NavigationLeaveGuardProvider, useRegisterNavigationLeaveGuard } from '../components/NavigationLeaveGuard'
import { KIRO_SIGN_IN_PATH } from '../pages/developer/kiroSignInLink'
import { api, type KasLoginStatus } from '../api/client'

const navigate = vi.fn()
vi.mock('react-router-dom', async importOriginal => ({
  ...(await importOriginal<typeof import('react-router-dom')>()),
  useNavigate: () => navigate,
}))

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      kasLoginStatus: vi.fn(),
      kirocrewConfig: vi.fn(),
    },
  }
})

const kasLoginStatus = vi.mocked(api.kasLoginStatus)
const kirocrewConfig = vi.mocked(api.kirocrewConfig)

// Token-free status bodies, exactly the fields `/api/kas-login` returns.
const HEALTHY: KasLoginStatus = {
  authenticated: true,
  provider: 'Google',
  identity: 'social',
  transport: 'device',
  expires_at: '2099-01-01T00:00:00Z',
  expired: false,
  has_refresh_token: true,
  refresh_rejected: false,
  usable: true,
}
/** Access token past the margin and nothing to renew it with. */
const EXPIRED: KasLoginStatus = { ...HEALTHY, expires_at: '2000-01-01T00:00:00Z', expired: true, has_refresh_token: false, usable: false }
/** The issuer refused the last renewal; `usable` stays true (see auth/service.py status). */
const REFUSED: KasLoginStatus = { ...HEALTHY, refresh_rejected: true }
const SIGNED_OUT: KasLoginStatus = { ...HEALTHY, authenticated: false, provider: '', identity: '', expires_at: null, has_refresh_token: false, usable: false }

const notice = () => screen.queryByTestId('kiro-sign-in-notice')
const refused = () => screen.queryByTestId('kiro-sign-in-refused')
const failure = () => screen.queryByTestId('kiro-sign-in-status-error')

// Every wait below sits behind a chained query: the config read lands, which
// enables the status read, whose answer decides the render.
const chain = () => namedCeiling('KIRO_SIGN_IN_NOTICE_CHAIN', 5000)
const findNotice = () => screen.findByTestId('kiro-sign-in-notice', {}, chain())
const findRefused = () => screen.findByTestId('kiro-sign-in-refused', {}, chain())
const findFailure = () => screen.findByTestId('kiro-sign-in-status-error', {}, chain())

function backend(id: string) {
  kirocrewConfig.mockResolvedValue({ agent: { acp_backend: id } } as Awaited<ReturnType<typeof api.kirocrewConfig>>)
}

/** A page below the notice that answers the leave guard (holds a draft, or not). */
function NavigationVeto({ guard }: { guard: () => boolean }) {
  useRegisterNavigationLeaveGuard(guard)
  return null
}

/** Let a landed answer's render commit before an absence check. */
const settle = () => act(async () => { await Promise.resolve() })

beforeEach(() => {
  navigate.mockReset()
  kasLoginStatus.mockReset()
  kirocrewConfig.mockReset()
})

// Unmount first so no query timer fires on the fake clock, then restore real
// timers even when a fake-clock test fails part-way.
afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('KiroSignInNotice', () => {
  it('shows an expired sign-in with nothing to renew it as a status notice', async () => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(EXPIRED)
    renderWithProviders(<KiroSignInNotice />)
    const el = await findNotice()
    expect(el).toHaveAttribute('role', 'status')
    expect(el).toHaveTextContent(/^Kiro sign-in expiredAgents cannot run until you sign in again/)
    expect(refused()).not.toBeInTheDocument()
  })

  it('shows a refused renewal as an error under its own title, with the hand-off and the way to fix it', async () => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(REFUSED)
    renderWithProviders(<KiroSignInNotice />)
    const el = await findRefused()
    expect(el).toHaveAttribute('role', 'alert')
    // Not "expired": the sign-in still works until its access token runs out.
    expect(el).toHaveTextContent(/^Kiro refused to renew your sign-in/)
    expect(el).not.toHaveTextContent('Kiro sign-in expired')
    expect(el).toHaveTextContent('Agents will stop when this sign-in expires')
    expect(within(el).getByRole('button', { name: /Ask the agent/i })).toBeInTheDocument()
    fireEvent.click(within(el).getByRole('button', { name: 'Sign in again in Settings' }))
    expect(navigate).toHaveBeenCalledWith(KIRO_SIGN_IN_PATH)
    expect(notice()).not.toBeInTheDocument()
  })

  it('names where its button goes, and opens the sign-in card instead of starting a sign-in', async () => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(EXPIRED)
    renderWithProviders(<KiroSignInNotice />)
    const el = await findNotice()
    fireEvent.click(within(el).getByRole('button', { name: 'Sign in again in Settings' }))
    expect(navigate).toHaveBeenCalledWith(KIRO_SIGN_IN_PATH)
  })

  it('asks the page it sits above before leaving it, so an unsaved draft there is not discarded', async () => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(EXPIRED)
    const guard = vi.fn().mockReturnValue(false)
    renderWithProviders(
      <NavigationLeaveGuardProvider>
        <NavigationVeto guard={guard} />
        <KiroSignInNotice />
      </NavigationLeaveGuardProvider>,
    )
    const open = within(await findNotice()).getByRole('button', { name: 'Sign in again in Settings' })
    fireEvent.click(open)
    expect(guard).toHaveBeenCalledTimes(1)
    expect(navigate).not.toHaveBeenCalled()

    guard.mockReturnValue(true)
    fireEvent.click(open)
    expect(navigate).toHaveBeenCalledWith(KIRO_SIGN_IN_PATH)
  })

  it('renders a failed status read as an error with the agent hand-off, not as nothing', async () => {
    backend('kas')
    kasLoginStatus.mockRejectedValue(new Error('token_store_failed: the sign-in store could not be read'))
    renderWithProviders(<KiroSignInNotice />)
    const el = await findFailure()
    expect(el).toHaveAttribute('role', 'alert')
    expect(el).toHaveTextContent(/^We could not check sign-in status/)
    expect(el).toHaveTextContent('token_store_failed')
    expect(within(el).getByRole('button', { name: /Ask the agent/i })).toBeInTheDocument()
    expect(notice()).not.toBeInTheDocument()

    fireEvent.click(within(el).getByRole('button', { name: 'Dismiss' }))
    expect(failure()).not.toBeInTheDocument()
  })

  it('renders a failed backend-setting read the same way, since the sign-in then goes unchecked', async () => {
    kirocrewConfig.mockRejectedValue(new Error('config read failed'))
    renderWithProviders(<KiroSignInNotice />)
    const el = await findFailure()
    expect(el).toHaveTextContent(/^We could not check sign-in status/)
    expect(el).toHaveTextContent('config read failed')
    expect(within(el).getByRole('button', { name: /Ask the agent/i })).toBeInTheDocument()
    expect(kasLoginStatus).not.toHaveBeenCalled()
  })

  it('keeps a dismissed failure hidden across failed polls, and shows a new one after a good read', async () => {
    backend('kas')
    kasLoginStatus.mockRejectedValue(new Error('token_store_failed'))
    const { queryClient } = renderWithProviders(<KiroSignInNotice />)
    fireEvent.click(within(await findFailure()).getByRole('button', { name: 'Dismiss' }))

    // The same failure on the next read stays dismissed.
    await act(async () => { await queryClient.refetchQueries({ queryKey: ['kas-login'] }) })
    expect(kasLoginStatus).toHaveBeenCalledTimes(2)
    expect(failure()).not.toBeInTheDocument()

    // A good read, then a failure again: that is a new failure.
    kasLoginStatus.mockResolvedValue(HEALTHY)
    await act(async () => { await queryClient.refetchQueries({ queryKey: ['kas-login'] }) })
    kasLoginStatus.mockRejectedValue(new Error('token_store_failed'))
    await act(async () => { await queryClient.refetchQueries({ queryKey: ['kas-login'] }) })
    expect(await findFailure()).toBeInTheDocument()
  })

  it('holds two controls on each row of the lapsed notices', async () => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(EXPIRED)
    renderWithProviders(<KiroSignInNotice />)
    expect(within(await findNotice()).getAllByRole('button')).toHaveLength(2)
  })

  it('stays dismissed for the same cause, and shows again when the cause changes', async () => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(REFUSED)
    const { queryClient } = renderWithProviders(<KiroSignInNotice />)
    fireEvent.click(within(await findRefused()).getByRole('button', { name: 'Dismiss' }))
    expect(refused()).not.toBeInTheDocument()

    // The same refusal read again stays dismissed.
    await act(async () => { await queryClient.invalidateQueries({ queryKey: ['kas-login'] }) })
    expect(kasLoginStatus).toHaveBeenCalledTimes(2)
    expect(refused()).not.toBeInTheDocument()

    // A different cause is new information: the token has now expired, nothing renews it.
    kasLoginStatus.mockResolvedValue(EXPIRED)
    await act(async () => { await queryClient.invalidateQueries({ queryKey: ['kas-login'] }) })
    expect(await findNotice()).toBeInTheDocument()
  })

  it('is absent on Settings > Agent Harness, where the sign-in card itself shows the state', async () => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(REFUSED)
    renderWithProviders(<KiroSignInNotice />, { route: '/settings/agent?highlight=key%3Akiro-sign-in' })
    await waitFor(() => expect(kasLoginStatus).toHaveBeenCalled(), chain())
    await settle()
    expect(refused()).not.toBeInTheDocument()
    expect(notice()).not.toBeInTheDocument()
  })

  it('is absent while the sign-in is healthy', async () => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(HEALTHY)
    renderWithProviders(<KiroSignInNotice />)
    await waitFor(() => expect(kasLoginStatus).toHaveBeenCalled(), chain())
    await settle()
    expect(notice()).not.toBeInTheDocument()
    expect(refused()).not.toBeInTheDocument()
  })

  it('is absent with no stored sign-in: not signed in is not lapsed', async () => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(SIGNED_OUT)
    renderWithProviders(<KiroSignInNotice />)
    await waitFor(() => expect(kasLoginStatus).toHaveBeenCalled(), chain())
    await settle()
    expect(notice()).not.toBeInTheDocument()
  })

  it.each([
    ['kiro-cli', ''],
    ['Claude', 'claude'],
  ])('never reads the sign-in on the %s backend, where it concerns no agent', async (_name, id) => {
    backend(id)
    kasLoginStatus.mockResolvedValue(EXPIRED)
    renderWithProviders(<KiroSignInNotice />)
    await waitFor(() => expect(kirocrewConfig).toHaveBeenCalled(), chain())
    // Let the config land and any status read that would follow it run.
    await settle()
    expect(kasLoginStatus).not.toHaveBeenCalled()
    expect(notice()).not.toBeInTheDocument()
    expect(failure()).not.toBeInTheDocument()
  })

  it('clears as soon as a new sign-in lands on the shared status entry', async () => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(EXPIRED)
    const { queryClient } = renderWithProviders(<KiroSignInNotice />)
    await findNotice()
    // What the sign-in flow does when its credential lands (KasLoginGate
    // invalidates `['kas-login']`), so the notice does not wait for its poll.
    kasLoginStatus.mockResolvedValue(HEALTHY)
    await act(async () => { await queryClient.invalidateQueries({ queryKey: ['kas-login'] }) })
    await waitFor(() => expect(notice()).not.toBeInTheDocument(), chain())
  })

  it('re-reads the status on its own interval, so a refusal or a renewal shows without a reload', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    backend('kas')
    kasLoginStatus.mockResolvedValue(HEALTHY)
    renderWithProviders(<KiroSignInNotice />)
    await waitFor(() => expect(kasLoginStatus).toHaveBeenCalledTimes(1), chain())
    expect(refused()).not.toBeInTheDocument()

    // The gateway's renewal tick records a refusal between two reads.
    kasLoginStatus.mockResolvedValue(REFUSED)
    await vi.advanceTimersByTimeAsync(KIRO_SIGN_IN_NOTICE_POLL_MS)
    await waitFor(() => expect(refused()).toBeInTheDocument(), chain())

    // A successful renewal (or a new sign-in) clears it on the next read.
    kasLoginStatus.mockResolvedValue(HEALTHY)
    await vi.advanceTimersByTimeAsync(KIRO_SIGN_IN_NOTICE_POLL_MS)
    await waitFor(() => expect(refused()).not.toBeInTheDocument(), chain())
  })
})
