/**
 * A thumbnail that becomes a scrubbable video on hover.
 *
 * Moving the mouse across it maps horizontal position to time, so you can find the moment you care about
 * without opening anything. The still is what shows at rest — a node full of autoplaying video is noise,
 * and decoding every clip on a canvas at once is worse.
 *
 * The video element is only mounted while hovering. That matters on a canvas with a dozen nodes: mounting
 * them all would have the browser fetch and decode a dozen files nobody is looking at.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { cx } from '@/components/ui'

interface Props {
  /** The still shown at rest. */
  thumbUrl: string | null
  /** The video to scrub. When absent this is just an image. */
  videoUrl?: string | null
  /** Known duration in seconds, used until the element reports its own. */
  duration?: number | null
  className?: string
  /** Extra classes for the media itself, so a node and a list row can size it differently. */
  mediaClassName?: string
}

export function HoverScrub({
  thumbUrl, videoUrl, duration, className, mediaClassName = 'h-24 w-full object-contain',
}: Props) {
  const [hovering, setHovering] = useState(false)
  const [position, setPosition] = useState(0)
  const video = useRef<HTMLVideoElement>(null)
  const frame = useRef<number>()

  // Seeking on every mousemove would queue more seeks than the decoder can service, and the picture ends
  // up lagging behind the cursor. One seek per animation frame keeps it responsive.
  const seek = useCallback((fraction: number) => {
    setPosition(fraction)
    if (frame.current) cancelAnimationFrame(frame.current)
    frame.current = requestAnimationFrame(() => {
      const element = video.current
      if (!element) return
      const length = Number.isFinite(element.duration) ? element.duration : (duration ?? 0)
      if (length > 0) element.currentTime = Math.min(length - 0.001, Math.max(0, fraction * length))
    })
  }, [duration])

  useEffect(() => () => { if (frame.current) cancelAnimationFrame(frame.current) }, [])

  const scrubbable = Boolean(videoUrl)

  return (
    <div
      className={cx('relative overflow-hidden', className)}
      onMouseEnter={() => scrubbable && setHovering(true)}
      onMouseLeave={() => { setHovering(false); setPosition(0) }}
      onMouseMove={(event) => {
        if (!scrubbable) return
        const box = event.currentTarget.getBoundingClientRect()
        if (box.width <= 0) return
        seek(Math.min(1, Math.max(0, (event.clientX - box.left) / box.width)))
      }}
    >
      {hovering && videoUrl ? (
        <video
          ref={video}
          src={videoUrl}
          muted
          playsInline
          preload="metadata"
          className={mediaClassName}
          // Metadata arriving after the first move would otherwise leave the frame at zero.
          onLoadedMetadata={() => seek(position)}
        />
      ) : thumbUrl ? (
        <img src={thumbUrl} alt="" className={mediaClassName} />
      ) : (
        <div className={cx(mediaClassName, 'bg-black/40')} />
      )}

      {scrubbable && (
        <>
          {/* A hairline showing where in the clip the cursor is. */}
          {hovering && (
            <div
              className="pointer-events-none absolute inset-y-0 w-px bg-[var(--color-accent)]"
              style={{ left: `${position * 100}%` }}
            />
          )}
          <span className="pointer-events-none absolute bottom-0.5 right-0.5 rounded bg-black/60 px-1 text-[8px] text-white/80">
            {hovering ? `${Math.round(position * 100)}%` : '▶'}
          </span>
        </>
      )}
    </div>
  )
}
