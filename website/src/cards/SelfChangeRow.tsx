/**
 * One line in a crewmate's chat for a change it made to itself: its name, its
 * description or its face (`update_self`, the `kirocrew-mate` server).
 *
 * The gateway writes the record the moment the crewmate asks, so there is no
 * card to press -- the row tells the user what just changed and offers one
 * **Undo**. Drawn centred and quiet, like a notice, with the old face beside the
 * new one when the face changed. The row's `before`/`after` are for drawing
 * only: Undo posts to the gateway, which restores from its OWN record of the
 * change (`dashboard/member_self_changes.py`) and then marks this row
 * `undone` over the same `chat_message_update` frame every card row uses.
 */
import { useState } from 'react'
import { ArrowRight, RotateCcw } from 'lucide-react'
import { apiTransport } from '../api/apiTransport'
import CrewAvatar from '../components/CrewAvatar'
import ErrorNotice from '../components/ErrorNotice'
import { i18nT } from '../i18n/t'

export type SelfField = 'display_name' | 'description' | 'avatar'

export interface SelfChangeRef {
  surface: 'self'
  id: string
  slot: string
  /** The crewmate's agent key (the avatar seed), never shown. */
  member: string
  /** The display name the crewmate goes by after the change: what the row calls it. */
  name: string
  /** The crewmate's slug, which the undo route is addressed to. */
  slug: string
  /** The fields that changed, in display order. */
  fields: SelfField[]
  before: Partial<Record<SelfField, unknown>>
  after: Partial<Record<SelfField, unknown>>
  status: 'applied' | 'undone'
}

const FACE_PX = 22

const str = (v: unknown, max = 300) => (typeof v === 'string' ? v.slice(0, max) : '')

function titleFor(ref: SelfChangeRef): string {
  // The name the row speaks of is the one the user knows: after a rename that
  // is the NEW name (the crewmate answers to it from now on), unless the rename
  // was undone, in which case the old name is back.
  const shown = ref.status === 'undone' ? ref.before.display_name : ref.after.display_name
  const name = str(shown) || ref.name || ref.member
  const key = ref.fields.length !== 1 ? 'changed_profile'
    : ref.fields[0] === 'avatar' ? 'changed_avatar'
    : ref.fields[0] === 'description' ? 'changed_description'
    : 'changed_display_name'
  return i18nT(`components.selfChange.${key}`, { name })
}

export default function SelfChangeRow({ change }: { change: SelfChangeRef }) {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const undone = change.status === 'undone'
  const faceChanged = change.fields.includes('avatar')
  const undo = async () => {
    setPending(true)
    setError(null)
    try {
      const { post, j } = apiTransport
      const r = await post(
        `/api/members/${encodeURIComponent(change.slug)}/self-changes/${encodeURIComponent(change.id)}/undo`,
      )
      const body = (await j(r)) as { ok?: boolean; error?: string; code?: string } | null
      if (!r.ok || !body?.ok) {
        // The gateway holds an undo record only while it runs and only for the
        // newest changes: past that, the crew editor is where the value is set.
        setError(body?.code === 'undo_unavailable'
          ? i18nT('components.selfChange.undo_unavailable')
          : i18nT('components.selfChange.undo_failed', { message: body?.error || body?.code || r.statusText }))
      }
      // The row itself flips to `undone` through the gateway's row update; no
      // local state to keep in step.
    } catch (e) {
      setError(i18nT('components.selfChange.undo_failed', { message: e instanceof Error ? e.message : String(e) }))
    } finally {
      setPending(false)
    }
  }
  return (
    <div className="flex flex-col items-center gap-1.5" data-testid="self-change-row" data-self-status={change.status}>
      <div
        className={`inline-flex max-w-full items-center gap-2.5 rounded-full border border-border bg-bg-elevated px-3 py-1.5 text-[12px] text-muted ${undone ? 'opacity-70' : ''}`}
      >
        {faceChanged && (
          <span className="inline-flex items-center gap-1.5" aria-hidden="true">
            <span className={undone ? '' : 'opacity-60'}>
              <CrewAvatar seed={change.member} avatar={change.before.avatar} size={FACE_PX} />
            </span>
            <ArrowRight size={12} className="lucide-inline text-muted-strong" />
            <span className={undone ? 'opacity-60' : ''}>
              <CrewAvatar seed={change.member} avatar={change.after.avatar} size={FACE_PX} />
            </span>
          </span>
        )}
        <span className="min-w-0 truncate">
          {titleFor(change)}
          {undone && <span className="ml-1">· {i18nT('components.selfChange.undone')}</span>}
        </span>
        {!undone && (
          <button
            type="button"
            onClick={() => void undo()}
            disabled={pending}
            aria-busy={pending || undefined}
            className="inline-flex shrink-0 items-center gap-1 rounded-full px-1.5 py-0.5 text-accent hover:bg-accent-subtle disabled:opacity-50"
            data-testid="self-change-undo"
          >
            <RotateCcw size={12} className="lucide-inline" aria-hidden="true" />
            {i18nT('components.selfChange.undo')}
          </button>
        )}
      </div>
      {/* No hand-off: the row sits in the chat beside the composer, whose unsent draft the hand-off navigation would discard. */}
      {error && <ErrorNotice variant="inline" testId="self-change-undo-error" message={error} />}
    </div>
  )
}
