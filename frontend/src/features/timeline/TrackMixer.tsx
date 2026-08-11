/**
 * The mixer controls on an audio track's header: level, stereo placement, mute and solo.
 *
 * Solo is not just "mute the others": it is a temporary override, so the app has to show *why* a track
 * is silent — a track muted by someone else's solo looks identical to one you muted yourself otherwise,
 * and chasing that is a familiar waste of an afternoon.
 */

import { api, ApiError } from '@/api/client'
import type { Project, Track } from '@/api/types'
import { cx, useToast } from '@/components/ui'

interface Props {
  project: Project
  track: Track
  /** True when some *other* track is soloed, which is what actually silences this one. */
  silencedBySolo: boolean
  onChanged: () => void
}

export function TrackMixer({ project, track, silencedBySolo, onChanged }: Props) {
  const toast = useToast()

  const patch = async (body: Partial<Track>) => {
    try {
      await api.timeline.updateTrack(project.id, track.id, body)
      onChanged()
    } catch (error) {
      toast.push('bad', (error as ApiError).message)
    }
  }

  const silent = track.muted || silencedBySolo

  return (
    <div className="flex min-w-0 shrink-0 items-center gap-1">
      <button
        title={track.solo ? 'Stop soloing this track' : 'Solo — silence every other track'}
        onClick={() => void patch({ solo: !track.solo })}
        className={cx(
          'rounded px-1 text-[10px] font-semibold',
          track.solo
            ? 'bg-[var(--color-warn)]/25 text-[var(--color-warn)]'
            : 'text-[var(--color-ink-dim)] hover:text-[var(--color-ink)]',
        )}
      >
        S
      </button>
      <button
        title={
          track.muted
            ? 'Unmute'
            : silencedBySolo
              ? 'Silent because another track is soloed'
              : 'Mute'
        }
        onClick={() => void patch({ muted: !track.muted })}
        className={cx(
          'rounded px-1 text-[10px] font-semibold',
          track.muted
            ? 'bg-[var(--color-bad)]/25 text-[var(--color-bad)]'
            : silent
              ? 'text-[var(--color-warn)]'
              : 'text-[var(--color-ink-dim)] hover:text-[var(--color-ink)]',
        )}
      >
        M
      </button>

      <label className="flex items-center gap-1" title={`Level — ${Math.round(track.volume * 100)}%`}>
        <span className="text-[9px] text-[var(--color-ink-dim)]">lvl</span>
        <input
          type="range"
          min={0}
          max={2}
          step={0.01}
          value={track.volume}
          // Committed on release: dragging a slider would otherwise be one request per pixel.
          onChange={(event) => void patch({ volume: Number(event.target.value) })}
          className="h-1 w-12 accent-[var(--color-accent)]"
        />
      </label>

      <label
        className="flex items-center gap-1"
        title={`Pan — ${track.pan === 0 ? 'centre' : `${Math.abs(Math.round(track.pan * 100))}% ${track.pan < 0 ? 'left' : 'right'}`}`}
      >
        <span className="text-[9px] text-[var(--color-ink-dim)]">pan</span>
        <input
          type="range"
          min={-1}
          max={1}
          step={0.01}
          value={track.pan}
          onChange={(event) => void patch({ pan: Number(event.target.value) })}
          onDoubleClick={() => void patch({ pan: 0 })}
          className="h-1 w-12 accent-[var(--color-accent)]"
        />
      </label>
    </div>
  )
}
