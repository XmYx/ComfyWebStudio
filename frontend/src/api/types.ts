/** Mirrors backend/comfywebstudio/core/models.py. Kept hand-written so the shapes stay readable. */

export type PortKind =
  | 'image' | 'mask' | 'video' | 'audio' | 'latent'
  | 'string' | 'int' | 'float' | 'boolean' | 'file'

export type ParamKind = 'string' | 'int' | 'float' | 'boolean' | 'choice'
export type SeedMode = 'fixed' | 'randomize' | 'increment'
export type RunMode = 'step' | 'chain' | 'shot' | 'timeline'
export type TrackKind = 'video' | 'audio' | 'text' | 'overlay'

export type RunStatus =
  | 'pending' | 'queued' | 'running' | 'success'
  | 'error' | 'cancelled' | 'skipped' | 'cached'

export interface PortSpec {
  key: string
  direction: 'in' | 'out'
  kind: PortKind
  node_id: string
  label: string
  group: string
  order: number
  optional: boolean
  meta: Record<string, unknown>
}

export interface ParamSpec {
  key: string
  kind: ParamKind
  label: string
  default: unknown
  min: number | null
  max: number | null
  step: number | null
  choices: string[] | null
  multiline: boolean
  tooltip: string
  group: string
  order: number
  node_id: string
  input_name: string
  source: 'ws_node' | 'raw_widget'
  is_seed: boolean
}

export interface WorkflowRef {
  id: string
  name: string
  hash: string
  ports: PortSpec[]
  params: ParamSpec[]
  last_synced: string | null
  missing_nodes: string[]
  warnings: string[]
}

export interface Vec2 { x: number; y: number }

export interface Step {
  id: string
  name: string
  workflow_id: string
  enabled: boolean
  param_overrides: Record<string, unknown>
  seed_mode: SeedMode | null
  backend_id: string | null
  notes: string
  ui_pos: Vec2
}

export interface Link {
  id: string
  from_step: string
  from_port: string
  to_step: string
  to_port: string
}

export interface Shot {
  id: string
  name: string
  notes: string
  color: string | null
  steps: Step[]
  links: Link[]
}

export interface Artifact {
  id: string
  kind: PortKind
  port_key: string
  path: string
  thumb: string | null
  sha256: string
  meta: Record<string, any>
}

export interface StepRun {
  step_id: string
  status: RunStatus
  prompt_id: string | null
  started: string | null
  finished: string | null
  progress: number
  current_node: string | null
  outputs: Artifact[]
  error: string | null
  error_node: string | null
  cached: boolean
  resolved_params: Record<string, unknown>
  logs: string[]
}

export interface Run {
  id: string
  shot_id: string | null
  mode: RunMode
  status: RunStatus
  started: string
  finished: string | null
  step_runs: StepRun[]
  error: string | null
}

export interface ClipSource {
  kind: 'step_output' | 'asset'
  shot_id: string | null
  step_id: string | null
  port_key: string | null
  asset_id: string | null
}

export interface Clip {
  id: string
  name: string
  source: ClipSource
  start: number
  duration: number
  in_point: number
  out_point: number | null
  transform: { scale: number; offset_x: number; offset_y: number; rotation: number; fit: string }
  transition_in: { kind: string; duration: number }
  transition_out: { kind: string; duration: number }
  opacity: number
  volume: number
  text: string
  text_style: Record<string, unknown>
  enabled: boolean
}

export interface Track {
  id: string
  kind: TrackKind
  name: string
  muted: boolean
  locked: boolean
  clips: Clip[]
}

export interface Timeline {
  fps: number
  width: number
  height: number
  background: string
  tracks: Track[]
  /** Server-computed from the clips; read-only (ignored on PATCH). */
  duration: number
}

export interface Asset {
  id: string
  name: string
  kind: PortKind
  path: string
  thumb: string | null
  meta: Record<string, any>
}

export interface ProjectSettings {
  fps: number
  width: number
  height: number
  backend_id: string | null
}

export interface Project {
  schema_version: number
  id: string
  name: string
  description: string
  created: string
  modified: string
  settings: ProjectSettings
  workflows: Record<string, WorkflowRef>
  shots: Shot[]
  timeline: Timeline
  assets: Record<string, Asset>
}

export interface ProjectSummary {
  id: string
  name: string
  description: string
  modified: string
  shot_count: number
  workflow_count: number
}

export interface GraphIssue {
  level: 'error' | 'warning'
  message: string
  step_id: string | null
  link_id: string | null
  port_key: string | null
}

export interface GraphReport {
  ok: boolean
  order: string[]
  issues: GraphIssue[]
}

export interface BackendConfig {
  id: string
  name: string
  kind: 'local' | 'remote'
  base_url: string
  headers: Record<string, string>
  comfy_root: string | null
  comfy_user: string
  enabled: boolean
  timeout_s: number
}

export interface BackendStatus {
  id: string
  name: string
  reachable: boolean
  error: string | null
  comfyui_version: string | null
  devices: Array<{ name: string; vram_total?: number; vram_free?: number }> | null
  node_pack: { pack_version: string; protocol: number } | null
  protocol_ok: boolean
  shared_filesystem: boolean
}

export interface AppSettings {
  root: string
  projects_dir: string
  host: string
  port: number
  cors_origins: string[]
  backends: BackendConfig[]
  default_backend_id: string | null
  execution: {
    max_concurrent_steps: number
    enable_cache: boolean
    step_timeout_s: number | null
    retry_on_error: number
    default_seed_mode: SeedMode
  }
  render: {
    fps: number; width: number; height: number
    container: string; video_codec: string; crf: number; pix_fmt: string
    audio_codec: string; audio_bitrate: string; audio_sample_rate: number
  }
  preview: {
    thumbnail_size: number
    thumbnail_format: string
    thumbnail_quality: number
    autoplay_video: boolean
  }
  ui: { theme: string; timeline_snap: boolean; timeline_snap_frames: number }
}

export interface BindableWidget {
  node_id: string
  class_type: string
  title: string
  input_name: string
  kind: ParamKind
  current: unknown
  key: string
  choices: string[] | null
  min: number | null
  max: number | null
  step: number | null
  multiline: boolean
  exposed: boolean
}

export interface StudioEvent {
  type: string
  project_id: string | null
  run_id: string | null
  step_id: string | null
  data: Record<string, any>
  ts: number
}

export interface ResolvedTimeline {
  duration: number
  clips: Array<{
    track_id: string
    clip_id: string
    error: string | null
    kind: string | null
    artifacts: Artifact[]
  }>
}

export interface PluginInfo {
  id: string
  name: string
  version: string
  author: string
  description: string
  created: string
  enabled: boolean
  workflows: Array<{ id: string; name: string; has_ui_graph: boolean; ports: PortSpec[] }>
  shot_templates: Array<{ name: string; steps: unknown[]; links: unknown[] }>
}
