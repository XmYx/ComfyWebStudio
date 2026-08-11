/**
 * Hearing the timeline.
 *
 * The monitor draws pictures; this plays the sound that goes with them. It keeps one `<audio>` element per
 * audio clip and follows the shared playhead, so pressing play in the monitor, on the timeline, or in a
 * floating window all produce the same sound.
 *
 * Built on Web Audio rather than each element's own `volume`, for one reason that matters: an element can
 * only be made quieter, not *placed*. Routing each one through a gain and a stereo panner is what lets a
 * track's pan be heard while cutting rather than only appearing in the finished render.
 *
 * The mix here deliberately mirrors `render/encoder.py`: gains multiply, pans add, and solo silences
 * everything else. What you hear while cutting is what the file will contain.
 */

import { useEffect, useMemo, useRef } from 'react'

import { api } from '@/api/client'
import type { Project, ResolvedTimeline } from '@/api/types'
import { useStudio } from '@/store/studio'

/** How far out of step with the playhead a clip may drift before it is nudged back. */
const RESYNC_THRESHOLD_S = 0.25

interface Playable {
  clipId: string
  url: string
  /** Timeline seconds. */
  start: number
  duration: number
  /** Seconds into the source file. */
  inPoint: number
  gain: number
  pan: number
}

interface Wiring {
  gain: GainNode
  panner: StereoPannerNode
}

export function TimelineAudio({
  project, resolved,
}: { project: Project; resolved: ResolvedTimeline | undefined }) {
  const playhead = useStudio((s) => s.playhead)
  const playing = useStudio((s) => s.playing)

  const context = useRef<AudioContext | null>(null)
  const elements = useRef(new Map<string, HTMLAudioElement>())
  /**
   * Keyed by the element, not the clip.
   *
   * An element may only ever be given to `createMediaElementSource` once — a second call throws and
   * leaves it permanently detached from the graph, silent for the rest of the session. React reuses the
   * same DOM node across renders while ref callbacks come and go, so anything keyed by clip id loses
   * track of what has already been wired. A WeakMap on the node itself cannot.
   */
  const wiring = useRef(new WeakMap<HTMLAudioElement, Wiring>())

  /** Every audible clip with its media and its share of the mix, in one flat list. */
  const playable = useMemo<Playable[]>(() => {
    const paths = new Map(
      (resolved?.clips ?? []).map((entry) => [entry.clip_id, entry.artifacts?.[0]?.path]),
    )
    const audioTracks = project.timeline.tracks.filter((track) => track.kind === 'audio')
    // Solo wins over mute, exactly as the renderer decides it.
    const soloed = audioTracks.filter((track) => track.solo)
    const audible = soloed.length ? soloed : audioTracks.filter((track) => !track.muted)

    const result: Playable[] = []
    for (const track of audible) {
      for (const clip of track.clips) {
        const path = paths.get(clip.id)
        if (!clip.enabled || !path) continue
        result.push({
          clipId: clip.id,
          url: api.media.url(project.id, path),
          start: clip.start,
          duration: clip.duration,
          inPoint: clip.in_point,
          gain: Math.max(0, clip.volume) * Math.max(0, track.volume),
          pan: Math.max(-1, Math.min(1, clip.pan + track.pan)),
        })
      }
    }
    return result
  }, [project, resolved])

  useEffect(() => {
    const open = context.current
    const pool = elements.current
    return () => {
      for (const element of pool.values()) element.pause()
      void open?.close()
      context.current = null
    }
  }, [])

  useEffect(() => {
    if (!playable.length) return

    // Created on the first play, never before: browsers refuse an AudioContext without a gesture, and
    // one made at load time sits suspended and silent.
    if (playing && !context.current) context.current = new AudioContext()
    const audio = context.current
    if (playing) void audio?.resume()

    for (const entry of playable) {
      const element = elements.current.get(entry.clipId)
      if (!element) continue

      if (audio && !wiring.current.has(element)) {
        try {
          const gain = audio.createGain()
          const panner = audio.createStereoPanner()
          audio
            .createMediaElementSource(element)
            .connect(gain)
            .connect(panner)
            .connect(audio.destination)
          wiring.current.set(element, { gain, panner })
        } catch (error) {
          // Never fatal: without an error boundary a throw in here unmounts the whole page, and losing
          // the timeline because the pan control could not be wired is a bad trade.
          console.warn('[Timeline] could not route audio through the mixer', error)
        }
      }
      const wired = wiring.current.get(element)
      if (wired) {
        wired.gain.gain.value = entry.gain
        wired.panner.pan.value = entry.pan
      } else {
        // Before the first play there is no graph, so the element's own volume is the only control.
        element.volume = Math.min(1, entry.gain)
      }

      const offset = playhead - entry.start
      const inside = offset >= 0 && offset < entry.duration
      const at = entry.inPoint + Math.max(0, offset)

      if (!inside || !playing) {
        if (!element.paused) element.pause()
        // Seek even while paused, so scrubbing leaves it ready to play from the right place.
        if (inside && Number.isFinite(at)) element.currentTime = at
        continue
      }

      // Only correct a real drift: assigning currentTime every frame stutters the decoder.
      if (Math.abs(element.currentTime - at) > RESYNC_THRESHOLD_S) element.currentTime = at
      if (element.paused) void element.play().catch(() => {})
    }
  }, [playable, playhead, playing])

  // Rendered rather than created imperatively: React owns their lifetime, and they can be seen in the
  // inspector when something is not making the noise it should.
  return (
    <div hidden data-testid="timeline-audio">
      {playable.map((entry) => (
        <audio
          key={entry.clipId}
          data-clip={entry.clipId}
          src={entry.url}
          preload="auto"
          crossOrigin="anonymous"
          ref={(element) => {
            if (element) elements.current.set(entry.clipId, element)
            else elements.current.delete(entry.clipId)
          }}
        />
      ))}
    </div>
  )
}
