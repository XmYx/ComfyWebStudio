/**
 * An audio clip's shape, drawn from peaks the server derived.
 *
 * Audio is edited by eye as much as by ear — you cut on the silence between words, and you cannot see
 * silence in a coloured rectangle. The samples never reach the browser: the server sends a few hundred
 * min/max pairs, which is all a drawing this size can show anyway.
 */

import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'

import { api } from '@/api/client'

interface Props {
  projectId: string
  /** The stored path of the audio file behind this clip. */
  path: string
  /** Which slice of the file the clip covers, so trimming redraws rather than rescaling. */
  inPoint?: number
  duration?: number
  className?: string
}

export function Waveform({ projectId, path, inPoint = 0, duration, className }: Props) {
  const { data } = useQuery({
    // Keyed on the path alone: the file is content-addressed, so its peaks never change.
    queryKey: ['waveform', projectId, path],
    queryFn: () => api.media.waveform(projectId, path),
    staleTime: Infinity,
    retry: false,
  })

  const points = useMemo(() => {
    if (!data?.peaks?.length) return null

    // Show only the part of the file the clip actually uses.
    const total = data.duration || 0
    const from = total > 0 ? Math.max(0, Math.min(1, inPoint / total)) : 0
    const to =
      total > 0 && duration ? Math.max(from, Math.min(1, (inPoint + duration) / total)) : 1
    const slice = data.peaks.slice(
      Math.floor(from * data.peaks.length),
      Math.max(Math.floor(from * data.peaks.length) + 1, Math.ceil(to * data.peaks.length)),
    )
    if (!slice.length) return null

    // One polygon: the highs left to right, then the lows back again. Cheaper for the browser than a
    // bar per bucket, and it reads as a continuous waveform rather than a comb.
    const step = 100 / slice.length
    const highs = slice.map(([, high], i) => `${(i * step).toFixed(3)},${(50 - high * 48).toFixed(2)}`)
    const lows = slice
      .map(([low], i) => `${(i * step).toFixed(3)},${(50 - low * 48).toFixed(2)}`)
      .reverse()
    return [...highs, ...lows].join(' ')
  }, [data, inPoint, duration])

  if (!points) return null

  return (
    <svg
      className={className}
      viewBox="0 0 100 100"
      preserveAspectRatio="none"
      aria-hidden
      // The clip is the interactive thing; the drawing inside it must not eat drags.
      style={{ pointerEvents: 'none' }}
    >
      <polygon points={points} fill="currentColor" fillOpacity={0.55} />
      <line x1="0" y1="50" x2="100" y2="50" stroke="currentColor" strokeOpacity={0.35} strokeWidth={0.5} />
    </svg>
  )
}
