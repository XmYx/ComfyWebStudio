/**
 * The workspace shell: panels as dockable, tabbable, floatable widgets.
 *
 * Every panel is a widget in a *group*. A group is a tab strip plus whichever tab is in front, and it
 * lives in one of five slots — top, left, centre, right, bottom. Dragging a tab shows the drop zones and
 * puts the widget wherever it lands: an edge to dock it there, the middle for the centre, another group's
 * tab strip to tab it alongside, or outside them all to float it in a window.
 *
 * Two details that are easy to get wrong and matter a lot:
 *
 * * **Height.** Panels are authored as auto-height boxes, and inside a flex or grid cell that collapses
 *   them to their content — for a canvas, to nothing. Every content area forces its child to fill.
 * * **Scrolling.** A docked panel gets a scroll box unless it says it manages its own, because a panel
 *   that cannot scroll simply hides whatever does not fit.
 */

import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'

import {
  DOCK_SLOTS, useLayout, WIDGET_LABELS, type DockSlot, type WidgetId, type WidgetState,
} from '@/store/layout'
import { cx } from '@/components/ui'

const MIN_FLOAT = { w: 240, h: 160 }
const TOP_LIMIT = 8
/** How much of the workspace edge counts as "dock here" while dragging. */
const EDGE_FRACTION = 0.22

/**
 * What a widget's own root element is forced to, once it is inside a dock frame.
 *
 * `h-full` because panels are authored auto-height and would otherwise collapse; the rest strips the
 * border, rounding and background a standalone Panel draws, since the group already provides them.
 */
const PANEL_RESET = '[&>*]:h-full [&>*]:rounded-none [&>*]:border-0 [&>*]:bg-transparent'

interface Props {
  render: Partial<Record<WidgetId, ReactNode>>
}

/** What a drag is currently over: a slot to dock into, a group to join as a tab, or nothing. */
type DropTarget = { kind: 'slot'; slot: DockSlot } | { kind: 'tabs'; slot: DockSlot } | null

interface DragState {
  id: WidgetId
  target: DropTarget
  x: number
  y: number
}

