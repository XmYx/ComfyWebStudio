import { describe, expect, it } from 'vitest'

import { choiceOptions, isMissing } from './choices'

const models = ['a.safetensors', 'b.safetensors']

describe('what a combo offers', () => {
  it('is just the list when the value is in it', () => {
    expect(choiceOptions('b.safetensors', models).map((c) => c.value)).toEqual(models)
    expect(choiceOptions('b.safetensors', models).some((c) => c.missing)).toBe(false)
  })

  it('carries a value the list does not have, rather than letting it show as the first one', () => {
    const options = choiceOptions('gone.safetensors', models)
    expect(options[0]).toEqual({
      value: 'gone.safetensors',
      label: 'gone.safetensors — not on this ComfyUI',
      missing: true,
    })
    expect(options.map((c) => c.value)).toEqual(['gone.safetensors', ...models])
  })

  it('adds nothing for an empty value', () => {
    expect(choiceOptions('', models).map((c) => c.value)).toEqual(models)
  })

  it('says when the picker is showing something that cannot run', () => {
    expect(isMissing('gone.safetensors', models)).toBe(true)
    expect(isMissing('a.safetensors', models)).toBe(false)
    expect(isMissing('', models)).toBe(false)
  })

  it('treats an empty list as offering nothing rather than as offering everything', () => {
    expect(choiceOptions('a.safetensors', []).map((c) => c.missing)).toEqual([true])
  })
})
