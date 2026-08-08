/**
 * Typed HTTP client.
 *
 * The backend returns structured errors ({code, message, details}) for everything it expects to go wrong,
 * so `ApiError` carries those through to the UI instead of a generic status string.
 */

import type {
  AppSettings, Asset, BackendConfig, BackendStatus, BindableWidget, Clip, GraphReport,
  Link, Project, ProjectSummary, ResolvedTimeline, Run, RunMode, Shot, Step, StepRun,
  Timeline, Track, TrackKind, WorkflowRef,
} from './types'

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string = 'error',
    readonly details?: unknown,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  if (init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }

  let response: Response
  try {
    response = await fetch(path, { ...init, headers })
  } catch (cause) {
    throw new ApiError(
      'Cannot reach the ComfyWebStudio server. Is it running?',
      0,
      'network_error',
      cause,
    )
  }

  if (!response.ok) {
    let payload: any = null
    try {
      payload = await response.json()
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(
      payload?.message ?? payload?.detail ?? `${response.status} ${response.statusText}`,
      response.status,
      payload?.code ?? 'error',
      payload?.details,
    )
  }

  if (response.status === 204) return undefined as T
  const text = await response.text()
  return (text ? JSON.parse(text) : undefined) as T
}

const json = (body: unknown) => JSON.stringify(body)

