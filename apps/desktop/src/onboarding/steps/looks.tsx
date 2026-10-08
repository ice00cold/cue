import { useStore } from '@nanostores/react'
import { Puzzle } from 'lucide-react'

import { ConnectorLogo } from '@/components/ui/connector-logo'
import { Skeleton } from '@/components/ui/skeleton'
import { useI18n } from '@/i18n'
import { connectorSubject } from '@/lib/connector-tools'
import { Cpu, LayoutDashboard, Palette, Plug } from '@/lib/icons'
import { $activeGatewayProfile, normalizeProfileKey } from '@/store/profile'
import { accentsFor, customAccentId, normalizeAccentId, resolveAccent } from '@/themes/accents'
import { useTheme } from '@/themes/context'

import { type AccentTarget, commitAccent, commitLayout, previewAccent, QUESTIONNAIRE_PROFILE } from '../apply'
import { pluginOptions, stepOptions, STEPS } from '../flow'
import { $questionnaire, confirmStep, holdPreview, setAnswers, skipStep } from '../store'
import { Chip } from '../visuals/chip'
import { AnswerMark } from '../visuals/motion'
import { AccentSwatch, LayoutPreviewCard, LAYOUTS } from '../visuals/options'

import { StepCard } from './step-card'

function useAccentTarget(): AccentTarget {
  const { accent, setAccent } = useTheme()
  const live = normalizeProfileKey(useStore($activeGatewayProfile)) === QUESTIONNAIRE_PROFILE

  return { current: accent, live, setAccent }
}

export function AccentStep() {
  const { t } = useI18n()
  const { renderedMode, theme } = useTheme()
  const target = useAccentTarget()
  const { answers } = useStore($questionnaire)
  const dark = renderedMode === 'dark'
  const selected = answers.accent !== undefined ? answers.accent : target.live ? target.current : null
  const custom = selected?.startsWith('custom:') ? resolveAccent(selected, dark) : null

  const pick = (id: string) => {
    const revert = previewAccent(target, normalizeAccentId(id))

    if (revert) {
      holdPreview(revert)
    }

    setAnswers({ accent: normalizeAccentId(id) })
  }

  return (
    <StepCard
      canConfirm
      icon={Palette}
      kind={t.questionnaire.kinds.accent}
      onConfirm={() => {
        commitAccent(target, selected)
        setAnswers({ accent: selected })
        confirmStep('accent')
      }}
      onSkip={() => skipStep('accent')}
      title={t.questionnaire.accent.title}
    >
      <div className="flex flex-wrap items-center gap-2.5 p-1" role="group">
        {accentsFor(dark).map(swatch => {
          const active = swatch.id === 'nous' ? selected === null : selected === swatch.id

          return (
            <span className="relative rounded-full" key={swatch.id}>
              <AccentSwatch active={active} hex={swatch.hex} name={swatch.name} onPick={() => pick(swatch.id)} />
              {active ? <AnswerMark stepId="accent" /> : null}
            </span>
          )
        })}
        <AccentSwatch
          active={custom !== null}
          hex={custom ?? theme.colors.primary}
          name={t.questionnaire.accent.custom}
          onColorChange={hex => pick(customAccentId(hex))}
        />
      </div>
    </StepCard>
  )
}

const LAYOUT_COPY = {
  'sidebar-left': { detail: 'basicDetail', name: 'basic' },
  'terminal-deck': { detail: 'eliteDetail', name: 'elite' }
} as const

export function LayoutStep() {
  const { t } = useI18n()
  const { answers } = useStore($questionnaire)
  const copy = t.questionnaire.layout

  return (
    <StepCard
      canConfirm={answers.layout !== undefined}
      icon={LayoutDashboard}
      kind={t.questionnaire.kinds.layout}
      onConfirm={() => {
        if (answers.layout) {
          commitLayout(answers.layout)
        }

        confirmStep('layout')
      }}
      onSkip={() => skipStep('layout')}
      title={copy.title}
    >
      <div className="grid grid-cols-2 gap-4 p-1">
        {LAYOUTS.map(layout => {
          const keys = LAYOUT_COPY[layout.id as keyof typeof LAYOUT_COPY]
          const active = answers.layout === layout.id

          return (
            <div className="relative rounded-[8px]" key={layout.id}>
              <LayoutPreviewCard
                active={active}
                description={keys ? copy[keys.detail] : layout.description}
                name={keys ? copy[keys.name] : layout.name}
                onSelect={() => setAnswers({ layout: layout.id })}
                tree={layout.tree}
              />
              {active ? <AnswerMark stepId="layout" /> : null}
            </div>
          )
        })}
      </div>
    </StepCard>
  )
}

const OS_NAMES: Record<string, string> = { darwin: 'macOS', linux: 'Linux', win32: 'Windows' }

