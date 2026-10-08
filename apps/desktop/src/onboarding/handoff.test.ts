import { describe, expect, it } from 'vitest'

import { answers, FIXTURES } from './fixtures.test-util'
import { handoffPrompt, MEMORY_LINE, NO_TASK_ASK } from './handoff'

const lines = (text: string) => text.split('\n')

describe('handoffPrompt', () => {
  const spark = FIXTURES.spark

  it('puts the ask on line one', () => {
    const prompt = handoffPrompt(spark, answers({ apps: ['blender'], name: 'Sid', task: { id: 'blender' } }))

    expect(lines(prompt)[0]).toBe('Build me a small scene in Blender: a desk, a lamp and a mug, lit for a render.')
  })

  it('asks what to do when no task was picked', () => {
    expect(lines(handoffPrompt(spark, answers()))[0]).toBe(NO_TASK_ASK)
    expect(lines(handoffPrompt(spark, answers({ skipped: ['task'], task: { id: 'tidy' } })))[0]).toBe(NO_TASK_ASK)
  })

  it('uses typed task text as the ask', () => {
    const prompt = handoffPrompt(spark, answers({ task: { id: 'other', text: '  Plan my week  ' } }))

    expect(lines(prompt)[0]).toBe('Plan my week')
  })

  it('carries the memory line exactly when About me has lines', () => {
    expect(handoffPrompt(spark, answers({ name: 'Sid' }))).toContain(MEMORY_LINE)

    const bare = { ...spark, machine: null }

    expect(handoffPrompt(bare, answers())).not.toContain('About me:')
    expect(handoffPrompt(bare, answers())).not.toContain(MEMORY_LINE)
    expect(handoffPrompt(bare, answers({ name: 'Sid' }))).toContain(MEMORY_LINE)
  })

  it('drops "Call me" when the name step was skipped', () => {
    expect(handoffPrompt(spark, answers({ name: 'Sid' }))).toContain('- Call me Sid.')
    expect(handoffPrompt(spark, answers({ name: 'Sid', skipped: ['name'] }))).not.toContain('Call me')
  })

  it('describes the machine from its facts', () => {
    expect(handoffPrompt(spark, answers())).toContain(
      '- This machine: RTX Spark (NVIDIA N1X · Windows 11 · 128 GB RAM).'
    )
  })

  it('adds exactly the picked options lines, with catalog ids', () => {
    const base = handoffPrompt(spark, answers())
    const picked = handoffPrompt(spark, answers({ apps: ['blender'], connectors: ['github'] }))
    const added = lines(picked).filter(line => !lines(base).includes(line))

    expect(added).toEqual([
      '- Apps I use: Blender, GitHub.',
      '- Install the Blender plugin (catalog name: blender).',
      '- If the Blender plugin will not connect, write ~/hermes-first-task/first_scene.py and give me the blender --python command.',
      '- Connect GitHub (connector: github).'
    ])
  })

  it('adds nothing for offered options that were not picked', () => {
    const prompt = handoffPrompt(spark, answers({ apps: ['nvidia-app'] }))

    expect(prompt).not.toContain('Blender')
    expect(prompt).not.toContain('Connect ')
  })

  it('drops picks of a step that was skipped afterwards', () => {
    const prompt = handoffPrompt(spark, answers({ apps: ['blender'], skipped: ['apps'] }))

    expect(prompt).not.toContain('Blender')
  })

  it('omits connectors when the list was unavailable', () => {
    const prompt = handoffPrompt({ ...spark, connectors: { status: 'unavailable' } }, answers({ connectors: ['github'] }))

    expect(prompt).not.toContain('GitHub')
  })

  it('adds the task lines only with a task', () => {
    expect(handoffPrompt(spark, answers())).not.toContain('~/hermes-first-task/.')
    expect(handoffPrompt(spark, answers({ task: { id: 'tidy' } }))).toContain('- Put new files in ~/hermes-first-task/.')
  })
})
