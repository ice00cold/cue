/**
 * The first-run questionnaire (plan PR3). `Questionnaire` is the controller wiring mounts: it runs
 * the due check once the gateway is open, reads the facts while the questionnaire is open, and lends
 * the handoff what only the app shell has. `QuestionnaireScreen` is the first state of the one
 * first-run overlay (`components/onboarding`, D22); the picker, ready screen and confirm screen are its others.
 */

import { useStore } from '@nanostores/react'
import { AnimatePresence, LayoutGroup } from 'motion/react'
import { atom } from 'nanostores'
import { type ComponentType, useEffect } from 'react'

import { quickstartLocalModels } from '@/api/local-models'
import { Button } from '@/components/ui/button'
import { Progress } from '@/components/ui/progress'
import { useI18n } from '@/i18n'
import { $freeTierStatus } from '@/store/free-tier'
import { localModelsOwner, localModelsRequestScope } from '@/store/local-runtime-jobs'
import { notifyError } from '@/store/notifications'
import { markQuestionnaireDecided } from '@/store/onboarding-presence'
import { $activeGatewayProfile, normalizeProfileKey } from '@/store/profile'
import { $connection } from '@/store/session'
import { isMainWindow } from '@/store/windows'

import { decideQuestionnaire, type OnboardingRequester, setRun } from './due'
import { defaultFactSources, freeAccountState, watchFacts } from './facts'
import type { StepId } from './flow'
import { finish, type HandoffDeps, skipSetup } from './handoff'
import { $questionnaireDownload } from './status-items'
import { AccentStep, AppsStep, ConnectorsStep, LayoutStep, LocalStep } from './steps/looks'
import { NameStep, TaskStep, TourStep } from './steps/questions'
import { ReviewStep } from './steps/review'
import {
  $questionnaire,
  $questionnaireOpen,
  closeQuestionnaire,
  goBack,
  goToStep,
  passedSteps,
  type QuestionnaireState,
  setFacts,
  setStarting
} from './store'
import { answerSummary } from './summary'
import { runQuickTourWhenTargetsPaint } from './tour'
import { StepTransition, TrailChip } from './visuals/motion'

/** What the handoff needs from the app shell; the overlay adds its own readiness round. */
type QuestionnaireHost = Omit<HandoffDeps, 'launchProfile' | 'refreshReadiness'>

const $questionnaireHost = atom<null | QuestionnaireHost>(null)

/** The local primary backend: a remote host is not this computer (D2). */
const onLocalPrimary = () => $connection.get()?.mode === 'local'

interface QuestionnaireProps {
  enabled: boolean
  /** `session.create` in the default profile + `prompt.submit`; answers the runtime session id. */
  openDefaultChat: (text: string) => Promise<string>
  requestGateway: OnboardingRequester
}

export function Questionnaire({ enabled, openDefaultChat, requestGateway }: QuestionnaireProps) {
  const open = useStore($questionnaireOpen)

  useEffect(() => {
    if (!isMainWindow()) {
      markQuestionnaireDecided()

      return
    }

    if (enabled) {
      if (onLocalPrimary()) {
        void decideQuestionnaire(requestGateway)
      } else {
        markQuestionnaireDecided()
      }
    }
  }, [enabled, requestGateway])

  useEffect(() => {
    $questionnaireHost.set({
      openDefaultChat,
      request: requestGateway,
      runTour: runQuickTourWhenTargetsPaint,
      startQuickstart: async model => {
        $questionnaireDownload.set(model.name)
        await quickstartLocalModels(model.id, localModelsRequestScope(localModelsOwner('default')))
      }
    })
  }, [openDefaultChat, requestGateway])

  useEffect(() => (open ? watchFacts(defaultFactSources(requestGateway), setFacts) : undefined), [open, requestGateway])

  return null
}

const STEP_VIEWS: Record<StepId, ComponentType> = {
  accent: AccentStep,
  apps: AppsStep,
  connectors: ConnectorsStep,
  layout: LayoutStep,
  local: LocalStep,
  name: NameStep,
  task: TaskStep,
  tour: TourStep
}

function Trail({ state }: { state: QuestionnaireState }) {
  const { t } = useI18n()
  const passed = passedSteps(state)

  if (passed.length === 0) {
    return null
  }

  return (
    <nav aria-label={t.questionnaire.trailLabel} className="flex flex-wrap gap-1.5">
      {passed.map(stepId => (
        <TrailChip key={stepId} label={t.questionnaire.changeAnswer} onClick={() => goToStep(stepId)} stepId={stepId}>
          {answerSummary(stepId, state, t.questionnaire)}
        </TrailChip>
      ))}
    </nav>
  )
}

function Preparing() {
  const { t } = useI18n()

  return (
    <div className="grid gap-3" role="status">
      <p className="text-sm font-medium">{t.onboarding.starting}</p>
      <p className="text-sm text-muted-foreground">{t.onboarding.preparingInstall}</p>
      <Progress animated aria-label={t.onboarding.starting} indeterminate />
    </div>
  )
}

/** The questionnaire inside the first-run overlay. `refreshReadiness` is the overlay's own readiness round. */
export function QuestionnaireScreen({ refreshReadiness }: { refreshReadiness: () => Promise<unknown> }) {
  const { t } = useI18n()
  const state = useStore($questionnaire)
  const host = useStore($questionnaireHost)
  const { starting, stepId, view } = state

  // D23: a free account that failed for good hands over to the picker at once, mid-question or not.
  useEffect(() => {
    if (!host) {
      return
    }

    return $freeTierStatus.subscribe(status => {
      const current = $questionnaire.get()

      if (freeAccountState(status) === 'failed' && current.phase === 'shown' && !current.starting && current.view !== 'preparing') {
        closeQuestionnaire('failed')
        void setRun(host.request, false).finally(() => void refreshReadiness())
      }
    })
  }, [host, refreshReadiness])

  if (!host) {
    return null
  }

  const deps: HandoffDeps = {
    ...host,
    launchProfile: normalizeProfileKey($activeGatewayProfile.get()),
    refreshReadiness
  }

  const start = () => {
    void finish(state.facts, state.answers, deps).catch(error => {
      setStarting(false)
      notifyError(error, t.questionnaire.start.failed)
    })
  }

  const skip = () => void skipSetup(deps).catch(error => notifyError(error, t.questionnaire.settings.failed))

  if (view === 'preparing') {
    return <Preparing />
  }

  const Step = stepId ? STEP_VIEWS[stepId] : null
  const canGoBack = view === 'review' || passedSteps(state).length > 0

  return (
    <LayoutGroup>
      <div className="grid gap-5">
        <Trail state={state} />
        <AnimatePresence initial={false} mode="wait">
          <StepTransition key={view === 'review' ? 'review' : (stepId ?? 'none')}>
            {view === 'review' ? <ReviewStep onStart={start} /> : Step ? <Step /> : null}
          </StepTransition>
        </AnimatePresence>
        <div className="flex items-center justify-between gap-3">
          <Button disabled={starting} onClick={skip} size="xs" type="button" variant="text">
            {t.questionnaire.skipSetup}
          </Button>
          {canGoBack ? (
            <Button disabled={starting} onClick={goBack} size="xs" type="button" variant="text">
              {t.questionnaire.back}
            </Button>
          ) : null}
        </div>
      </div>
    </LayoutGroup>
  )
}