export function Dock({ render }: Props) {
  const widgets = useLayout((s) => s.widgets)
  const active = useLayout((s) => s.active)
  const dockWidget = useLayout((s) => s.dockWidget)
  const floatWidget = useLayout((s) => s.floatWidget)
  const setWidget = useLayout((s) => s.setWidget)

  const workspace = useRef<HTMLDivElement>(null)
  const [drag, setDrag] = useState<DragState | null>(null)

  const groupFor = (slot: DockSlot) =>
    (Object.keys(widgets) as WidgetId[])
      .filter((id) => widgets[id].slot === slot && widgets[id].visible && !widgets[id].floating)
      .filter((id) => render[id] !== undefined)
      .sort((a, b) => widgets[a].order - widgets[b].order)

  const groups = Object.fromEntries(DOCK_SLOTS.map((slot) => [slot, groupFor(slot)])) as Record<
    DockSlot, WidgetId[]
  >
  const floating = (Object.keys(widgets) as WidgetId[]).filter(
    (id) => widgets[id].floating && widgets[id].visible && render[id] !== undefined,
  )

  /**
   * Where a pointer at these coordinates would drop.
   *
   * A tab strip under the pointer wins over the geometric zones: aiming at a specific strip is a more
   * deliberate act than being near an edge, and the two overlap constantly.
   */
  const targetAt = useCallback((x: number, y: number): DropTarget => {
    const strip = document.elementFromPoint(x, y)?.closest('[data-tabstrip]')
    if (strip) return { kind: 'tabs', slot: strip.getAttribute('data-tabstrip') as DockSlot }

    const box = workspace.current?.getBoundingClientRect()
    if (!box || x < box.left || x > box.right || y < box.top || y > box.bottom) return null

    const fx = (x - box.left) / box.width
    const fy = (y - box.top) / box.height
    if (fx < EDGE_FRACTION) return { kind: 'slot', slot: 'left' }
    if (fx > 1 - EDGE_FRACTION) return { kind: 'slot', slot: 'right' }
    if (fy < EDGE_FRACTION) return { kind: 'slot', slot: 'top' }
    if (fy > 1 - EDGE_FRACTION) return { kind: 'slot', slot: 'bottom' }
    return { kind: 'slot', slot: 'centre' }
  }, [])

  /** Begin dragging a widget by its tab or its titlebar. */
  const beginDrag = useCallback(
    (id: WidgetId, event: React.MouseEvent) => {
      event.preventDefault()
      const move = (e: MouseEvent) =>
        setDrag({ id, x: e.clientX, y: e.clientY, target: targetAt(e.clientX, e.clientY) })

      const up = (e: MouseEvent) => {
        window.removeEventListener('mousemove', move)
        window.removeEventListener('mouseup', up)
        setDrag(null)

        const target = targetAt(e.clientX, e.clientY)
        if (target) {
          dockWidget(id, target.slot)
          return
        }
        // Dropped outside the workspace: float it, opening the window where it was let go.
        setWidget(id, {
          rect: {
            x: Math.max(0, e.clientX - 120),
            y: Math.max(TOP_LIMIT, e.clientY - 12),
            w: Math.max(MIN_FLOAT.w, widgets[id].rect.w),
            h: Math.max(MIN_FLOAT.h, widgets[id].rect.h),
          },
        })
        floatWidget(id, true)
      }

      window.addEventListener('mousemove', move)
      window.addEventListener('mouseup', up)
    },
    [targetAt, dockWidget, floatWidget, setWidget, widgets],
  )

  const rows = [
    groups.top.length ? 'minmax(0, 200px)' : null,
    'minmax(0, 1fr)',
    groups.bottom.length ? 'minmax(0, 240px)' : null,
  ].filter(Boolean).join(' ')

  const columns = [
    groups.left.length ? '260px' : null,
    'minmax(0, 1fr)',
    groups.right.length ? '340px' : null,
  ].filter(Boolean).join(' ')

  return (
    <>
      <div
        ref={workspace}
        className="relative grid h-full gap-2 p-2"
        style={{ gridTemplateRows: rows, gridTemplateColumns: columns }}
      >
        {groups.top.length > 0 && (
          <div className="col-span-full min-h-0">
            <DockGroup slot="top" ids={groups.top} active={active.top} render={render} onDrag={beginDrag} />
          </div>
        )}

        {groups.left.length > 0 && (
          <DockGroup slot="left" ids={groups.left} active={active.left} render={render} onDrag={beginDrag} />
        )}

        <DockGroup
          slot="centre" ids={groups.centre} active={active.centre} render={render} onDrag={beginDrag}
        />

        {groups.right.length > 0 && (
          <DockGroup slot="right" ids={groups.right} active={active.right} render={render} onDrag={beginDrag} />
        )}

        {groups.bottom.length > 0 && (
          <div className="col-span-full min-h-0">
            <DockGroup
              slot="bottom" ids={groups.bottom} active={active.bottom} render={render} onDrag={beginDrag}
            />
          </div>
        )}

        {drag && <DropOverlay target={drag.target} label={WIDGET_LABELS[drag.id]} />}
      </div>

      {floating.map((id) => (
        <FloatingWidget key={id} id={id} onDrag={beginDrag}>{render[id]}</FloatingWidget>
      ))}

      {drag && (
        <div
          className="pointer-events-none fixed z-50 rounded bg-[var(--color-accent)] px-2 py-1 text-[10px] text-white shadow-lg"
          style={{ left: drag.x + 12, top: drag.y + 12 }}
        >
          {WIDGET_LABELS[drag.id]}
        </div>
      )}
    </>
  )
}

