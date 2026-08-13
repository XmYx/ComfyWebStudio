/**
 * Where the storyboard's models are chosen, and where missing ones are fetched.
 *
 * Two models, chosen separately, because they are two different jobs: one *writes* the shots and one
 * *looks* at the frames that come back. The one that writes well is rarely the one that can see.
 *
 * Which models can see is detected rather than guessed — Ollama reports it per model — so the vision
 * picker only offers models that will actually look at the image instead of confidently describing
 * nothing. When none is installed, the library below is how you get one without leaving the app.
 */

import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, ApiError } from '@/api/client'
import type { AppSettings, LlmModel } from '@/api/types'
import { useStudioEvents } from '@/api/events'
import {
  Badge, Button, Callout, Field, Panel, PanelHeader, ProgressBar, Select, Spinner, TextInput,
  cx, useToast,
} from '@/components/ui'

interface Pull {
  model: string
  status: string
  progress: number
  error?: string
}

export function LlmSettings({ settings, onChanged }: { settings: AppSettings; onChanged: () => void }) {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [pull, setPull] = useState<Pull | null>(null)
  const [typed, setTyped] = useState('')

  const { data: providers } = useQuery({
    queryKey: ['llm-providers'],
    queryFn: api.settings.llmProviders,
  })
  const { data: library } = useQuery({ queryKey: ['llm-library'], queryFn: api.settings.llmLibrary })

  const providerId = settings.story.provider_id ?? providers?.[0]?.id ?? ''
  const { data: installed, refetch: refetchModels, error: modelsError } = useQuery({
    queryKey: ['llm-models', providerId],
    queryFn: () => api.settings.llmModels(providerId),
    enabled: Boolean(providerId),
    retry: false,
  })

  // A pull is a background job that reports itself, exactly as a run does; when it finishes the model
  // list is refetched so the new one can be chosen straight away.
  useStudioEvents((event) => {
    if (event.type !== 'llm.pull') return
    const data = event.data as Pull & { finished?: boolean }
    setPull(data.finished && !data.error ? null : data)
    if (data.finished) {
      void refetchModels()
      if (data.error) toast.push('bad', data.error)
      else toast.push('ok', `${data.model} is ready.`)
    }
  })

  const addOllama = useMutation({
    mutationFn: () =>
      api.settings.addLlmProvider({
        name: 'Ollama', kind: 'ollama', base_url: 'http://127.0.0.1:11434',
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['llm-providers'] })
      onChanged()
    },
    onError: (error: ApiError) => toast.push('bad', error.message),
  })

  const patchStory = async (body: Partial<AppSettings['story']>) => {
    try {
      await api.settings.update({ story: { ...settings.story, ...body } })
      onChanged()
    } catch (error) {
      toast.push('bad', (error as ApiError).message)
    }
  }

  const startPull = async (model: string) => {
    if (!providerId || !model.trim()) return
    try {
      await api.settings.pullLlmModel(providerId, model.trim())
      setPull({ model: model.trim(), status: 'starting', progress: 0 })
    } catch (error) {
      toast.push('bad', (error as ApiError).message)
    }
  }

  const models: LlmModel[] = installed?.models ?? []
  const vision = models.filter((m) => m.vision)
  const have = new Set(models.map((m) => m.name))

  // Ollama's own names carry the tag; a library entry without one means :latest.
  const isInstalled = (name: string) =>
    have.has(name) || have.has(`${name}:latest`) || [...have].some((h) => h.startsWith(`${name}:`))

  useEffect(() => {
    if (!providerId || settings.story.provider_id) return
    void patchStory({ provider_id: providerId })
  }, [providerId]) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <Panel className="overflow-hidden">
      <PanelHeader>Language models</PanelHeader>

      <div className="space-y-3 p-3">
        {!providers?.length ? (
          <Callout tone="info">
            <div className="mb-2">
              Nothing configured. If Ollama is running on this machine, one click is all it takes.
            </div>
            <Button size="sm" disabled={addOllama.isPending} onClick={() => addOllama.mutate()}>
              {addOllama.isPending ? <Spinner /> : null} Use Ollama on this machine
            </Button>
          </Callout>
        ) : (
          <>
            <div className="grid grid-cols-3 gap-3">
              <Field label="Provider">
                <Select
                  value={providerId}
                  onChange={(e) => patchStory({ provider_id: e.target.value })}
                >
                  {providers.map((provider) => (
                    <option key={provider.id} value={provider.id}>
                      {provider.name} ({provider.kind})
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label="Writes the shots" hint="turns a premise into a sequence">
                <Select
                  data-testid="write-model"
                  value={settings.story.write_model}
                  onChange={(e) => patchStory({ write_model: e.target.value })}
                >
                  <option value="">Choose…</option>
                  {models.map((model) => (
                    <option key={model.name} value={model.name}>
                      {model.name}{model.size ? ` · ${model.size}` : ''}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field
                label="Looks at the frames"
                hint={vision.length ? 'only models that can see' : 'none installed yet'}
              >
                <Select
                  data-testid="vision-model"
                  value={settings.story.vision_model}
                  disabled={!vision.length}
                  onChange={(e) =>
                    patchStory({ vision_model: e.target.value, vision_provider_id: providerId })
                  }
                >
                  <option value="">Choose…</option>
                  {vision.map((model) => (
                    <option key={model.name} value={model.name}>
                      {model.name}{model.size ? ` · ${model.size}` : ''}
                    </option>
                  ))}
                </Select>
              </Field>
            </div>

            {modelsError && (
              <Callout tone="bad">{(modelsError as ApiError).message}</Callout>
            )}
            {!modelsError && !vision.length && (
              <Callout tone="warn">
                None of the installed models can see, so a frame cannot be described. Pull one below —
                <strong> qwen2.5vl</strong> is the usual choice.
              </Callout>
            )}

            {pull && (
              <div>
                <div className="mb-1 flex items-center justify-between text-[11px]">
                  <span>{pull.model}</span>
                  <span className="text-[var(--color-ink-dim)]">
                    {pull.status} · {Math.round((pull.progress ?? 0) * 100)}%
                  </span>
                </div>
                <ProgressBar value={pull.progress ?? 0} />
              </div>
            )}

            <div className="border-t border-[var(--color-edge)] pt-3">
              <div className="mb-2 text-[10px] font-semibold uppercase tracking-wide text-[var(--color-ink-dim)]">
                Get another model
              </div>
              <div className="space-y-1">
                {(library ?? []).map((entry) => (
                  <div
                    key={entry.name}
                    className={cx(
                      'flex items-center gap-2 rounded border border-[var(--color-edge)] px-2 py-1.5',
                    )}
                  >
                    <span className="w-40 shrink-0 truncate font-mono text-[11px]">{entry.name}</span>
                    {entry.vision ? <Badge tone="info">sees</Badge> : <Badge tone="muted">writes</Badge>}
                    <span className="min-w-0 flex-1 truncate text-[10px] text-[var(--color-ink-dim)]">
                      {entry.note}
                    </span>
                    <span className="shrink-0 text-[10px] text-[var(--color-ink-dim)]">{entry.size}</span>
                    {isInstalled(entry.name) ? (
                      <Badge tone="ok">installed</Badge>
                    ) : (
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={Boolean(pull)}
                        onClick={() => startPull(entry.name)}
                      >
                        Pull
                      </Button>
                    )}
                  </div>
                ))}
              </div>

              <div className="mt-2 flex items-center gap-2">
                <TextInput
                  value={typed}
                  placeholder="…or any other name, e.g. llama3.2-vision:11b"
                  className="h-7 flex-1 text-[11px]"
                  onChange={(e) => setTyped(e.target.value)}
                />
                <Button size="sm" variant="ghost" disabled={Boolean(pull) || !typed.trim()}
                        onClick={() => startPull(typed)}>
                  Pull
                </Button>
              </div>
            </div>
          </>
        )}
      </div>
    </Panel>
  )
}
