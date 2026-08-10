/** Small design-system primitives, so feature code stays about behaviour rather than class strings. */

import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'

export function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ')
}

// -- button ---------------------------------------------------------------------------------------------

type ButtonProps = {
  children: ReactNode
  /** The event is passed through, so a handler can read modifiers such as shift. */
  onClick?: (event: React.MouseEvent<HTMLButtonElement>) => void
  variant?: 'primary' | 'default' | 'ghost' | 'danger'
  size?: 'sm' | 'md'
  disabled?: boolean
  title?: string
  className?: string
  type?: 'button' | 'submit'
}

const BUTTON_VARIANTS = {
  primary: 'bg-[var(--color-accent)] text-white hover:brightness-110',
  default: 'bg-[var(--color-panel-2)] text-[var(--color-ink)] hover:bg-[#242b36]',
  ghost: 'bg-transparent text-[var(--color-ink-dim)] hover:bg-[var(--color-panel-2)] hover:text-[var(--color-ink)]',
  danger: 'bg-[var(--color-bad)]/15 text-[var(--color-bad)] hover:bg-[var(--color-bad)]/25',
}

export function Button({
  children, onClick, variant = 'default', size = 'md', disabled, title, className, type = 'button',
}: ButtonProps) {
  return (
    <button
      type={type}
      title={title}
      disabled={disabled}
      onClick={onClick}
      className={cx(
        'inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-md font-medium transition-colors',
        'disabled:opacity-40 disabled:cursor-not-allowed',
        size === 'sm' ? 'px-2 py-1 text-xs' : 'px-3 py-1.5 text-sm',
        BUTTON_VARIANTS[variant],
        className,
      )}
    >
      {children}
    </button>
  )
}

// -- panels ---------------------------------------------------------------------------------------------

export function Panel({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={cx('rounded-lg border border-[var(--color-edge)] bg-[var(--color-panel)]', className)}>
      {children}
    </div>
  )
}

export function PanelHeader({ children, actions }: { children: ReactNode; actions?: ReactNode }) {
  return (
    <div className="flex items-center justify-between border-b border-[var(--color-edge)] px-3 py-2">
      <div className="text-xs font-semibold uppercase tracking-wide text-[var(--color-ink-dim)]">
        {children}
      </div>
      {actions && <div className="flex items-center gap-1">{actions}</div>}
    </div>
  )
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="block">
      <div className="mb-1 flex items-baseline justify-between gap-2">
        <span className="text-xs font-medium text-[var(--color-ink-dim)]">{label}</span>
        {hint && <span className="text-[10px] text-[var(--color-ink-dim)]/70">{hint}</span>}
      </div>
      {children}
    </label>
  )
}

const INPUT_CLASS =
  'w-full rounded-md border border-[var(--color-edge)] bg-[var(--color-surface)] px-2 py-1.5 text-sm ' +
  'text-[var(--color-ink)] outline-none focus:border-[var(--color-accent)] disabled:opacity-50'

export function TextInput(props: React.InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={cx(INPUT_CLASS, props.className)} />
}

export function TextArea(props: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea {...props} className={cx(INPUT_CLASS, 'min-h-20 resize-y font-mono text-xs', props.className)} />
}

export function Select(props: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return <select {...props} className={cx(INPUT_CLASS, props.className)} />
}

export function Checkbox({
  checked, onChange, label,
}: { checked: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <label className="flex cursor-pointer items-center gap-2 text-sm">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="size-4 accent-[var(--color-accent)]"
      />
      <span>{label}</span>
    </label>
  )
}

// -- feedback -------------------------------------------------------------------------------------------

const BADGE_TONES = {
  ok: 'bg-[var(--color-ok)]/15 text-[var(--color-ok)]',
  warn: 'bg-[var(--color-warn)]/15 text-[var(--color-warn)]',
  bad: 'bg-[var(--color-bad)]/15 text-[var(--color-bad)]',
  info: 'bg-[var(--color-accent)]/15 text-[var(--color-accent)]',
  muted: 'bg-[var(--color-panel-2)] text-[var(--color-ink-dim)]',
}

