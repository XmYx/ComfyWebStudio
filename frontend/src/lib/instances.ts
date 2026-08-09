/**
 * Joining a placed template's promoted ports back to what its inner steps produced.
 *
 * A run reports against expanded steps — ids like `inst_x:consume` — while the node the user is looking
 * at shows promoted port names like `final`. Without this join a template that ran perfectly well shows
 * no output at all, which is exactly how it looked before.
 *
 * Kept as a plain function rather than living in a component: both the canvas node and the inspector
 * need it, and neither should have to import the other.
 */

import type { Artifact, TemplateInstance, TemplatePort } from '@/api/types'
import type { LiveStep } from '@/store/studio'

/** Mirrors KEY_SEPARATOR in backend/comfywebstudio/core/templates.py. */
export const INSTANCE_KEY_SEPARATOR = ':'

/** The expanded id of one key inside a placed template. */
export function innerStepId(instanceId: string, key: string): string {
  return `${instanceId}${INSTANCE_KEY_SEPARATOR}${key}`
}

/** True when this step id belongs to that instance. */
export function belongsToInstance(stepId: string, instanceId: string): boolean {
  return stepId.startsWith(`${instanceId}${INSTANCE_KEY_SEPARATOR}`)
}

export interface InstanceOutput {
  port: TemplatePort
  artifact: Artifact | null
}

/** Each promoted output port with whatever the inner step behind it produced, or null. */
export function outputsForInstance(
  instance: TemplateInstance,
  ports: TemplatePort[] | undefined,
  liveSteps: Record<string, LiveStep>,
): InstanceOutput[] {
  return (ports ?? [])
    .filter((port) => port.direction === 'out')
    .map((port) => ({
      port,
      artifact:
        liveSteps[innerStepId(instance.id, port.inner_key)]?.outputs?.find(
          (a) => a.port_key === port.inner_port,
        ) ?? null,
    }))
}
