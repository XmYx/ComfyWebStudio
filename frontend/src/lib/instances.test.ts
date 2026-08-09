import { describe, expect, it } from 'vitest'

import type { Artifact, TemplateInstance, TemplatePort } from '@/api/types'
import type { LiveStep } from '@/store/studio'
import { belongsToInstance, innerStepId, outputsForInstance } from './instances'

const instance = { id: 'inst_1' } as TemplateInstance

function port(key: string, innerKey: string, innerPort: string, direction: 'in' | 'out' = 'out') {
  return {
    key, direction, kind: 'image', inner_key: innerKey, inner_port: innerPort,
    label: '', optional: false, shown: true,
  } as TemplatePort
}

function artifact(portKey: string): Artifact {
  return {
    id: `art_${portKey}`, kind: 'image', port_key: portKey, path: `p/${portKey}.png`,
    thumb: `t/${portKey}.webp`, sha256: '', meta: {},
  }
}

const live: Record<string, LiveStep> = {
  'inst_1:consume': { status: 'success', progress: 1, outputs: [artifact('final'), artifact('echo')] },
  // A step from a different instance, to prove the prefix match is not just a substring search.
  'inst_2:consume': { status: 'success', progress: 1, outputs: [artifact('final')] },
}

describe('outputsForInstance', () => {
  it('finds the artifact behind each promoted output port', () => {
    const result = outputsForInstance(instance, [port('result', 'consume', 'final')], live)
    expect(result).toHaveLength(1)
    expect(result[0].artifact?.path).toBe('p/final.png')
  })

  it('reports a port with nothing produced as null rather than dropping it', () => {
    const result = outputsForInstance(instance, [port('later', 'generate', 'image')], live)
    expect(result[0].port.key).toBe('later')
    expect(result[0].artifact).toBeNull()
  })

  it('ignores input ports', () => {
    const ports = [port('in', 'consume', 'image', 'in'), port('out', 'consume', 'final')]
    expect(outputsForInstance(instance, ports, live).map((r) => r.port.key)).toEqual(['out'])
  })

  it('does not pick up another instance of the same template', () => {
    const other = { id: 'inst_2' } as TemplateInstance
    const ports = [port('result', 'consume', 'final')]
    expect(outputsForInstance(other, ports, live)[0].artifact?.id).toBe('art_final')
    // Both resolve, but each from its own expanded step.
    expect(innerStepId('inst_1', 'consume')).toBe('inst_1:consume')
    expect(innerStepId('inst_2', 'consume')).toBe('inst_2:consume')
  })

  it('survives a template with no ports at all', () => {
    expect(outputsForInstance(instance, undefined, live)).toEqual([])
  })
})

describe('belongsToInstance', () => {
  it('matches only the instance that owns the step', () => {
    expect(belongsToInstance('inst_1:consume', 'inst_1')).toBe(true)
    expect(belongsToInstance('inst_10:consume', 'inst_1')).toBe(false)
    expect(belongsToInstance('step_plain', 'inst_1')).toBe(false)
  })
})
