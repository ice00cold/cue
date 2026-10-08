/**
 * The end of the questionnaire. `handoffPrompt` is the first message of the first chat: the ask on
 * line one, then "About me" and "Before you start", every line from a picked option or a machine
 * fact. `finish` (Start) and `skipSetup` (Skip setup) leave the questionnaire for the default profile.
 */

import { $freeTierStatus, ackFreeTierNotice } from '@/store/free-tier'
import { acceptLocalSetupOffer, dismissLocalSetupOffer, noteHandoffSession } from '@/store/local-setup-offer'
import { clearFreeTierIntro } from '@/store/onboarding'

import { type OnboardingRequester, setRun } from './due'
import { freeAccountState } from './facts'
import {
  type Answers,
  chosenName,
  chosenTask,
  connectorOptions,
  type Facts,
  FLOW,
  type FlowOption,
  type FlowStep,
  type LocalFit,
  pluginOptions,
  stepOptions,
  visibleSteps
} from './flow'
import { type InferenceClock, waitForInference } from './inference'
import { closeQuestionnaire, setStarting, showPreparing } from './store'

export const NO_TASK_ASK = 'What can you help me with? Ask me what I want to do first.'

export const MEMORY_LINE = 'Save the About me lines to your memory of me.'

const OS_NAMES: Record<string, string> = { darwin: 'macOS', linux: 'Linux', win32: 'Windows' }

/** "RTX Spark (NVIDIA N1X · Windows 11 · 128 GB RAM)", or `null` when the facts are missing. */
export function machineLine(facts: Facts): null | string {
  const summary = facts.machine

  if (!summary) {
    return null
  }

  const { machine } = summary
  const os = machine.os_family ? [OS_NAMES[machine.os_family] ?? machine.os_family, machine.os_release] : []

  const detail = [
    machine.cpu_model,
    os.filter(Boolean).join(' '),
    summary.has_nvidia_gpu && !summary.is_spark ? 'NVIDIA GPU' : null,
    machine.ram_gb ? `${machine.ram_gb} GB RAM` : null
  ].filter(Boolean)

  const label = summary.is_spark && machine.os_family === 'win32' ? 'RTX Spark' : summary.machine_kind

  return detail.length > 0 ? `${label} (${detail.join(' · ')})` : label
}

function pickedOptions(step: FlowStep, facts: Facts, answers: Answers): FlowOption[] {
  if (answers.skipped.includes(step.id)) {
    return []
  }

  const ids: readonly string[] = step.id === 'apps' ? answers.apps : step.id === 'connectors' ? answers.connectors : []

  return stepOptions(step, facts, answers).filter(option => ids.includes(option.id))
}

function appsLine(facts: Facts, answers: Answers): null | string {
  const offered = (options: FlowOption[], step: 'apps' | 'connectors') =>
    answers.skipped.includes(step) ? new Set<string>() : new Set(options.map(option => option.id))

  const plugins = offered(pluginOptions(facts), 'apps')
  const connectors = offered(connectorOptions(facts), 'connectors')
  const connectorRows = facts.connectors.status === 'ready' ? facts.connectors.rows : []

  const titles = [
    ...facts.plugins.filter(row => plugins.has(row.name) && answers.apps.includes(row.name)).map(row => row.title),
    ...connectorRows.filter(row => connectors.has(row.id) && answers.connectors.includes(row.id)).map(row => row.label)
  ]

  return titles.length > 0 ? `Apps I use: ${titles.join(', ')}.` : null
}

function aboutLines(facts: Facts, answers: Answers): string[] {
  const name = chosenName(answers)
  const machine = machineLine(facts)

  return [
    name ? `Call me ${name}.` : null,
    machine ? `This machine: ${machine}.` : null,
    appsLine(facts, answers)
  ].filter((line): line is string => line !== null)
}

function beforeLines(facts: Facts, answers: Answers, flow: readonly FlowStep[]): string[] {
  const steps = visibleSteps(facts, answers, flow)
  const picked = steps.flatMap(step => pickedOptions(step, facts, answers))
  const task = chosenTask(facts, answers, flow)

  return [...picked, ...(task ? [task] : [])].flatMap(option => option.before ?? [])
}

