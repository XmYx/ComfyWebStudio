/**
 * Looking inside a placed template.
 *
 * Double-clicking a container node opens this, the way double-clicking a subgraph opens it in ComfyUI.
 * It shows the template's own graph — its steps, its value nodes, and the wiring between them — laid out
 * where its author left them.
 *
 * Read-only, deliberately. A template is shared by every shot that placed it, so editing one from inside
 * a single instance would silently change everyone else's; the way to change a template is to build the
 * shot you want and save over it. What you *can* do from here is open any inner step's workflow in
 * ComfyUI, which is usually why someone looks inside in the first place.
 */

import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Background, BackgroundVariant, Controls, Handle, Position, ReactFlow, ReactFlowProvider,
  type Edge, type Node, type NodeProps,
} from '@xyflow/react'

import { api, ApiError } from '@/api/client'
import type { PortSpec, TemplateInstance } from '@/api/types'
import { KIND_COLOR } from '@/lib/kinds'
import { useStudio } from '@/store/studio'
import { Badge, Button, Empty, Panel, Spinner, cx, useToast } from '@/components/ui'

interface Props {
  projectId: string
  shotName: string
  instance: TemplateInstance
}

interface InnerNodeData extends Record<string, unknown> {
  title: string
  subtitle: string
  inputs: PortSpec[]
  outputs: PortSpec[]
  /** Set for a step whose workflow this project has a copy of, so it can be opened in ComfyUI. */
  workflowId: string | null
  onOpen: (workflowId: string) => void
}

const NODE_TYPES = { inner: InnerNode }

export function TemplateContents(props: Props) {
  return (
    <ReactFlowProvider>
      <Contents {...props} />
    </ReactFlowProvider>
  )
}

function Contents({ projectId, shotName, instance }: Props) {
  const toast = useToast()
  const openInstance = useStudio((s) => s.openInstance)

  const { data: template, isLoading, error } = useQuery({
    queryKey: ['template', instance.template_id],
    queryFn: () => api.templates.get(instance.template_id),
  })

  const openInComfy = async (workflowId: string) => {
    try {
      const result = await api.workflows.openInComfy(projectId, workflowId)
      if (result.hint) toast.push('bad', result.hint)
      window.open(result.url, '_blank', 'noopener')
    } catch (err) {
      toast.push('bad', (err as ApiError).message)
    }
  }

  const nodes = useMemo<Node<InnerNodeData>[]>(() => {
    if (!template) return []

    const steps = template.steps.map((step) => {
      const workflow = template.workflows.find((w) => w.key === step.workflow_key)
      return {
        id: step.key,
        type: 'inner',
        position: { x: step.ui_pos?.x ?? 0, y: step.ui_pos?.y ?? 0 },
        data: {
          title: step.name,
          subtitle: workflow?.name ?? step.workflow_key,
          inputs: (workflow?.ports ?? []).filter((p) => p.direction === 'in'),
          outputs: (workflow?.ports ?? []).filter((p) => p.direction === 'out'),
          workflowId: instance.workflow_map[step.workflow_key] ?? null,
          onOpen: openInComfy,
        },
      } satisfies Node<InnerNodeData>
    })

    const values = template.nodes.map((node) => ({
      id: node.key,
      type: 'inner',
      position: { x: node.ui_pos?.x ?? 0, y: node.ui_pos?.y ?? 0 },
      data: {
        title: node.name || node.key,
        subtitle: `${node.kind} value`,
        inputs: [],
        // A value node has one output, and it is always called `value`.
        outputs: [
          { key: 'value', direction: 'out', kind: node.kind === 'media' ? 'image' : node.kind,
            node_id: '', label: '', group: '', order: 0, optional: false, meta: {} } as PortSpec,
        ],
        workflowId: null,
        onOpen: openInComfy,
      },
    } satisfies Node<InnerNodeData>))

    return [...steps, ...values]
  }, [template, instance.workflow_map])  // eslint-disable-line react-hooks/exhaustive-deps

  const edges = useMemo<Edge[]>(
    () =>
      (template?.links ?? []).map((link, index) => ({
        id: `${index}`,
        source: link.from_key,
        sourceHandle: link.from_port,
        target: link.to_key,
        targetHandle: link.to_port,
        style: { strokeWidth: 2 },
      })),
    [template],
  )

  const title = instance.name || template?.name || 'Template'

  return (
    <Panel className="flex min-h-0 flex-col overflow-hidden">
      <div className="flex shrink-0 items-center gap-2 border-b border-[var(--color-edge)] px-2 py-1.5">
        <Button size="sm" variant="ghost" onClick={() => openInstance(null)}>
          ‹ {shotName}
        </Button>
        <span className="text-xs font-semibold">{title}</span>
        {template && (
          <span className="text-[10px] text-[var(--color-ink-dim)]">
            revision {template.revision} · read-only
          </span>
        )}
        <div className="flex-1" />
        <Badge tone="muted">inside a template</Badge>
      </div>

      <div className="min-h-0 flex-1">
        {isLoading ? (
          <Empty title="Loading the template…"><Spinner /></Empty>
        ) : error || !template ? (
          <Empty title="This template is no longer in the library">
            The node is still here, but there is nothing to look inside. Save a shot over it to restore it.
          </Empty>
        ) : (
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={NODE_TYPES}
            nodesDraggable={false}
            nodesConnectable={false}
            elementsSelectable={false}
            fitView
            fitViewOptions={{ padding: 0.25, maxZoom: 1 }}
            proOptions={{ hideAttribution: true }}
            minZoom={0.2}
            maxZoom={1.8}
          >
            <Background variant={BackgroundVariant.Dots} gap={18} size={1} color="#232a35" />
            <Controls showInteractive={false} />
          </ReactFlow>
        )}
      </div>
    </Panel>
  )
}

