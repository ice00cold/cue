/**
 * The questionnaire's background work, in the status bar (it stays visible under the overlay):
 * the free account while it is being made, and the local model download started after Start.
 */

import { useStore } from '@nanostores/react'
import { atom } from 'nanostores'

import { AnimatedInt } from '@/components/ui/diff-count'
import { Progress } from '@/components/ui/progress'
import { useI18n } from '@/i18n'
import { $freeTierStatus, freeTierSetupFailure } from '@/store/free-tier'
import { localModelsOwner, runningModelDownloads, useLocalRuntimeJobs } from '@/store/local-runtime-jobs'

import { freeAccountState } from './facts'
import { $questionnaireOpen } from './store'

/** The model the questionnaire's quickstart is downloading, by display name; `null` when none. */
export const $questionnaireDownload = atom<null | string>(null)

const ITEM_CLASS = 'flex h-full items-center gap-1.5 px-1.5 text-[0.6875rem]'

/** "Setting up your free account" until `setup.ready`, then "Nous · free tier"; only while the questionnaire is open. */
export function FreeAccountStatusItem() {
  const { t } = useI18n()
  const open = useStore($questionnaireOpen)
  const status = useStore($freeTierStatus)
  const copy = t.questionnaire.status

  if (!open || !status?.enabled) {
    return null
  }

  const state = freeAccountState(status)

  if (state === 'ready') {
    return <span className={ITEM_CLASS}>{copy.ready}</span>
  }

  if (state === 'failed') {
    return <span className={`${ITEM_CLASS} text-destructive`}>{copy.unavailable}</span>
  }

  const label = freeTierSetupFailure(status) ? copy.stillSettingUp : copy.settingUp

  return (
    <span className={ITEM_CLASS} role="status">
      <span>{label}</span>
      <Progress animated aria-label={label} className="w-12" indeterminate size="sm" />
    </span>
  )
}

const DEFAULT_OWNER = localModelsOwner('default')

function DownloadProgress({ model }: { model: string }) {
  const { t } = useI18n()
  const job = useLocalRuntimeJobs(DEFAULT_OWNER, jobs => runningModelDownloads(jobs)[0] ?? null)

  if (!job) {
    return null
  }

  const percent = Math.round(job.percent ?? (job.total_bytes ? (job.done_bytes / job.total_bytes) * 100 : 0))
  const label = t.questionnaire.status.downloading(model)

  return (
    <span className={ITEM_CLASS} role="status">
      <span className="truncate">{label}</span>
      <Progress aria-label={label} className="w-14" size="sm" value={percent / 100} />
      <span className="tabular-nums">
        <AnimatedInt value={percent} />%
      </span>
    </span>
  )
}

/** The local model the questionnaire started downloading, with a bar, until the job settles. */
export function LocalDownloadStatusItem() {
  const model = useStore($questionnaireDownload)

  return model ? <DownloadProgress model={model} /> : null
}
