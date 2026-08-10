/**
 * A thumbnail that plays on hover and scrubs when you move.
 *
 * Resting the pointer on a clip plays it, which is what you want when you are just checking *what* a step
 * produced. Moving across it takes over and maps horizontal position to time, which is what you want when
 * you are looking for a particular moment. Stop moving and it picks the playback back up from wherever you
 * left the cursor.
 *
 * The still is what shows at rest — a node full of autoplaying video is noise, and decoding every clip on a
 * canvas at once is worse. For the same reason the video element only exists while hovering: mounting a
 * dozen of them would have the browser fetch and decode a dozen files nobody is looking at.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { cx } from '@/components/ui'

/**
 * How far the pointer must travel before playing becomes scrubbing.
 *
 * Entering an element almost always comes with a mousemove or two in the same gesture, so without a
 * threshold the playback we just started would be cancelled a frame later and hovering would never play.
 */
const SCRUB_THRESHOLD_PX = 8

/** How long the pointer must sit still before scrubbing hands back to playback. */
const RESUME_AFTER_MS = 600

interface Props {
  /** The still shown at rest. */
  thumbUrl: string | null
  /** The video to play and scrub. When absent this is just an image. */
  videoUrl?: string | null
  /** Known duration in seconds, used until the element reports its own. */
  duration?: number | null
  className?: string
  /** Extra classes for the media itself, so a node and a list row can size it differently. */
  mediaClassName?: string
}

type Mode = 'idle' | 'playing' | 'scrubbing'

export function HoverScrub({
  thumbUrl, videoUrl, duration, className, mediaClassName = 'h-24 w-full object-contain',
}: Props) {
  const [mode, setMode] = useState<Mode>('idle')
  const [position, setPosition] = useState(0)

  const video = useRef<HTMLVideoElement>(null)
  const frame = useRef<number>()
  const resumeTimer = useRef<number>()
  /** Where the pointer entered, so we can tell a real move from the jitter of arriving. */
  const entry = useRef<{ x: number; y: number } | null>(null)
  /** Read inside async callbacks, which would otherwise close over a stale position. */
  const positionRef = useRef(0)
  positionRef.current = position

  const lengthOf = (element: HTMLVideoElement) =>
    Number.isFinite(element.duration) && element.duration > 0 ? element.duration : (duration ?? 0)

  // Seeking on every mousemove queues more seeks than the decoder can service and the picture ends up
  // lagging behind the cursor. One seek per animation frame keeps it responsive.
  const seek = useCallback((fraction: number) => {
    setPosition(fraction)
    if (frame.current) cancelAnimationFrame(frame.current)
    frame.current = requestAnimationFrame(() => {
      const element = video.current
      if (!element) return
      const length = lengthOf(element)
      if (length > 0) element.currentTime = Math.min(length - 0.001, Math.max(0, fraction * length))
    })
  }, [duration])

  const play = useCallback(() => {
    setMode('playing')
    // play() rejects if the element is torn down mid-promise, or if autoplay policy objects. Neither is
    // worth an unhandled rejection in the console — the still simply stays put.
    void video.current?.play().catch(() => {})
  }, [])

  const stopTimers = () => {
    if (frame.current) cancelAnimationFrame(frame.current)
    if (resumeTimer.current) clearTimeout(resumeTimer.current)
  }

  useEffect(() => stopTimers, [])

  const scrubbable = Boolean(videoUrl)
  const hovering = mode !== 'idle'

  const onEnter = (event: React.MouseEvent) => {
    if (!scrubbable) return
    entry.current = { x: event.clientX, y: event.clientY }
    const box = event.currentTarget.getBoundingClientRect()
    // Start where the pointer came in rather than at zero: entering halfway across a clip and being
    // thrown back to the first frame reads as the scrub being broken.
    setPosition(box.width > 0 ? Math.min(1, Math.max(0, (event.clientX - box.left) / box.width)) : 0)
    setMode('playing')
  }

  const onMove = (event: React.MouseEvent) => {
    if (!scrubbable) return
    const from = entry.current
    if (from) {
      const travelled = Math.hypot(event.clientX - from.x, event.clientY - from.y)
      if (travelled < SCRUB_THRESHOLD_PX) return
    }

    const box = event.currentTarget.getBoundingClientRect()
    if (box.width <= 0) return

    setMode('scrubbing')
    video.current?.pause()
    seek(Math.min(1, Math.max(0, (event.clientX - box.left) / box.width)))

    // Sitting still is a hover again, so hand back to playback from wherever the cursor left off.
    if (resumeTimer.current) clearTimeout(resumeTimer.current)
    resumeTimer.current = window.setTimeout(play, RESUME_AFTER_MS)
  }

  const onLeave = () => {
    stopTimers()
    entry.current = null
    setMode('idle')
    setPosition(0)
  }

  return (
    <div
      className={cx('relative overflow-hidden', className)}
      onMouseEnter={onEnter}
      onMouseLeave={onLeave}
      onMouseMove={onMove}
    >
      {hovering && videoUrl ? (
        <video
          ref={video}
          src={videoUrl}
          muted
          loop
          playsInline
          preload="metadata"
          className={mediaClassName}
          // Metadata arrives after mount, so the entry position and the initial play both have to wait
          // for it — seeking or playing before this lands is silently dropped.
          onLoadedMetadata={() => {
            seek(positionRef.current)
            if (mode === 'playing') play()
          }}
          // While playing, the marker should follow the video rather than the cursor.
          onTimeUpdate={(event) => {
            if (mode !== 'playing') return
            const element = event.currentTarget
            const length = lengthOf(element)
            if (length > 0) setPosition(Math.min(1, element.currentTime / length))
          }}
        />
      ) : thumbUrl ? (
        <img src={thumbUrl} alt="" className={mediaClassName} />
      ) : (
        <div className={cx(mediaClassName, 'bg-black/40')} />
      )}

      {scrubbable && (
        <>
          {/* A hairline showing where in the clip we are — the playhead or the cursor, as appropriate. */}
          {hovering && (
            <div
              className="pointer-events-none absolute inset-y-0 w-px bg-[var(--color-accent)]"
              style={{ left: `${position * 100}%` }}
            />
          )}
          <span className="pointer-events-none absolute bottom-0.5 right-0.5 rounded bg-black/60 px-1 text-[8px] text-white/80">
            {mode === 'idle' ? '▶' : mode === 'playing' ? '▶ playing' : `${Math.round(position * 100)}%`}
          </span>
        </>
      )}
    </div>
  )
}