const bullets = (lines: string[]) => lines.map(line => `- ${line}`)

export function handoffPrompt(facts: Facts, answers: Answers, flow: readonly FlowStep[] = FLOW): string {
  const ask = chosenTask(facts, answers, flow)?.ask ?? NO_TASK_ASK
  const about = aboutLines(facts, answers)
  const before = [...beforeLines(facts, answers, flow), ...(about.length > 0 ? [MEMORY_LINE] : [])]

  const sections = [
    [ask],
    about.length > 0 ? ['About me:', ...bullets(about)] : [],
    before.length > 0 ? ['Before you start:', ...bullets(before)] : []
  ].filter(section => section.length > 0)

  return sections.map(section => section.join('\n')).join('\n\n')
}

export interface HandoffDeps {
  /** The connected backend (the launch profile's). */
  request: OnboardingRequester
  launchProfile: string
  /** `session.create` in the default profile, then `prompt.submit`; answers the runtime session id. */
  openDefaultChat: (text: string) => Promise<string>
  /** Re-run the overlay's readiness round so it lands on the right screen after the questionnaire. */
  refreshReadiness: () => Promise<unknown>
  /** The built-in quick tour; resolves once it is closed. */
  runTour: () => Promise<void>
  /** Local-model quickstart for the default profile. */
  startQuickstart: (model: LocalFit) => Promise<void>
  clock?: InferenceClock
}

export type FinishResult = 'failed' | 'started'

function applyLocalAnswer(answers: Answers): void {
  if (answers.skipped.includes('local')) {
    return
  }

  if (answers.local === 'yes') {
    acceptLocalSetupOffer()
  } else if (answers.local === 'no') {
    dismissLocalSetupOffer()
  }
}

/** Start: wait for inference, settle the flags, close, tour, then the first chat in default. */
export async function finish(facts: Facts, answers: Answers, deps: HandoffDeps): Promise<FinishResult> {
  setStarting(true)
  const ready = await waitForInference(deps.request, { clock: deps.clock })

  if (!ready.ok) {
    // D23: the prompt is dropped; confirmed accent and layout stay; the picker explains the failure.
    await setRun(deps.request, false)
    closeQuestionnaire('failed')
    await deps.refreshReadiness()

    return 'failed'
  }

  if (deps.launchProfile !== 'default') {
    // The default profile adopts the free account the launch profile's backend made.
    await deps.request('free_tier.provision', { profile: 'default' })
  }

  await setRun(deps.request, false, { markProfileOffered: true })
  applyLocalAnswer(answers)

  // D24: the free account was introduced here, so the ready screen must not follow.
  if (await ackFreeTierNotice(deps.request)) {
    clearFreeTierIntro()
  }

  await deps.refreshReadiness()
  closeQuestionnaire('done')

  // D19: after the overlay is gone and before the first message, so it never covers approval cards.
  if (answers.tour === 'quick' && !answers.skipped.includes('tour')) {
    await deps.runTour()
  }

  const sessionId = await deps.openDefaultChat(handoffPrompt(facts, answers))

  noteHandoffSession(sessionId)

  // After session.create, so the first chat stays on the free tier while the model downloads.
  if (answers.local === 'yes' && !answers.skipped.includes('local') && facts.local) {
    void deps.startQuickstart(facts.local)
  }

  return 'started'
}

/** Resolves once the free account exists or has failed for good. */
function freeAccountSettled(): Promise<void> {
  return new Promise(resolve => {
    const stop = $freeTierStatus.listen(status => {
      if (freeAccountState(status) !== 'waiting') {
        stop()
        resolve()
      }
    })
  })
}

/**
 * Skip setup: done for good, the profile offer left alone (D13), the notice still owed (D24). While
 * the free account is still being made the overlay says "Starting Hermes…" and then moves on to the
 * ready screen or the picker on its own.
 */
export async function skipSetup(deps: Pick<HandoffDeps, 'refreshReadiness' | 'request'>): Promise<void> {
  await setRun(deps.request, false)

  if (freeAccountState($freeTierStatus.get()) === 'waiting') {
    showPreparing()
    await freeAccountSettled()
  }

  closeQuestionnaire('skipped')
  await deps.refreshReadiness()
}