function InnerNode({ data }: NodeProps) {
  const { title, subtitle, inputs, outputs, workflowId, onOpen } = data as InnerNodeData
  return (
    <div className="w-56 overflow-hidden rounded-lg border border-[var(--color-edge)] bg-[var(--color-panel)] shadow-lg">
      <div className="flex items-center gap-1.5 border-b border-[var(--color-edge)] px-2.5 py-1.5">
        <div className="min-w-0 flex-1">
          <div className="truncate text-xs font-semibold">{title}</div>
          <div className="truncate text-[10px] text-[var(--color-ink-dim)]">{subtitle}</div>
        </div>
        {workflowId && (
          <button
            title="Open this step's workflow in ComfyUI"
            onClick={(e) => { e.stopPropagation(); onOpen(workflowId) }}
            className="rounded px-1 text-[10px] text-[var(--color-ink-dim)] hover:bg-[var(--color-panel-2)] hover:text-[var(--color-accent)]"
          >
            ↗
          </button>
        )}
      </div>
      <div className="grid grid-cols-2 gap-x-2 py-2 text-[10px]">
        <div className="space-y-1.5">
          {inputs.map((port) => <InnerPort key={port.key} port={port} side="left" />)}
        </div>
        <div className="space-y-1.5 text-right">
          {outputs.map((port) => <InnerPort key={port.key} port={port} side="right" />)}
        </div>
      </div>
    </div>
  )
}

function InnerPort({ port, side }: { port: PortSpec; side: 'left' | 'right' }) {
  const color = KIND_COLOR[port.kind] ?? 'var(--kind-file)'
  return (
    <div className={cx('relative flex items-center gap-1 px-2.5', side === 'right' && 'justify-end')}>
      <Handle
        id={port.key}
        type={side === 'left' ? 'target' : 'source'}
        position={side === 'left' ? Position.Left : Position.Right}
        isConnectable={false}
        style={{ background: color, top: 'auto', transform: 'none' }}
        className="!relative !size-2.5"
      />
      <span
        className="truncate text-[var(--color-ink-dim)]"
        title={`${port.label || port.key} (${port.kind})`}
        style={{ order: side === 'left' ? 1 : -1 }}
      >
        {port.label || port.key}
      </span>
    </div>
  )
}