export const api = {
  health: () => request<{ ok: boolean; version: string; backends: any[] }>('/api/health'),

  // -- projects ------------------------------------------------------------------------------------
  projects: {
    list: () => request<ProjectSummary[]>('/api/projects'),
    get: (id: string) => request<Project>(`/api/projects/${id}`),
    create: (name: string, description = '') =>
      request<Project>('/api/projects', { method: 'POST', body: json({ name, description }) }),
    update: (id: string, patch: Partial<Project>) =>
      request<Project>(`/api/projects/${id}`, { method: 'PATCH', body: json(patch) }),
    remove: (id: string) => request<void>(`/api/projects/${id}`, { method: 'DELETE' }),
    duplicate: (id: string, name?: string) =>
      request<Project>(`/api/projects/${id}/duplicate${name ? `?name=${encodeURIComponent(name)}` : ''}`, {
        method: 'POST',
      }),
    exportUrl: (id: string, opts: { assets?: boolean; renders?: boolean } = {}) =>
      `/api/projects/${id}/export?include_assets=${opts.assets ?? true}&include_renders=${opts.renders ?? false}`,
    import: (file: File, name?: string) => {
      const form = new FormData()
      form.append('file', file)
      return request<Project>(`/api/projects/import${name ? `?name=${encodeURIComponent(name)}` : ''}`, {
        method: 'POST',
        body: form,
      })
    },
  },

  // -- workflows -----------------------------------------------------------------------------------
  workflows: {
    list: (projectId: string) => request<WorkflowRef[]>(`/api/projects/${projectId}/workflows`),
    get: (projectId: string, id: string) =>
      request<WorkflowRef>(`/api/projects/${projectId}/workflows/${id}`),
    graph: (projectId: string, id: string, fmt: 'ui' | 'api' = 'ui') =>
      request<Record<string, unknown>>(`/api/projects/${projectId}/workflows/${id}/graph?fmt=${fmt}`),
    upload: (projectId: string, file: File) => {
      const form = new FormData()
      form.append('file', file)
      return request<WorkflowRef>(`/api/projects/${projectId}/workflows/upload`, {
        method: 'POST',
        body: form,
      })
    },
    remove: (projectId: string, id: string) =>
      request<void>(`/api/projects/${projectId}/workflows/${id}`, { method: 'DELETE' }),
    rediscover: (projectId: string, id: string) =>
      request<WorkflowRef>(`/api/projects/${projectId}/workflows/${id}/rediscover`, { method: 'POST' }),
    bindable: (projectId: string, id: string) =>
      request<BindableWidget[]>(`/api/projects/${projectId}/workflows/${id}/bindable`),
    expose: (projectId: string, id: string, nodeId: string, inputName: string) =>
      request<WorkflowRef>(`/api/projects/${projectId}/workflows/${id}/expose`, {
        method: 'POST',
        body: json({ node_id: nodeId, input_name: inputName }),
      }),
    unexpose: (projectId: string, id: string, key: string) =>
      request<void>(`/api/projects/${projectId}/workflows/${id}/expose/${key}`, { method: 'DELETE' }),
    openInComfy: (projectId: string, id: string) =>
      request<{ url: string; node_pack_installed: boolean; hint: string | null }>(
        `/api/projects/${projectId}/workflows/${id}/open-in-comfy`,
        { method: 'POST' },
      ),
  },

  // -- shots ---------------------------------------------------------------------------------------
  shots: {
    create: (projectId: string, name: string) =>
      request<Shot>(`/api/projects/${projectId}/shots`, { method: 'POST', body: json({ name }) }),
    update: (projectId: string, shotId: string, patch: Partial<Shot>) =>
      request<Shot>(`/api/projects/${projectId}/shots/${shotId}`, {
        method: 'PATCH',
        body: json(patch),
      }),
    remove: (projectId: string, shotId: string) =>
      request<void>(`/api/projects/${projectId}/shots/${shotId}`, { method: 'DELETE' }),
    duplicate: (projectId: string, shotId: string) =>
      request<Shot>(`/api/projects/${projectId}/shots/${shotId}/duplicate`, { method: 'POST' }),
    validate: (projectId: string, shotId: string) =>
      request<GraphReport>(`/api/projects/${projectId}/shots/${shotId}/validate`),
    results: (projectId: string, shotId: string) =>
      request<Record<string, { run_id: string; step_run: StepRun }>>(
        `/api/projects/${projectId}/shots/${shotId}/results`,
      ),
  },

  steps: {
    create: (projectId: string, shotId: string, workflowId: string, uiPos?: { x: number; y: number }) =>
      request<Step>(`/api/projects/${projectId}/shots/${shotId}/steps`, {
        method: 'POST',
        body: json({ workflow_id: workflowId, ui_pos: uiPos }),
      }),
    update: (projectId: string, stepId: string, patch: Partial<Step>) =>
      request<Step>(`/api/projects/${projectId}/steps/${stepId}`, {
        method: 'PATCH',
        body: json(patch),
      }),
    replaceParams: (projectId: string, stepId: string, overrides: Record<string, unknown>) =>
      request<Step>(`/api/projects/${projectId}/steps/${stepId}/params`, {
        method: 'PUT',
        body: json(overrides),
      }),
    remove: (projectId: string, stepId: string) =>
      request<void>(`/api/projects/${projectId}/steps/${stepId}`, { method: 'DELETE' }),
  },

  links: {
    create: (projectId: string, shotId: string, link: Omit<Link, 'id'>) =>
      request<Link>(`/api/projects/${projectId}/shots/${shotId}/links`, {
        method: 'POST',
        body: json(link),
      }),
    remove: (projectId: string, shotId: string, linkId: string) =>
      request<void>(`/api/projects/${projectId}/shots/${shotId}/links/${linkId}`, {
        method: 'DELETE',
      }),
  },

  // -- runs ----------------------------------------------------------------------------------------
  runs: {
    start: (projectId: string, shotId: string, body: { mode?: RunMode; step_ids?: string[]; force?: boolean }) =>
      request<Run>(`/api/projects/${projectId}/shots/${shotId}/run`, {
        method: 'POST',
        body: json(body),
      }),
    list: (projectId: string, shotId?: string) =>
      request<Run[]>(`/api/projects/${projectId}/runs${shotId ? `?shot_id=${shotId}` : ''}`),
    get: (projectId: string, runId: string) => request<Run>(`/api/projects/${projectId}/runs/${runId}`),
    active: (projectId: string) => request<Run[]>(`/api/projects/${projectId}/runs/active`),
    cancel: (projectId: string, runId: string) =>
      request<{ cancelled: boolean; message: string }>(
        `/api/projects/${projectId}/runs/${runId}/cancel`,
        { method: 'POST' },
      ),
    clearCache: (projectId: string) =>
      request<{ ok: boolean }>(`/api/projects/${projectId}/cache/clear`, { method: 'POST' }),
  },

  // -- media ---------------------------------------------------------------------------------------
  media: {
    url: (projectId: string, path: string) =>
      `/api/projects/${projectId}/media?path=${encodeURIComponent(path)}`,
    assets: (projectId: string) => request<Asset[]>(`/api/projects/${projectId}/assets`),
    upload: (projectId: string, file: File) => {
      const form = new FormData()
      form.append('file', file)
      return request<Asset>(`/api/projects/${projectId}/assets`, { method: 'POST', body: form })
    },
    removeAsset: (projectId: string, assetId: string) =>
      request<void>(`/api/projects/${projectId}/assets/${assetId}`, { method: 'DELETE' }),
  },

  // -- timeline ------------------------------------------------------------------------------------
  timeline: {
    get: (projectId: string) => request<Timeline>(`/api/projects/${projectId}/timeline`),
    update: (projectId: string, patch: Partial<Timeline>) =>
      request<Timeline>(`/api/projects/${projectId}/timeline`, { method: 'PATCH', body: json(patch) }),
    resolved: (projectId: string) =>
      request<ResolvedTimeline>(`/api/projects/${projectId}/timeline/resolved`),
    createTrack: (projectId: string, kind: TrackKind, name?: string) =>
      request<Track>(`/api/projects/${projectId}/timeline/tracks`, {
        method: 'POST',
        body: json({ kind, name }),
      }),
    updateTrack: (projectId: string, trackId: string, patch: Partial<Track>) =>
      request<Track>(`/api/projects/${projectId}/timeline/tracks/${trackId}`, {
        method: 'PATCH',
        body: json(patch),
      }),
    removeTrack: (projectId: string, trackId: string) =>
      request<void>(`/api/projects/${projectId}/timeline/tracks/${trackId}`, { method: 'DELETE' }),
    createClip: (projectId: string, trackId: string, body: Partial<Clip>) =>
      request<Clip>(`/api/projects/${projectId}/timeline/tracks/${trackId}/clips`, {
        method: 'POST',
        body: json(body),
      }),
    updateClip: (projectId: string, trackId: string, clipId: string, patch: Partial<Clip>) =>
      request<Clip>(`/api/projects/${projectId}/timeline/tracks/${trackId}/clips/${clipId}`, {
        method: 'PATCH',
        body: json(patch),
      }),
    removeClip: (projectId: string, trackId: string, clipId: string) =>
      request<void>(`/api/projects/${projectId}/timeline/tracks/${trackId}/clips/${clipId}`, {
        method: 'DELETE',
      }),
    fromShots: (projectId: string, shotIds?: string[]) =>
      request<Timeline>(`/api/projects/${projectId}/timeline/from-shots`, {
        method: 'POST',
        body: json(shotIds ?? null),
      }),
    render: (projectId: string, body: { name?: string; still?: boolean; time_s?: number }) =>
      request<{ render_id: string; status: string }>(`/api/projects/${projectId}/timeline/render`, {
        method: 'POST',
        body: json(body),
      }),
    renders: (projectId: string) =>
      request<Array<{ name: string; path: string; size: number; modified: number }>>(
        `/api/projects/${projectId}/timeline/renders`,
      ),
  },

  // -- settings ------------------------------------------------------------------------------------
  settings: {
    get: () => request<AppSettings>('/api/settings'),
    update: (patch: Partial<AppSettings>) =>
      request<AppSettings>('/api/settings', { method: 'PATCH', body: json(patch) }),
    notices: () => request<{ corrupt_settings: string[]; no_backends: boolean; root: string }>(
      '/api/settings/notices',
    ),
    backends: () => request<BackendConfig[]>('/api/settings/backends'),
    addBackend: (body: Partial<BackendConfig>) =>
      request<BackendConfig>('/api/settings/backends', { method: 'POST', body: json(body) }),
    updateBackend: (id: string, body: Partial<BackendConfig>) =>
      request<BackendConfig>(`/api/settings/backends/${id}`, { method: 'PATCH', body: json(body) }),
    removeBackend: (id: string) =>
      request<void>(`/api/settings/backends/${id}`, { method: 'DELETE' }),
    testBackend: (id: string) =>
      request<BackendStatus>(`/api/settings/backends/${id}/test`, { method: 'POST' }),
    installNodepack: (id: string) =>
      request<{ ok: boolean; action: string; path: string; restart_required: boolean; message?: string }>(
        `/api/settings/backends/${id}/install-nodepack`,
        { method: 'POST' },
      ),
  },
}
