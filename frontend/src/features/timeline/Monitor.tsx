/**
 * The program monitor: what the timeline looks like at the playhead, and playback.
 *
 * It previews rather than renders. Asking the server to composite a frame per tick would be far too slow
 * to play at all, so this shows the topmost visual clip under the playhead directly — a `<video>` seeked
 * into the source, or the still for an image clip. Overlays and transforms are not composited here; the
 * render is the authority on the final picture, and this is for finding your way around the cut.
 *
 * Playback drives the playhead from `requestAnimationFrame` against a wall clock rather than counting
 * frames, so a slow tab drops frames instead of drifting out of sync with the audio the user can hear.
 */

import { useCallback, useEffect, useMemo, useRef } from 'react'

import { api } from '@/api/client'
import type { Clip, Project, ResolvedTimeline } from '@/api/types'
import { formatTimecode } from '@/lib/format'
import { Button, cx } from '@/components/ui'

interface Props {
  project: Project
  resolved: ResolvedTimeline | undefined
  playhead: number
  onScrub: (time: number) => void
  playing: boolean
  onPlayingChange: (playing: boolean) => void
  className?: string
}

/** What to show right now: a clip, where in its source, and the media behind it. */
interface Frame {
  clip: Clip
  /** Seconds into the source file. */
  sourceTime: number
  url: string | null
  kind: string | null
  text: string
}

export function Monitor({
  project, resolved, playhead, onScrub, playing, onPlayingChange, className,
}: Props) {
  const timeline = project.timeline
  const video = useRef<HTMLVideoElement>(null)
  const raf = useRef<number>()

  const media = useMemo(() => {
    const map = new Map<string, { url: string | null; kind: string | null }>()
    for (const entry of resolved?.clips ?? []) {
      const artifact = entry.artifacts[0]
      map.set(entry.clip_id, {
        url: artifact ? api.media.url(project.id, artifact.path) : null,
        kind: entry.kind,
      })
    }
    return map
  }, [resolved, project.id])

  /**
   * The clip the monitor should be showing.
   *
   * Topmost wins, matching how the compositor stacks tracks: later tracks paint over earlier ones, so the
   * last visual track with something under the playhead is what you would actually see.
   */
  const frame = useMemo<Frame | null>(() => {
    let found: Frame | null = null
    for (const track of timeline.tracks) {
      if (track.kind === 'audio' || track.muted) continue
      for (const clip of track.clips) {
        if (!clip.enabled) continue
        if (playhead < clip.start || playhead >= clip.start + clip.duration) continue
        const entry = media.get(clip.id)
        found = {
          clip,
          sourceTime: clip.in_point + (playhead - clip.start),
          url: entry?.url ?? null,
          kind: entry?.kind ?? null,
          text: clip.text,
        }
      }
    }
    return found
  }, [timeline.tracks, playhead, media])

  const isVideo = frame?.kind === 'video'

  // Keep a video element in step with the playhead while scrubbing. During playback the element runs on
  // its own clock and we follow it instead, which avoids fighting the decoder with a seek every frame.
  useEffect(() => {
    const element = video.current
    if (!element || !isVideo || playing) return
    if (Math.abs(element.currentTime - (frame?.sourceTime ?? 0)) > 0.02) {
      element.currentTime = Math.max(0, frame?.sourceTime ?? 0)
    }
  }, [frame?.sourceTime, isVideo, playing])

  const stop = useCallback(() => {
    onPlayingChange(false)
    video.current?.pause()
  }, [onPlayingChange])

  // The transport. One rAF loop advances the playhead from elapsed wall time and stops at the end.
  useEffect(() => {
    if (!playing) return
    let last = performance.now()
    let current = playhead

    const tick = (now: number) => {
      const delta = (now - last) / 1000
      last = now
      current += delta
      if (current >= timeline.duration) {
        onScrub(timeline.duration)
        stop()
        return
      }
      onScrub(current)
      raf.current = requestAnimationFrame(tick)
    }

    raf.current = requestAnimationFrame(tick)
    return () => { if (raf.current) cancelAnimationFrame(raf.current) }
    // Deliberately not depending on `playhead`: it changes every tick, and re-running would restart the
    // loop each frame. The starting point is read once, when playback begins.
  }, [playing])  // eslint-disable-line react-hooks/exhaustive-deps

  // Play or pause the underlying element alongside the transport, so video and playhead move together.
  useEffect(() => {
    const element = video.current
    if (!element || !isVideo) return
    if (playing) {
      element.currentTime = Math.max(0, frame?.sourceTime ?? 0)
      void element.play().catch(() => { /* autoplay policies; the playhead still advances */ })
    } else {
      element.pause()
    }
  }, [playing, isVideo, frame?.clip.id])  // eslint-disable-line react-hooks/exhaustive-deps

  const atEnd = playhead >= timeline.duration && timeline.duration > 0

  return (
    <div className={cx('flex min-h-0 flex-col', className)}>
      <div className="flex min-h-0 flex-1 items-center justify-center bg-black/60">
        {!frame ? (
          <div className="p-4 text-center text-xs text-[var(--color-ink-dim)]">
            {timeline.duration > 0 ? 'Nothing under the playhead.' : 'The timeline is empty.'}
          </div>
        ) : frame.text ? (
          <div className="p-4 text-center text-sm">{frame.text}</div>
        ) : isVideo && frame.url ? (
          <video
            ref={video}
            key={frame.clip.id}
            src={frame.url}
            muted
            playsInline
            preload="auto"
            className="max-h-full max-w-full object-contain"
          />
        ) : frame.url ? (
          <img src={frame.url} alt="" className="max-h-full max-w-full object-contain" />
        ) : (
          <div className="p-4 text-center text-xs text-[var(--color-bad)]">
            This clip has no media yet.
          </div>
        )}
      </div>

      <div className="flex shrink-0 items-center gap-2 border-t border-[var(--color-edge)] px-2 py-1.5">
        <Button
          size="sm"
          variant={playing ? 'danger' : 'primary'}
          disabled={timeline.duration <= 0}
          onClick={() => {
            if (playing) return stop()
            // Playing from the very end would stop instantly; start over instead.
            if (atEnd) onScrub(0)
            onPlayingChange(true)
          }}
          title={playing ? 'Pause' : 'Play from the playhead'}
        >
          {playing ? '❚❚' : '▶'}
        </Button>
        <Button size="sm" variant="ghost" onClick={() => { stop(); onScrub(0) }} title="Back to the start">
          ⏮
        </Button>
        <span className="font-mono text-[10px] text-[var(--color-ink-dim)]">
          {formatTimecode(playhead, timeline.fps)} / {formatTimecode(timeline.duration, timeline.fps)}
        </span>
        <div className="flex-1" />
        <span className="truncate text-[10px] text-[var(--color-ink-dim)]">
          {frame ? frame.clip.name || 'clip' : '—'}
        </span>
      </div>
    </div>
  )
}
