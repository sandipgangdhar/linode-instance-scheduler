export type InstanceStatus = 'running' | 'stopped' | 'unreachable' | 'needs_manual_recovery'
export interface DataVolume {
  volume_id: number
  device_slot: string
  fstab_identifier: string | null
}
export interface InstanceRecord {
  label: string | null
  region: string | null
  network_interface_model: string | null
  network_config: unknown
  authorized_keys: string[] | null
  tags: string[] | null
  instance_attrs: Record<string, unknown> | null
  network_helper_enabled: boolean | null
  vpc_prefix: number | null
  os_volume_id: number | null
  data_volumes: DataVolume[] | null
  reserved_ip: string | null
  group_id: number | null
  current_linode_id: number | null
  current_status: InstanceStatus
  transitioning: boolean
  manual_override_expires_at: string | null
}
export interface StartResult {
  outcome:
    | 'started'
    | 'already_running'
    | 'confirmed_gone_reset_to_stopped'
    | 'found_offline_marked_unreachable'
    | 'transitioning'
    | 'reachability_check_failed'
    | 'create_failed'
  instance_id: number | null
  reserved_ip: string | null
  live_status: string | null
  security_warning: boolean
  detail: string | null
  manual_override_expires_at: string | null
}
export const START_SUCCESS_OUTCOMES: ReadonlySet<StartResult['outcome']> = new Set([
  'started',
  'already_running',
])
export interface StopResult {
  outcome:
    | 'stopped'
    | 'already_stopped'
    | 'confirmed_gone_reset_to_stopped'
    | 'aborted_by_user'
    | 'prepare_failed'
    | 'delete_failed'
  instance_id: number | null
  detail: string | null
}
export const STOP_SUCCESS_OUTCOMES: ReadonlySet<StopResult['outcome']> = new Set([
  'stopped',
  'already_stopped',
  'confirmed_gone_reset_to_stopped',
])
export interface ScheduleRule {
  days_of_week: string[]
  start_time: string
  stop_time: string
}
export interface Schedule {
  timezone: string
  rules: ScheduleRule[]
  enabled: boolean
  warnings?: string[]
}
export interface ScheduleGroup {
  id: number
  name: string
  timezone: string
  rules: ScheduleRule[]
  enabled: boolean
  members: string[]
  warnings?: string[]
}
export interface ScheduleGroupSummary {
  id: number
  name: string
  timezone: string
  rules: ScheduleRule[]
  enabled: boolean
  member_count: number
}
export interface ScheduleEvent {
  action: 'create' | 'delete'
  triggered_by: 'schedule' | 'manual' | 'api'
  timestamp: string
  result: 'success' | 'failure'
  error_message: string | null
  actor: string | null
}
export interface LinodeRawInstance {
  id: number
  label: string
  region: string
  status: string
  tags: string[]
  ipv4: string[]
  onboarded_as: string | null
}
export interface OnboardResult {
  outcome: 'onboarded' | 'identity_change_declined'
  record: InstanceRecord | null
  warnings?: string[]
}
export interface OffboardResult {
  outcome: 'aborted_by_user' | 'incomplete' | 'offboarded'
  detail: string | null
  warnings?: string[]
}
export interface DeregisterResult {
  outcome: 'not_onboarded' | 'aborted_by_user' | 'deregistered'
}
export interface PatchInstanceGroupResult {
  group_id?: number | null
  outcome?: 'removed_had_individual_schedule' | 'removed_kept_manual' | 'removed_copied_schedule'
  warnings?: string[]
}
export interface MigrateStartResult {
  outcome: 'started' | 'identity_change_declined'
  instance_id: number | null
  dest_volume_id: number | null
  dest_volume_size_gb: number | null
  local_disk_size_mb: number | null
}
export interface MigrateResumeResult {
  outcome: 'resumed' | 'incomplete'
  instance_id: number | null
  os_volume_id: number | null
  reserved_ip: string | null
  previous_attempts_count: number
  detail: string | null
  fstab_entries_disabled: string[]
}
export interface MigrateStatus {
  in_progress: boolean
  phase?: string | null
  instance_id?: number | null
  dest_volume_id?: number | null
  dest_volume_size_gb?: number | null
  local_disk_size_mb?: number | null
}
export interface OperationStatus<T> {
  status: 'running' | 'done' | 'error'
  action: string
  percent: number
  current_step: string | null
  warnings: string[]
  result?: T
}
export interface Savings {
  scheduled_savings_percent: number | null
  actual_savings_percent: number | null
  window_days: number
  schedule_state?: 'none' | 'disabled' | 'active'
  note?: string
}
export const VALID_DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'] as const
export type Weekday = (typeof VALID_DAYS)[number]