/** A tab strip plus whichever tab is in front. */
function DockGroup({
  slot, ids, active, render, onDrag,
}: {
  slot: DockSlot
  ids: WidgetId[]
  active: WidgetId | null
  render: Props['render']
  onDrag: (id: WidgetId, event: React.MouseEvent) => void
}) {
  const widgets = useLayout((s) => s.widgets)
  const setActive = useLayout((s) => s.setActive)
  const toggleWidget = useLayout((s) => s.toggleWidget)
  const floatWidget = useLayout((s) => s.floatWidget)

  // An empty centre still renders, so there is always something to drop back into.
  const shown = active && ids.includes(active) ? active : ids[0]

  return (
    <div className="flex min-h-0 min-w-0 flex-col overflow-hidden rounded-lg border border-[var(--color-edge)] bg-[var(--color-panel)]">
      <div
        data-tabstrip={slot}
        className="flex shrink-0 items-stretch gap-px overflow-x-auto border-b border-[var(--color-edge)] bg-[var(--color-panel-2)]"
      >
        {ids.map((id) => (
          <button
            key={id}
            // The tab is the drag handle — for a tabbed panel that is what "move it by its titlebar"
            // means. A press that never moves just selects the tab.
            onMouseDown={(event) => { setActive(slot, id); onDrag(id, event) }}
            className={cx(
              'cursor-grab whitespace-nowrap px-2.5 py-1 text-[11px] transition-colors active:cursor-grabbing',
              id === shown
                ? 'bg-[var(--color-panel)] text-[var(--color-ink)]'
                : 'text-[var(--color-ink-dim)] hover:bg-[var(--color-panel)]/60',
            )}
          >
            {WIDGET_LABELS[id]}
          </button>
        ))}
        <div className="flex-1" />
        {shown && (
          <div className="flex items-center gap-0.5 pr-1">
            <StripButton title="Pop out into a window" onClick={() => floatWidget(shown, true)}>⧉</StripButton>
            <StripButton title="Hide this panel" onClick={() => toggleWidget(shown)}>✕</StripButton>
          </div>
        )}
      </div>

      <div
        className={cx(
          'min-h-0 flex-1',
          // The group already draws the frame, so the panel inside drops its own — otherwise every
          // widget sits in a box inside a box.
          PANEL_RESET,
          // A panel that manages its own viewport (a canvas, the timeline) must not be put in a scroll
          // box; everything else gets one, or content that does not fit is simply invisible.
          shown && widgets[shown].noScroll ? 'overflow-hidden' : 'overflow-auto',
        )}
      >
        {shown ? render[shown] : (
          <div className="p-4 text-center text-xs text-[var(--color-ink-dim)]">Drag a panel here.</div>
        )}
      </div>
    </div>
  )
}

/** The zone highlight shown while dragging, so where a drop will land is never a guess. */
function DropOverlay({ target, label }: { target: DropTarget; label: string }) {
  if (!target) {
    return (
      <div className="pointer-events-none absolute inset-0 z-30 flex items-start justify-center pt-2">
        <span className="rounded bg-[var(--color-panel-2)] px-2 py-1 text-[10px] text-[var(--color-ink-dim)]">
          Release outside to float {label}
        </span>
      </div>
    )
  }

  const box: Record<DockSlot, string> = {
    left: 'left-0 top-0 h-full w-[22%]',
    right: 'right-0 top-0 h-full w-[22%]',
    top: 'left-0 top-0 h-[22%] w-full',
    bottom: 'bottom-0 left-0 h-[22%] w-full',
    centre: 'inset-[22%]',
  }

  return (
    <div className="pointer-events-none absolute inset-0 z-30">
      <div
        className={cx(
          'absolute rounded border-2 border-[var(--color-accent)] bg-[var(--color-accent)]/20',
          box[target.slot],
        )}
      />
      <div className="absolute inset-x-0 top-2 text-center">
        <span className="rounded bg-[var(--color-accent)] px-2 py-1 text-[10px] text-white">
          {target.kind === 'tabs' ? `Tab ${label} into ${target.slot}` : `Dock ${label} ${target.slot}`}
        </span>
      </div>
    </div>
  )
}