export function Badge({
  children, tone = 'muted', title,
}: { children: ReactNode; tone?: keyof typeof BADGE_TONES; title?: string }) {
  return (
    <span
      title={title}
      className={cx('inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium', BADGE_TONES[tone])}
    >
      {children}
    </span>
  )
}

export function Callout({
  tone = 'warn', title, children,
}: { tone?: 'warn' | 'bad' | 'info'; title?: string; children: ReactNode }) {
  const color = tone === 'bad' ? 'var(--color-bad)' : tone === 'info' ? 'var(--color-accent)' : 'var(--color-warn)'
  return (
    <div
      className="rounded-md border px-3 py-2 text-xs"
      style={{ borderColor: `${color}55`, background: `${color}12`, color }}
    >
      {title && <div className="mb-0.5 font-semibold">{title}</div>}
      <div className="text-[var(--color-ink)]/85">{children}</div>
    </div>
  )
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 p-8 text-center">
      <div className="text-sm font-medium text-[var(--color-ink-dim)]">{title}</div>
      {children && <div className="max-w-md text-xs text-[var(--color-ink-dim)]/70">{children}</div>}
    </div>
  )
}

export function Spinner({ className }: { className?: string }) {
  return (
    <span
      className={cx(
        'inline-block size-3 animate-spin rounded-full border-2 border-current border-t-transparent',
        className,
      )}
    />
  )
}

export function ProgressBar({ value, tone = 'accent' }: { value: number; tone?: 'accent' | 'ok' | 'bad' }) {
  const color = tone === 'ok' ? 'var(--color-ok)' : tone === 'bad' ? 'var(--color-bad)' : 'var(--color-accent)'
  return (
    <div className="h-1 w-full overflow-hidden rounded-full bg-[var(--color-edge)]">
      <div
        className="h-full rounded-full transition-all duration-200"
        style={{ width: `${Math.min(100, Math.max(0, value * 100))}%`, background: color }}
      />
    </div>
  )
}

// -- modal ----------------------------------------------------------------------------------------------

export function Modal({
  open, onClose, title, children, width = 'max-w-lg',
}: { open: boolean; onClose: () => void; title: string; children: ReactNode; width?: string }) {
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 p-8" onClick={onClose}>
      <div
        className={cx('mt-12 w-full rounded-xl border border-[var(--color-edge)] bg-[var(--color-panel)] shadow-2xl', width)}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-[var(--color-edge)] px-4 py-3">
          <h2 className="text-sm font-semibold">{title}</h2>
          <Button variant="ghost" size="sm" onClick={onClose}>✕</Button>
        </div>
        <div className="max-h-[70vh] overflow-y-auto p-4">{children}</div>
      </div>
    </div>
  )
}

// -- toasts ---------------------------------------------------------------------------------------------

type Toast = { id: number; tone: 'ok' | 'bad' | 'info'; message: string }
type ToastApi = { push: (tone: Toast['tone'], message: string) => void }

const ToastContext = createContext<ToastApi>({ push: () => {} })
export const useToast = () => useContext(ToastContext)

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])

  const push = (tone: Toast['tone'], message: string) => {
    const id = Date.now() + Math.random()
    setToasts((current) => [...current, { id, tone, message }])
    // Errors stay longer: they usually contain something the user has to act on.
    window.setTimeout(() => setToasts((c) => c.filter((t) => t.id !== id)), tone === 'bad' ? 9000 : 4000)
  }

  return (
    <ToastContext.Provider value={{ push }}>
      {children}
      <div className="pointer-events-none fixed bottom-4 right-4 z-[100] flex w-96 flex-col gap-2">
        {toasts.map((toast) => (
          <div
            key={toast.id}
            className={cx(
              'pointer-events-auto rounded-lg border px-3 py-2 text-xs shadow-xl backdrop-blur',
              toast.tone === 'bad' && 'border-[var(--color-bad)]/50 bg-[var(--color-bad)]/15 text-[var(--color-bad)]',
              toast.tone === 'ok' && 'border-[var(--color-ok)]/50 bg-[var(--color-ok)]/15 text-[var(--color-ok)]',
              toast.tone === 'info' && 'border-[var(--color-edge)] bg-[var(--color-panel)] text-[var(--color-ink)]',
            )}
            onClick={() => setToasts((c) => c.filter((t) => t.id !== toast.id))}
          >
            {toast.message}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}
