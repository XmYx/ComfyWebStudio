import { useCallback, useEffect, useRef, useState } from 'react'

import { cx } from '@/components/ui'
import {
  COMMANDS, COMMANDS_BY_ID, MENUS, formatShortcut, matchesShortcut,
  type CommandContext, type MenuEntry,
} from './commands'
import { useCommandContext } from './useCommandContext'

/**
 * The application menu bar.
 *
 * Everything it shows comes from the command registry, so a menu item and its keyboard shortcut can never
 * disagree about whether they are available. Shortcuts are bound here too, from the same list.
 */
export function MenuBar() {
  const [openMenu, setOpenMenu] = useState<string | null>(null)
  const barRef = useRef<HTMLDivElement>(null)
  const context = useCommandContext()
  const contextRef = useRef(context)
  contextRef.current = context

  const invoke = useCallback((commandId: string) => {
    const command = COMMANDS_BY_ID.get(commandId)
    if (!command) return
    const ctx = contextRef.current
    if (command.enabled && !command.enabled(ctx)) return
    setOpenMenu(null)
    void command.run(ctx)
  }, [])

  // -- keyboard shortcuts --------------------------------------------------------------------------

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      const typing =
        target &&
        (target.tagName === 'INPUT' ||
          target.tagName === 'TEXTAREA' ||
          target.tagName === 'SELECT' ||
          target.isContentEditable)

      for (const command of COMMANDS) {
        if (!command.shortcut || !matchesShortcut(event, command.shortcut)) continue
        // Plain keys like Delete must not fire while the user is typing; chorded ones are safe.
        if (typing && !command.shortcut.includes('Mod')) return
        // Let the browser handle text editing shortcuts inside a field.
        if (typing && ['Mod+C', 'Mod+X', 'Mod+V', 'Mod+A'].includes(command.shortcut)) return

        event.preventDefault()
        invoke(command.id)
        return
      }
    }

    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [invoke])

  // -- click-away and Escape ------------------------------------------------------------------------

  useEffect(() => {
    if (!openMenu) return
    // Capture phase: the canvas and the timeline stop mousedown from propagating, so a bubble-phase
    // listener here would never see a click on them and the dropdown would stay open.
    const onMouseDown = (event: MouseEvent) => {
      if (!barRef.current?.contains(event.target as Node)) setOpenMenu(null)
    }
    const onKey = (event: KeyboardEvent) => event.key === 'Escape' && setOpenMenu(null)
    window.addEventListener('mousedown', onMouseDown, true)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('mousedown', onMouseDown, true)
      window.removeEventListener('keydown', onKey)
    }
  }, [openMenu])

  return (
    <div
      ref={barRef}
      className="flex h-7 shrink-0 items-stretch border-b border-[var(--color-edge)] bg-[var(--color-panel-2)] px-1 text-[13px]"
    >
      {MENUS.map((menu) => (
        <div key={menu.id} className="relative">
          <button
            onClick={() => setOpenMenu(openMenu === menu.id ? null : menu.id)}
            // Once a menu is open, hovering another opens it, which is how desktop menus behave.
            onMouseEnter={() => openMenu && setOpenMenu(menu.id)}
            className={cx(
              'h-full px-2.5 transition-colors',
              openMenu === menu.id
                ? 'bg-[var(--color-accent)] text-white'
                : 'text-[var(--color-ink)] hover:bg-[var(--color-panel)]',
            )}
          >
            {menu.label}
          </button>

          {openMenu === menu.id && (
            <MenuDropdown items={menu.items} context={context} onInvoke={invoke} />
          )}
        </div>
      ))}
    </div>
  )
}

function MenuDropdown({
  items, context, onInvoke, nested = false,
}: {
  items: MenuEntry[]
  context: CommandContext
  onInvoke: (id: string) => void
  nested?: boolean
}) {
  const [openSub, setOpenSub] = useState<string | null>(null)

  return (
    <div
      className={cx(
        'absolute z-50 min-w-60 rounded-md border border-[var(--color-edge)] bg-[var(--color-panel)] py-1 shadow-2xl',
        nested ? 'left-full top-0 -mt-1' : 'left-0 top-full',
      )}
    >
      {items.map((item, index) => {
        if (item.type === 'separator') {
          return <div key={index} className="my-1 h-px bg-[var(--color-edge)]" />
        }

        if (item.type === 'submenu') {
          return (
            <div
              key={item.label}
              className="relative"
              onMouseEnter={() => setOpenSub(item.label)}
              onMouseLeave={() => setOpenSub(null)}
            >
              <div className="flex cursor-default items-center justify-between px-3 py-1 hover:bg-[var(--color-panel-2)]">
                <span>{item.label}</span>
                <span className="text-[var(--color-ink-dim)]">›</span>
              </div>
              {openSub === item.label && (
                <MenuDropdown items={item.items} context={context} onInvoke={onInvoke} nested />
              )}
            </div>
          )
        }

        const command = COMMANDS_BY_ID.get(item.id)
        if (!command) return null
        const enabled = command.enabled ? command.enabled(context) : true
        const checked = command.checked?.() ?? false

        return (
          <button
            key={item.id}
            disabled={!enabled}
            onClick={() => onInvoke(item.id)}
            className={cx(
              'flex w-full items-center justify-between gap-6 px-3 py-1 text-left',
              enabled
                ? command.danger
                  ? 'text-[var(--color-bad)] hover:bg-[var(--color-bad)]/15'
                  : 'hover:bg-[var(--color-accent)] hover:text-white'
                : 'cursor-not-allowed text-[var(--color-ink-dim)]/50',
            )}
          >
            <span className="flex items-center gap-2">
              <span className="w-3 text-[10px]">{checked ? '✓' : ''}</span>
              {command.label}
            </span>
            {command.shortcut && (
              <span className="text-[11px] opacity-60">{formatShortcut(command.shortcut)}</span>
            )}
          </button>
        )
      })}
    </div>
  )
}
