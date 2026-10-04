// 按作业状态给出可用的操作（纯函数，可单测）。按钮文案与表单要求见 JobActions.vue

export type ActionId = 'stop' | 'accept' | 'rework' | 'reject'

export interface AvailableAction {
  id: ActionId
  label: string
  kind: 'primary' | 'default' | 'danger'
}

/**
 * 运行中 → 叫停；待审查 / 运行失败 / 意外中断 / 已叫停 / 已驳回 → 验收、返工续派、驳回；
 * 人工持有和其它状态（已验收等）没有操作。待你决定（needs_user）走专门的答复区，也不在这里。
 */
export function availableActions(job: { state: string; needs_user?: string | null }): AvailableAction[] {
  if (job.needs_user) return []
  if (job.state === 'running') return [{ id: 'stop', label: '叫停', kind: 'danger' }]
  if (['done', 'failed', 'interrupted', 'stopped', 'rejected'].includes(job.state)) {
    return [
      { id: 'accept', label: '验收', kind: 'primary' },
      { id: 'rework', label: '返工续派', kind: 'default' },
      { id: 'reject', label: '驳回', kind: 'danger' },
    ]
  }
  return []
}

/** tmux 方式且界面还开着时才显示「接入界面」 */
export function canAttach(job: { mode?: string; tmux_alive?: boolean }): boolean {
  return job.mode === 'tmux' && !!job.tmux_alive
}

/** 验收前对闸门的提示：没跑（none）或没过（fail）都要提示先跑闸门 */
export function gateHint(state: 'ok' | 'fail' | 'none'): string {
  if (state === 'none') return '这个作业还没跑过闸门，建议先跑闸门再验收。'
  if (state === 'fail') return '这个作业的闸门没有通过，建议先跑闸门确认最新结果。'
  return ''
}
