import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useLocation, useNavigate } from 'react-router-dom'
import { AlertTriangle, ArrowRight, X } from 'lucide-react'

import { api } from '../api/client'
import type { AcpBackendConfig } from '../api/acpBackend'
import { i18nT } from '../i18n/t'
import ErrorNotice from './ErrorNotice'
import { useGuardedLeave } from './NavigationLeaveGuard'
import { Btn } from './ui'
import { settingsPath } from './settingsPath'
import { AGENT_HARNESS_SETTINGS_TAB, KIRO_SIGN_IN_BACKEND, KIRO_SIGN_IN_PATH } from '../pages/developer/kiroSignInLink'
import { signInLapsed } from '../pages/developer/kiroSignInState'

// Same cache key the sign-in flow and card read status under
// (KasLoginGate.QUERY_KEY): a sign-in, sign-out or switch landing there
// re-reads this one entry, so the notice clears the moment a new sign-in lands
// instead of waiting for its own next poll.
const STATUS_QUERY_KEY = ['kas-login'] as const

/**
 * How often the notice re-reads the stored sign-in while KAS is the backend.
 * The gateway's renewal tick records a refused renewal in the store, and the
 * status read is a plain store read (no refresh, no network), so a minute
 * bounds how late the notice reports one at the cost of one small read per
 * minute.
 */
export const KIRO_SIGN_IN_NOTICE_POLL_MS = 60_000

/**
 * App-level notice for a stored Kiro sign-in that no longer works, mounted
 * above every page beside the crash notice. The sign-in card on Settings >
 * Agent Harness says the same thing, but on a page nobody keeps open; without
 * this a lapsed or issuer-refused sign-in stays invisible until a chat turn
 * fails.
 *
 * A banner rather than a top-bar control: the top bar's action groups are at
 * the two-control cap (or already past it from before the cap), so a new
 * control there would grow a capped row. A banner is its own region, and it
 * can say what happened and where the button goes at every width instead of
 * folding to an icon.
 *
 * What it shows, all hidden on Settings > Agent Harness itself (the card shows
 * every one of these states next to its own controls):
 * - The config read or the status read FAILED: an `ErrorNotice` with the
 *   agent hand-off. The config read decides whether KAS runs the agents at
 *   all, so its failure leaves the sign-in unchecked just as the status
 *   read's does.
 * - Kiro REFUSED to renew the stored sign-in (`refresh_rejected`, recorded by
 *   the renewal tick when the issuer answers no): that is a failed request, so
 *   it is an `ErrorNotice` too, with the way to fix it in its footer.
 * - The stored sign-in EXPIRED with nothing to renew it with: a state, not a
 *   failure, so a `role="status"` notice with the same button.
 * Nothing at all while KAS is not the selected backend (the one backend that
 * runs agents as this identity), with no stored sign-in, or while it works. A
 * gateway on another backend never reads the status.
 *
 * The hand-offs: the notice holds no input, and both the hand-off and the
 * sign-in button ask the page's leave guard before navigating, so a draft on
 * the page below is not lost. The dashboard's own agent is reachable whatever
 * this read says (the sign-in card makes the same call for a failed status
 * read, in KasLoginGate).
 *
 * The sign-in button opens the card rather than starting a sign-in: the card
 * is where the provider is chosen, and a one-click browser sign-in from a
 * banner is a misclick hazard. Dismissing hides a lapsed notice for its cause
 * until the page reloads (a different cause shows again), and a failed read
 * until a read succeeds again (not on every failed poll).
 */
