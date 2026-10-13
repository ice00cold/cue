/**
 * Cron-family API types (`/api/cron/*`).
 *
 * Extracted from api.ts (size ratchet — the M2 drill-down additions pushed
 * the facade over its ratcheted cap): the wire shapes shared by the cron REST
 * surface and its consumers (CronPage, the Thread drill-down's cron section).
 * api.ts re-exports these for existing import sites, exactly as it already
 * does for api-sessions/api-files/api-aux.
 */

export interface CronJobRepeat {
  times: number | null;
  completed?: number;
}

export interface CronJobMutation {
  name?: string;
  prompt?: string;
  schedule?: string;
  deliver?: string;
  skills?: string[];
  provider?: string | null;
  model?: string | null;
  base_url?: string | null;
  script?: string | null;
  no_agent?: boolean;
  context_from?: string[] | null;
  enabled_toolsets?: string[] | null;
  workdir?: string | null;
}

export interface CronJob {
  id: string;
  profile?: string | null;
  profile_name?: string | null;
  hermes_home?: string | null;
  is_default_profile?: boolean;
  name?: string | null;
  prompt?: string | null;
  script?: string | null;
  skills?: string[] | null;
  schedule?: { kind?: string; expr?: string; run_at?: string; display?: string };
  schedule_display?: string | null;
  repeat?: CronJobRepeat | null;
  enabled: boolean;
  state?: string | null;
  deliver?: string | null;
  model?: string | null;
  provider?: string | null;
  base_url?: string | null;
  no_agent?: boolean | null;
  context_from?: string[] | string | null;
  enabled_toolsets?: string[] | null;
  workdir?: string | null;
  last_run_at?: string | null;
  next_run_at?: string | null;
  /** Seconds since the job's profile ticker last iterated; null when it cannot be dated. */
  scheduler_heartbeat_age_s?: number | null;
  last_status?: string | null;
  last_error?: string | null;
  last_delivery_error?: string | null;
  last_fire_error?: { at?: string | null; detail?: string | null } | null;
}

export interface CronDeliveryTarget {
  id: string;
  name: string;
  home_target_set: boolean;
  home_env_var: string | null;
}