function FloatingWidget({
  id, children, onDrag,
}: {
  id: WidgetId
  children: ReactNode
  onDrag: (id: WidgetId, event: React.MouseEvent) => void
}) {
  const widget = useLayout((s) => s.widgets[id])
  const setWidget = useLayout((s) => s.setWidget)
  const floatWidget = useLayout((s) => s.floatWidget)
  const toggleWidget = useLayout((s) => s.toggleWidget)

  const rect = useRef<WidgetState['rect']>(widget.rect)
  rect.current = widget.rect

  /**
   * Moving and resizing a floating window.
   *
   * Listeners live on the window: a pointer moving faster than React re-renders routinely leaves the
   * element, and a window that stops following the mouse feels broken. Geometry is written to the store
   * on release so a drag is one persisted change rather than a hundred.
   */
  const gesture = useCallback(
    (event: React.MouseEvent, mode: 'move' | 'resize') => {
      event.preventDefault()
      const from = { x: event.clientX, y: event.clientY }
      const start = { ...rect.current }
      const element = (event.currentTarget as HTMLElement).closest('[data-floating]') as HTMLElement

      const move = (e: MouseEvent) => {
        const dx = e.clientX - from.x
        const dy = e.clientY - from.y
        rect.current =
          mode === 'move'
            ? {
                x: Math.max(0, Math.min(window.innerWidth - 80, start.x + dx)),
                y: Math.max(TOP_LIMIT, Math.min(window.innerHeight - 40, start.y + dy)),
                w: start.w,
                h: start.h,
              }
            : {
                x: start.x,
                y: start.y,
                w: Math.max(MIN_FLOAT.w, start.w + dx),
                h: Math.max(MIN_FLOAT.h, start.h + dy),
              }
        if (element) {
          element.style.left = `${rect.current.x}px`
          element.style.top = `${rect.current.y}px`
          element.style.width = `${rect.current.w}px`
          element.style.height = `${rect.current.h}px`
        }
      }
      const up = () => {
        window.removeEventListener('mousemove', move)
        window.removeEventListener('mouseup', up)
        setWidget(id, { rect: rect.current })
      }
      window.addEventListener('mousemove', move)
      window.addEventListener('mouseup', up)
    },
    [id, setWidget],
  )

  // A window remembered from a larger screen would otherwise open off the edge, unreachable.
  useEffect(() => {
    const x = Math.min(widget.rect.x, Math.max(0, window.innerWidth - 120))
    const y = Math.min(widget.rect.y, Math.max(TOP_LIMIT, window.innerHeight - 60))
    if (x !== widget.rect.x || y !== widget.rect.y) setWidget(id, { rect: { ...widget.rect, x, y } })
  }, [])  // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div
      data-floating={id}
      className="fixed z-40 flex flex-col overflow-hidden rounded-lg border border-[var(--color-accent)]/60 bg-[var(--color-panel)] shadow-2xl"
      style={{ left: widget.rect.x, top: widget.rect.y, width: widget.rect.w, height: widget.rect.h }}
    >
      <div
        className="flex shrink-0 cursor-move select-none items-center gap-2 border-b border-[var(--color-edge)] bg-[var(--color-panel-2)] px-2 py-1"
        // Alt-drag re-docks, using the same gesture the tabs use, so a floating window can be put back
        // anywhere. A plain drag just moves the window, which is what people expect of a titlebar.
        onMouseDown={(event) => (event.altKey ? onDrag(id, event) : gesture(event, 'move'))}
        title="Drag to move · Alt-drag to dock it somewhere"
      >
        <span className="flex-1 truncate text-[11px] font-semibold">{WIDGET_LABELS[id]}</span>
        <StripButton title="Dock it again" onClick={() => floatWidget(id, false)}>⇲</StripButton>
        <StripButton title="Close" onClick={() => toggleWidget(id)}>✕</StripButton>
      </div>

      <div
        className={cx(
          'min-h-0 flex-1',
          PANEL_RESET,
          widget.noScroll ? 'overflow-hidden' : 'overflow-auto',
        )}
      >
        {children}
      </div>

      <div
        title="Resize"
        onMouseDown={(event) => gesture(event, 'resize')}
        className="absolute bottom-0 right-0 h-3 w-3 cursor-nwse-resize bg-[var(--color-accent)]/40"
      />
    </div>
  )
}

function StripButton({
  children, title, onClick,
}: { children: ReactNode; title: string; onClick: () => void }) {
  return (
    <button
      title={title}
      onClick={(event) => { event.stopPropagation(); onClick() }}
      onMouseDown={(event) => event.stopPropagation()}
      className="rounded px-1 text-[10px] text-[var(--color-ink-dim)] hover:text-[var(--color-accent)]"
    >
      {children}
    </button>
  )
}
