/**
 * Right-click menus.
 *
 * One component and one hook serve every surface. A caller supplies a list of items — a mix of registry
 * commands (which already know their own label, shortcut and enabled state) and local actions specific to
 * the thing that was clicked.
 *
 * The menu flips itself away from the viewport edges, so right-clicking near the bottom of the window
 * still shows the whole menu.
 */

import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'

import { COMMANDS_BY_ID, formatShortcut, type CommandContext } from '@/features/menu/commands'
import { cx } from './ui'

export type MenuItem =
  | { type: 'command'; id: string }
  | { type: 'action'; label: string; onSelect: () => void; shortcut?: string; disabled?: boolean; danger?: boolean; checked?: boolean }
  | { type: 'submenu'; label: string; items: MenuItem[]; disabled?: boolean }
  | { type: 'separator' }
  | { type: 'header'; label: string }

export interface ContextMenuState {
  x: number
  y: number
  items: MenuItem[]
}

/** Opens and closes a context menu. Returns the state plus an `onContextMenu` handler factory. */
export function useContextMenu() {
  const [menu, setMenu] = useState<ContextMenuState | null>(null)

  const open = (event: React.MouseEvent, items: MenuItem[]) => {
    if (!items.length) return
    event.preventDefault()
    event.stopPropagation()
    setMenu({ x: event.clientX, y: event.clientY, items })
  }

  return { menu, open, close: () => setMenu(null) }
}

export function ContextMenu({
  state, onClose, context,
}: { state: ContextMenuState | null; onClose: () => void; context: CommandContext }) {
  if (!state) return null
  return createPortal(
    <Surface state={state} onClose={onClose} context={context} />,
    document.body,
  )
}

function Surface({
  state, onClose, context,
}: { state: ContextMenuState; onClose: () => void; context: CommandContext }) {
  const ref = useRef<HTMLDivElement>(null)
  const [position, setPosition] = useState({ left: state.x, top: state.y })

  // Measure once mounted, then nudge back inside the viewport if we would overflow.
  useLayoutEffect(() => {
    const element = ref.current
    if (!element) return
    const rect = element.getBoundingClientRect()
    setPosition({
      left: state.x + rect.width > window.innerWidth ? Math.max(4, state.x - rect.width) : state.x,
      top: state.y + rect.height > window.innerHeight ? Math.max(4, state.y - rect.height) : state.y,
    })
  }, [state.x, state.y])

  useEffect(() => {
    // Capture phase, deliberately: plenty of things below us stop mousedown from propagating — React
    // Flow's pane, the timeline's clip drag, the buttons in a node header — and a bubble-phase listener
    // on `window` would simply never hear those clicks, leaving the menu stuck open.
    const onMouseDown = (event: MouseEvent) => {
      if (ref.current?.contains(event.target as Node)) return
      onClose()
    }
    // A right-click elsewhere also fires mousedown first, so the handler above closes this menu before
    // the new one opens. No separate contextmenu listener — that one would run *after* React had already
    // opened the replacement and would close it again.
    const onKey = (event: KeyboardEvent) => event.key === 'Escape' && onClose()
    const dismiss = () => onClose()

    window.addEventListener('mousedown', onMouseDown, true)
    window.addEventListener('wheel', dismiss, true)
    window.addEventListener('blur', dismiss)
    window.addEventListener('resize', dismiss)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('mousedown', onMouseDown, true)
      window.removeEventListener('wheel', dismiss, true)
      window.removeEventListener('blur', dismiss)
      window.removeEventListener('resize', dismiss)
      window.removeEventListener('keydown', onKey)
    }
  }, [onClose])

  return (
    <div
      ref={ref}
      data-testid="context-menu"
      className="fixed z-[200] min-w-56 rounded-md border border-[var(--color-edge)] bg-[var(--color-panel)] py-1 text-[13px] shadow-2xl"
      style={{ left: position.left, top: position.top }}
      onMouseDown={(e) => e.stopPropagation()}
      onContextMenu={(e) => e.preventDefault()}
    >
      <Items items={state.items} onClose={onClose} context={context} />
    </div>
  )
}

function Items({
  items, onClose, context,
}: { items: MenuItem[]; onClose: () => void; context: CommandContext }) {
  const [openSub, setOpenSub] = useState<string | null>(null)

  return (
    <>
      {items.map((item, index) => {
        if (item.type === 'separator') {
          return <div key={index} className="my-1 h-px bg-[var(--color-edge)]" />
        }

        if (item.type === 'header') {
          return (
            <div
              key={index}
              className="px-3 py-1 text-[10px] font-semibold uppercase tracking-wide text-[var(--color-ink-dim)]"
            >
              {item.label}
            </div>
          )
        }

        if (item.type === 'submenu') {
          return (
            <div
              key={`${item.label}-${index}`}
              className="relative"
              onMouseEnter={() => setOpenSub(item.label)}
              onMouseLeave={() => setOpenSub(null)}
            >
              <Row
                label={item.label}
                disabled={item.disabled}
                trailing={<span className="text-[var(--color-ink-dim)]">›</span>}
              />
              {openSub === item.label && !item.disabled && (
                <div className="absolute left-full top-0 -mt-1 max-h-96 min-w-56 overflow-y-auto rounded-md border border-[var(--color-edge)] bg-[var(--color-panel)] py-1 shadow-2xl">
                  <Items items={item.items} onClose={onClose} context={context} />
                </div>
              )}
            </div>
          )
        }

        if (item.type === 'command') {
          const command = COMMANDS_BY_ID.get(item.id)
          if (!command) return null
          const enabled = command.enabled ? command.enabled(context) : true
          return (
            <Row
              key={item.id}
              label={command.label}
              shortcut={command.shortcut}
              disabled={!enabled}
              danger={command.danger}
              checked={command.checked?.()}
              onSelect={() => {
                onClose()
                void command.run(context)
              }}
            />
          )
        }

        return (
          <Row
            key={`${item.label}-${index}`}
            label={item.label}
            shortcut={item.shortcut}
            disabled={item.disabled}
            danger={item.danger}
            checked={item.checked}
            onSelect={() => {
              onClose()
              item.onSelect()
            }}
          />
        )
      })}
    </>
  )
}

function Row({
  label, shortcut, disabled, danger, checked, onSelect, trailing,
}: {
  label: string
  shortcut?: string
  disabled?: boolean
  danger?: boolean
  checked?: boolean
  onSelect?: () => void
  trailing?: ReactNode
}) {
  return (
    <button
      disabled={disabled}
      onClick={onSelect}
      className={cx(
        'flex w-full items-center justify-between gap-6 px-3 py-1 text-left',
        disabled
          ? 'cursor-not-allowed text-[var(--color-ink-dim)]/50'
          : danger
            ? 'text-[var(--color-bad)] hover:bg-[var(--color-bad)]/15'
            : 'hover:bg-[var(--color-accent)] hover:text-white',
      )}
    >
      <span className="flex items-center gap-2">
        {checked !== undefined && <span className="w-3 text-[10px]">{checked ? '✓' : ''}</span>}
        {label}
      </span>
      {trailing ?? (shortcut && <span className="text-[11px] opacity-60">{formatShortcut(shortcut)}</span>)}
    </button>
  )
}