function MachineSpec() {
  const { t } = useI18n()
  const machine = useStore($questionnaire).facts.machine
  const copy = t.questionnaire.local

  if (!machine) {
    return null
  }

  const info = machine.machine

  const rows: Array<[string, null | string | undefined]> = [
    [copy.chip, info.cpu_model],
    [copy.gpu, machine.has_nvidia_gpu ? 'NVIDIA' : null],
    [copy.memory, info.ram_gb ? copy.memoryValue(info.ram_gb) : null],
    [copy.os, info.os_family ? [OS_NAMES[info.os_family] ?? info.os_family, info.os_release].filter(Boolean).join(' ') : null]
  ]

  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs">
      {rows
        .filter((row): row is [string, string] => Boolean(row[1]))
        .map(([label, value]) => (
          <div className="contents" key={label}>
            <dt className="text-(--ui-text-tertiary)">{label}</dt>
            <dd className="font-mono">{value}</dd>
          </div>
        ))}
    </dl>
  )
}

export function LocalStep() {
  const { t } = useI18n()
  const { answers, facts } = useStore($questionnaire)
  const copy = t.questionnaire.local
  const kind = facts.machine?.machine_kind ?? 'computer'
  const title = facts.machine?.is_spark ? copy.titleSpark : copy.title(kind)

  return (
    <StepCard
      canConfirm={answers.local !== undefined}
      icon={Cpu}
      kind={t.questionnaire.kinds.local}
      onConfirm={() => confirmStep('local')}
      onSkip={() => {
        setAnswers({ local: 'no' })
        skipStep('local')
      }}
      title={title}
    >
      <MachineSpec />
      <div className="grid gap-2 sm:grid-cols-2">
        {stepOptions(STEPS.local, facts, answers).map(option => {
          const on = answers.local === option.id

          return (
            <div className="relative rounded-[6px]" key={option.id}>
              <Chip
                className="w-full"
                label={option.label(t.questionnaire)}
                on={on}
                onToggle={() => setAnswers({ local: option.id === 'yes' ? 'yes' : 'no' })}
                sub={option.detail?.(t.questionnaire)}
              />
              {on ? <AnswerMark stepId="local" /> : null}
            </div>
          )
        })}
      </div>
    </StepCard>
  )
}

function toggle(list: readonly string[], id: string): string[] {
  return list.includes(id) ? list.filter(item => item !== id) : [...list, id]
}

export function AppsStep() {
  const { t } = useI18n()
  const { answers, facts } = useStore($questionnaire)
  const copy = t.questionnaire.apps
  const options = pluginOptions(facts)
  const picked = options.filter(option => answers.apps.includes(option.id))

  return (
    <StepCard
      canConfirm={picked.length > 0}
      icon={Puzzle}
      kind={t.questionnaire.kinds.apps}
      onConfirm={() => confirmStep('apps')}
      onSkip={() => skipStep('apps')}
      title={copy.title}
    >
      <div className="grid gap-2 sm:grid-cols-2">
        {options.map(option => (
          <Chip
            icon={<Puzzle aria-hidden className="size-4 shrink-0 text-(--ui-text-tertiary)" />}
            key={option.id}
            label={option.label(t.questionnaire)}
            on={answers.apps.includes(option.id)}
            onToggle={() => setAnswers({ apps: toggle(answers.apps, option.id) })}
            sub={option.detail?.(t.questionnaire)}
          />
        ))}
      </div>
      {picked.length > 0 ? (
        <p className="text-xs leading-5 text-muted-foreground">
          {picked.map(option => `${option.label(t.questionnaire)}: ${option.disclosure ?? ''}`).join(' ')} {copy.approve}
        </p>
      ) : null}
      <p className="text-xs text-(--ui-text-tertiary)">{copy.found}</p>
    </StepCard>
  )
}

const SKELETON_ROWS = 6

export function ConnectorsStep() {
  const { t } = useI18n()
  const { answers, facts } = useStore($questionnaire)
  const copy = t.questionnaire.connectors
  const loading = facts.connectors.status === 'loading'
  const options = stepOptions(STEPS.connectors, facts, answers)

  return (
    <StepCard
      canConfirm={options.some(option => answers.connectors.includes(option.id))}
      icon={Plug}
      kind={t.questionnaire.kinds.connectors}
      onConfirm={() => confirmStep('connectors')}
      onSkip={() => skipStep('connectors')}
      title={copy.title}
    >
      <div aria-busy={loading || undefined} className="grid gap-2 sm:grid-cols-3">
        {loading
          ? Array.from({ length: SKELETON_ROWS }, (_, index) => <Skeleton className="h-10 rounded-[6px]" key={index} />)
          : options.map(option => (
              <Chip
                icon={<ConnectorLogo className="size-5 shrink-0" connector={connectorSubject(option.id)} />}
                key={option.id}
                label={option.label(t.questionnaire)}
                on={answers.connectors.includes(option.id)}
                onToggle={() => setAnswers({ connectors: toggle(answers.connectors, option.id) })}
              />
            ))}
      </div>
      <p className="text-xs text-(--ui-text-tertiary)">{loading ? copy.checking : copy.offered}</p>
    </StepCard>
  )
}
