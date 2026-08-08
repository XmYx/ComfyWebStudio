/**
 * The live event stream.
 *
 * One websocket for the whole app. It reconnects on its own, because a dropped socket during a long render
 * should not silently leave the UI frozen on stale progress.
 */

import { useEffect, useRef, useState } from 'react'
import type { StudioEvent } from './types'

type Handler = (event: StudioEvent) => void

const RECONNECT_DELAYS = [500, 1000, 2000, 5000, 10000]

class EventStream {
  private socket: WebSocket | null = null
  private handlers = new Set<Handler>()
  private attempt = 0
  private closing = false
  private timer: number | undefined
  private projectId: string | null = null

  connect(projectId: string | null) {
    if (this.projectId === projectId && this.socket?.readyState === WebSocket.OPEN) return
    this.projectId = projectId
    this.close()
    this.closing = false
    this.open()
  }

  private open() {
    const protocol = location.protocol === 'https:' ? 'wss' : 'ws'
    const query = this.projectId ? `?project_id=${encodeURIComponent(this.projectId)}` : ''
    const socket = new WebSocket(`${protocol}://${location.host}/api/events${query}`)
    this.socket = socket

    socket.onopen = () => {
      this.attempt = 0
      this.emit({ type: 'stream.connected', project_id: this.projectId, run_id: null, step_id: null, data: {}, ts: Date.now() })
    }
    socket.onmessage = (message) => {
      try {
        const event = JSON.parse(message.data) as StudioEvent
        if (event.type !== 'ping') this.emit(event)
      } catch {
        /* ignore malformed frames */
      }
    }
    socket.onclose = () => {
      this.socket = null
      if (this.closing) return
      this.emit({ type: 'stream.disconnected', project_id: this.projectId, run_id: null, step_id: null, data: {}, ts: Date.now() })
      const delay = RECONNECT_DELAYS[Math.min(this.attempt, RECONNECT_DELAYS.length - 1)]
      this.attempt += 1
      this.timer = window.setTimeout(() => this.open(), delay)
    }
    socket.onerror = () => socket.close()
  }

  private emit(event: StudioEvent) {
    for (const handler of this.handlers) handler(event)
  }

  subscribe(handler: Handler) {
    this.handlers.add(handler)
    return () => this.handlers.delete(handler)
  }

  close() {
    this.closing = true
    window.clearTimeout(this.timer)
    this.socket?.close()
    this.socket = null
  }
}

export const eventStream = new EventStream()

/** Subscribe to studio events for the lifetime of a component. */
export function useStudioEvents(handler: Handler, deps: unknown[] = []) {
  const ref = useRef(handler)
  ref.current = handler
  useEffect(() => {
    const unsubscribe = eventStream.subscribe((event) => ref.current(event))
    return () => { unsubscribe() }
  }, deps)
}

/** Connection state, for the status indicator in the header. */
export function useStreamStatus(): 'connected' | 'disconnected' {
  const [status, setStatus] = useState<'connected' | 'disconnected'>('disconnected')
  useStudioEvents((event) => {
    if (event.type === 'stream.connected') setStatus('connected')
    if (event.type === 'stream.disconnected') setStatus('disconnected')
  })
  return status
}
