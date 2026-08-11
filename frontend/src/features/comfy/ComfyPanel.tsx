/**
 * ComfyUI, embedded.
 *
 * Editing a workflow used to mean leaving for another browser tab and finding your way back. ComfyUI is
 * a whole application, so it is not reimplemented here — it is simply framed, and it works because it
 * ships no framing restrictions and lives on the same host as this app (a different port is still the
 * same *site*, so its origin-only guard lets the frame through).
 *
 * Everything that makes the embedded copy useful is already in place: our node pack's bridge extension
 * runs inside the frame exactly as it does in a tab, so opening a workflow here and saving it there syncs
 * back to the framework the same way.
 *
 * The panel shows whatever `comfyUrl` in the studio store points at, which is how "open in ComfyUI" from
 * anywhere in the app lands here without those callers knowing this component exists.
 */

import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { api } from '@/api/client'
import { useStudio } from '@/store/studio'
import { selectMaximized, useLayout } from '@/store/layout'
import { Button, Callout, Empty, Panel, PanelHeader, Spinner, cx } from '@/components/ui'

export function ComfyPanel() {
  const comfyUrl = useStudio((s) => s.comfyUrl)
  const comfyNonce = useStudio((s) => s.comfyNonce)
  const showInComfy = useStudio((s) => s.showInComfy)
  const maximized = useLayout(selectMaximized)
  const toggleMaximized = useLayout((s) => s.toggleMaximized)

  const [loading, setLoading] = useState(true)

  const { data: backends, isLoading, error } = useQuery({
    queryKey: ['backends'],
    queryFn: api.settings.backends,
  })
  const backend = backends?.find((entry) => entry.enabled) ?? backends?.[0]

  // The nonce is carried in the fragment: it re-keys the element so React remounts the frame, without
  // becoming a query parameter ComfyUI would have to ignore.
  const src = useMemo(() => {
    const base = comfyUrl ?? backend?.base_url
    if (!base) return null
    return `${base}${base.includes('#') ? '' : `#ws=${comfyNonce}`}`
  }, [comfyUrl, backend?.base_url, comfyNonce])

  const isMaximized = maximized === 'comfy'

  return (
    <Panel className="flex h-full min-h-0 flex-col overflow-hidden">
      <PanelHeader
        actions={
          <>
            {comfyUrl && (
              <Button
                size="sm"
                variant="ghost"
                title="Go back to ComfyUI's own start page"
                onClick={() => showInComfy(null)}
              >
                Home
              </Button>
            )}
            <Button
              size="sm"
              variant="ghost"
              title="Reload the embedded ComfyUI"
              onClick={() => { setLoading(true); showInComfy(comfyUrl) }}
            >
              ↻
            </Button>
            {src && (
              <a
                href={src}
                target="_blank"
                rel="noreferrer"
                title="Open this in a real browser tab instead"
                className="rounded px-1.5 py-0.5 text-[11px] text-[var(--color-ink-dim)] hover:text-[var(--color-accent)]"
              >
                ↗
              </a>
            )}
            <Button
              size="sm"
              variant="ghost"
              title={isMaximized ? 'Restore the layout' : 'Fill the workspace with ComfyUI'}
              onClick={() => toggleMaximized('comfy')}
            >
              {isMaximized ? '⤡' : '⤢'}
            </Button>
          </>
        }
      >
        ComfyUI
      </PanelHeader>

      <div className="relative min-h-0 flex-1 bg-black/30">
        {isLoading && <Empty title="Looking for ComfyUI…" />}

        {error && (
          <div className="p-3">
            <Callout tone="bad">Could not read the configured backends.</Callout>
          </div>
        )}

        {!isLoading && !src && (
          <Empty title="No ComfyUI configured">
            Add a backend on the Settings page and it will appear here.
          </Empty>
        )}

        {src && (
          <>
            {loading && (
              <div className="pointer-events-none absolute inset-0 z-10 grid place-items-center">
                <Spinner />
              </div>
            )}
            <iframe
              // Remounts whenever the URL or the nonce changes, which is what makes "open this workflow"
              // reload the frame even when it is already showing that workflow.
              key={src}
              src={src}
              title="ComfyUI"
              onLoad={() => setLoading(false)}
              className={cx('h-full w-full border-0', loading && 'opacity-0')}
            />
          </>
        )}
      </div>
    </Panel>
  )
}
