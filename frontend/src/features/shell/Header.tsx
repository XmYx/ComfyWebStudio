import { Link, useLocation, useMatch } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'

import { api } from '@/api/client'
import { useStreamStatus } from '@/api/events'
import { useStudio } from '@/store/studio'
import { Badge, ProgressBar, cx } from '@/components/ui'

export function Header() {
  // The header renders outside <Routes>, so useParams() would be empty here — match the path instead.
  const match = useMatch('/p/:projectId/*')
  const projectId = match?.params.projectId
  const location = useLocation()
  const streamStatus = useStreamStatus()
  const activeRun = useStudio((s) => s.activeRun)
  const renderProgress = useStudio((s) => s.renderProgress)

  const { data: project } = useQuery({
    queryKey: ['project', projectId],
    queryFn: () => api.projects.get(projectId!),
    enabled: Boolean(projectId),
  })

  const tabs = projectId
    ? [
        { to: `/p/${projectId}/storyboard`, label: 'Storyboard' },
        { to: `/p/${projectId}/shots`, label: 'Shots' },
        { to: `/p/${projectId}/timeline`, label: 'Timeline' },
      ]
    : []

  return (
    <header className="flex h-12 shrink-0 items-center gap-4 border-b border-[var(--color-edge)] bg-[var(--color-panel)] px-3">
      <Link to="/projects" className="flex items-center gap-2 text-sm font-semibold">
        <span className="grid size-6 place-items-center rounded bg-[var(--color-accent)] text-[11px] text-white">
          CW
        </span>
        ComfyWebStudio
      </Link>

      {project && (
        <>
          <span className="text-[var(--color-ink-dim)]">/</span>
          <span className="max-w-56 truncate text-sm" title={project.name}>{project.name}</span>
        </>
      )}

      <nav className="flex items-center gap-1">
        {tabs.map((tab) => (
          <Link
            key={tab.to}
            to={tab.to}
            className={cx(
              'rounded-md px-2.5 py-1 text-sm transition-colors',
              location.pathname === tab.to
                ? 'bg-[var(--color-panel-2)] text-[var(--color-ink)]'
                : 'text-[var(--color-ink-dim)] hover:text-[var(--color-ink)]',
            )}
          >
            {tab.label}
          </Link>
        ))}
      </nav>

      <div className="flex-1" />

      {renderProgress && (
        <div className="w-56">
          <div className="mb-1 flex justify-between text-[11px] text-[var(--color-ink-dim)]">
            <span>Rendering</span>
            <span>{renderProgress.message}</span>
          </div>
          <ProgressBar value={renderProgress.progress} />
        </div>
      )}

      {activeRun && activeRun.status === 'running' && (
        <Badge tone="info">Running…</Badge>
      )}

      <Badge
        tone={streamStatus === 'connected' ? 'ok' : 'bad'}
        title={
          streamStatus === 'connected'
            ? 'Live updates connected'
            : 'Not receiving live updates — is the backend running?'
        }
      >
        <span className="size-1.5 rounded-full bg-current" />
        {streamStatus === 'connected' ? 'live' : 'offline'}
      </Badge>

      <Link
        to="/settings"
        className={cx(
          'rounded-md px-2.5 py-1 text-sm transition-colors',
          location.pathname === '/settings'
            ? 'bg-[var(--color-panel-2)] text-[var(--color-ink)]'
            : 'text-[var(--color-ink-dim)] hover:text-[var(--color-ink)]',
        )}
      >
        Settings
      </Link>
    </header>
  )
}
