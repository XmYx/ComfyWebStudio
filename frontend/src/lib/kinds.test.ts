import { describe, expect, it } from 'vitest'
import { canConnect, isScalar, isVisual, KIND_COLOR } from './kinds'

/**
 * These mirror backend/comfywebstudio/core/models.py. The duplication is deliberate — the canvas has to
 * reject an impossible connection mid-drag — so this test exists to keep the two in step.
 */
describe('canConnect', () => {
  it('accepts identical kinds', () => {
    for (const kind of ['image', 'mask', 'video', 'audio', 'latent', 'string'] as const) {
      expect(canConnect(kind, kind)).toBe(true)
    }
  })

  it('allows the documented implicit conversions', () => {
    expect(canConnect('image', 'mask')).toBe(true)
    expect(canConnect('mask', 'image')).toBe(true)
    expect(canConnect('int', 'float')).toBe(true)
    expect(canConnect('int', 'string')).toBe(true)
    expect(canConnect('boolean', 'string')).toBe(true)
    expect(canConnect('image', 'file')).toBe(true)
  })

  it('refuses conversions that would lose meaning', () => {
    expect(canConnect('float', 'int')).toBe(false)
    expect(canConnect('image', 'audio')).toBe(false)
    expect(canConnect('audio', 'video')).toBe(false)
    expect(canConnect('latent', 'image')).toBe(false)
    expect(canConnect('string', 'image')).toBe(false)
  })
})

describe('kind helpers', () => {
  it('classifies scalars', () => {
    expect(isScalar('string')).toBe(true)
    expect(isScalar('int')).toBe(true)
    expect(isScalar('image')).toBe(false)
  })

  it('classifies visual kinds', () => {
    expect(isVisual('image')).toBe(true)
    expect(isVisual('video')).toBe(true)
    expect(isVisual('audio')).toBe(false)
  })

  it('has a colour for every kind the backend can emit', () => {
    const kinds = ['image', 'mask', 'video', 'audio', 'latent', 'string', 'int', 'float', 'boolean', 'file']
    for (const kind of kinds) expect(KIND_COLOR[kind]).toBeTruthy()
  })
})
