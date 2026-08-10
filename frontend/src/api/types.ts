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

/** What a render covers. `clips` is a batch — one file per clip. */
export type RenderScope = 'timeline' | 'range' | 'clip' | 'clips'

export interface RenderRequest {
  name?: string
  /** A single frame instead of a movie, taken at `time_s`. */
  still?: boolean
  time_s?: number
  scope?: RenderScope
  start_s?: number
  end_s?: number
  clip_id?: string
  /** Output overrides, applied for this render only. Omitted fields keep the project's settings. */
  fps?: number
  width?: number
  height?: number
  container?: string
  video_codec?: string
  crf?: number
}

export interface Vec2 { x: number; y: number }
/** Canvas size of a node. Zero means "size to content". */
export interface Size { w: number; h: number }

export interface Step {
  id: string
  name: string
  workflow_id: string
  enabled: boolean
  param_overrides: Record<string, unknown>
  /** Parameter keys pinned to the canvas node, in the order they appear there. */
  exposed_params: string[]
  seed_mode: SeedMode | null
  backend_id: string | null
  notes: string
  ui_pos: Vec2
  ui_size: Size
}

/** What a value node holds. `media` points at an imported asset; the rest are literals. */
export type ValueNodeKind = 'string' | 'int' | 'float' | 'boolean' | 'media' | 'shot'

/** The single output port every value node has. Mirrors VALUE_PORT in core/models.py. */
export const VALUE_PORT = 'value'

/** A constant on the shot canvas, feeding one or more step inputs. Never runs. */
export interface ValueNode {
  id: string
  name: string
  kind: ValueNodeKind
  value: unknown
  asset_id: string | null
  /** For a `shot` node: whose output to take, and which port of it. */
  source_shot_id: string | null
  source_port: string | null
  /** What an empty source node offers, so it can be wired before its source is chosen. */
  media_kind: PortKind
  ui_pos: Vec2
  ui_size: Size
}

/** A shot template placed on a canvas as one contained node. */
export interface TemplateInstance {
  id: string
  template_id: string
  name: string
  enabled: boolean
  /** Values for the template's promoted controls, by promoted key. */
  param_overrides: Record<string, unknown>
  workflow_map: Record<string, string>
  template_revision: number
  ui_pos: Vec2
  ui_size: Size
}

/** One port the container node exposes, and the inner port it stands for. */
export interface TemplatePort {
  key: string
  direction: 'in' | 'out'
  kind: PortKind
  inner_key: string
  inner_port: string
  label: string
  optional: boolean
  shown: boolean
}

/** One parameter the container node exposes. */
export interface TemplateControl {
  key: string
  inner_key: string
  inner_param: string
  label: string
  spec: ParamSpec | null
  shown: boolean
}

export interface TemplateSummary {
  id: string
  name: string
  description: string
  revision: number
  modified: string
  source_project: string
  step_count: number
  input_count: number
  output_count: number
  control_count: number
}

/**
 * The whole template, as `GET /api/templates/{id}` returns it.
 *
 * Deliberately not an extension of TemplateSummary: the counts on a summary are derived for the list and
 * are absent here, where the real collections are present instead.
 */
export interface ShotTemplate {
  id: string
  name: string
  description: string
  revision: number
  created: string
  modified: string
  source_project: string
  workflows: Array<{ key: string; name: string; ports: PortSpec[]; params: ParamSpec[] }>
  steps: Array<{ key: string; name: string; workflow_key: string; ui_pos: Vec2 }>
  nodes: Array<{ key: string; name: string; kind: ValueNodeKind; value: unknown; ui_pos: Vec2 }>
  /** Wiring inside the template, by key. */
  links: Array<{ from_key: string; from_port: string; to_key: string; to_port: string }>
  ports: TemplatePort[]
  controls: TemplateControl[]
}

/** What the canvas needs to draw one placed instance: its surface, and whether it is current. */
export interface PlacedTemplate {
  instance_id: string
  template_id: string
  missing: boolean
  stale?: boolean
  summary?: TemplateSummary
  ports?: TemplatePort[]
  controls?: TemplateControl[]
}

export interface Link {
  id: string
  /** A step id, a value node id, or a placed template's id — all three are ends on the canvas. */
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
  /** Set when this shot is an open editing session for that template rather than a shot of its own. */
  template_edit_id: string | null
  steps: Step[]
  nodes: ValueNode[]
  instances: TemplateInstance[]
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

/** What produces a generated asset. Absent on imported media. */
export interface AssetSource {
  shot_id: string
  step_id: string
  port_key: string
}

export interface Asset {
  id: string
  name: string
  kind: PortKind
  path: string
  thumb: string | null
  source?: AssetSource | null
  generated?: string | null
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

export interface VersionChange {
  scope: string
  target_id: string
  target_name: string
  action: string
  summary: string
  detail: Record<string, any>
}

export interface Version {
  id: string
  ts: string
  snapshot: string
  label: string | null
  summary: string
  scopes: string[]
  targets: string[]
  changes: VersionChange[]
}