export default function KiroSignInNotice() {
  const navigate = useNavigate()
  const leave = useGuardedLeave()
  const { pathname } = useLocation()
  const [dismissedCause, setDismissedCause] = useState<string | null>(null)
  // `<which read>:<its dataUpdatedAt>` when its failure was dismissed.
  const [dismissedFailure, setDismissedFailure] = useState<string | null>(null)
  const cfgQuery = useQuery<AcpBackendConfig>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const kas = cfgQuery.data?.agent?.acp_backend === KIRO_SIGN_IN_BACKEND
  const statusQuery = useQuery({
    queryKey: STATUS_QUERY_KEY,
    queryFn: api.kasLoginStatus,
    enabled: kas,
    refetchInterval: KIRO_SIGN_IN_NOTICE_POLL_MS,
  })

  if (pathname === settingsPath({ tab: AGENT_HARNESS_SETTINGS_TAB })) return null

  const failedRead = cfgQuery.isError
    ? { key: `config:${cfgQuery.dataUpdatedAt}`, error: cfgQuery.error }
    : kas && statusQuery.isError
      ? { key: `status:${statusQuery.dataUpdatedAt}`, error: statusQuery.error }
      : null
  if (failedRead) {
    if (dismissedFailure === failedRead.key) return null
    const title = i18nT('components.kasLogin.sign_in_status_unavailable')
    return (
      <ErrorNotice
        className="mx-3 md:mx-6 mt-4 mb-2"
        title={title}
        // The server's text is the journal key the hand-off recovers the
        // endpoint and status from, so it stays, as the secondary line.
        message={failedRead.error.message || title}
        messagePlacement="below"
        actionPlacement="below"
        askAgent
        onDismiss={() => setDismissedFailure(failedRead.key)}
        testId="kiro-sign-in-status-error"
      />
    )
  }

  const status = statusQuery.data
  if (!kas || !status?.authenticated || !signInLapsed(status)) return null
  const cause = status.refresh_rejected ? 'refresh_rejected' : 'expired'
  if (dismissedCause === cause) return null

  const openSignIn = (
    <Btn
      primary
      data-testid="kiro-sign-in-notice-open"
      className="min-w-0 max-w-full text-left"
      onClick={() => leave(() => navigate(KIRO_SIGN_IN_PATH), KIRO_SIGN_IN_PATH)}
    >
      <span className="min-w-0">{i18nT('components.kiroSignInNotice.open_sign_in')}</span>
      <ArrowRight size={14} className="shrink-0" aria-hidden="true" />
    </Btn>
  )

  if (cause === 'refresh_rejected') {
    return (
      <ErrorNotice
        className="mx-3 md:mx-6 mt-4 mb-2"
        title={i18nT('components.kiroSignInNotice.refused_title')}
        message={i18nT('components.kiroSignInNotice.refused_detail')}
        messagePlacement="below"
        actionPlacement="below"
        footer={<div className="mt-1.5">{openSignIn}</div>}
        askAgent
        onDismiss={() => setDismissedCause(cause)}
        testId="kiro-sign-in-refused"
      />
    )
  }

  const dismissName = i18nT('components.errorNotice.dismiss')
  // Two controls: the way to fix it, and dismiss. The group may take a line of
  // its own below the text, and its button label wraps rather than pushing the
  // dismiss out of a 320px column.
  return (
    <div
      role="status"
      data-testid="kiro-sign-in-notice"
      className="mx-3 md:mx-6 mt-4 mb-2 bg-warn/10 border border-warn/30 rounded-lg p-3 flex flex-wrap items-center gap-x-3 gap-y-2 animate-rise"
    >
      <AlertTriangle size={16} className="text-warn shrink-0" aria-hidden="true" />
      <div className="flex-1 basis-56 min-w-0 text-[13px]">
        <div className="font-medium text-text">{i18nT('components.kiroSignInNotice.sign_in_expired')}</div>
        <div className="text-muted mt-0.5">{i18nT('components.kiroSignInNotice.expired_detail')}</div>
      </div>
      <div className="flex items-center gap-2 min-w-0 max-w-full">
        {openSignIn}
        <button
          type="button"
          onClick={() => setDismissedCause(cause)}
          aria-label={dismissName}
          title={dismissName}
          className="shrink-0 text-muted hover:text-text p-1 rounded cursor-pointer bg-transparent border-none"
        >
          <X size={14} aria-hidden="true" />
        </button>
      </div>
    </div>
  )
}
