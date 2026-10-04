// 工作台分组：把作业按「要主管做什么」分成四个队列（纯函数，可单测）

import type { JobSummary } from '../types'
import { sortJobs } from './jobs'

export type InboxGroupKey = 'decide' | 'review' | 'broken' | 'running'

export interface InboxGroup {
  key: InboxGroupKey
  title: string
  jobs: JobSummary[]
}

/** 单个作业属于哪个工作台分组；不属于任何分组（已验收 / 已驳回 / 已叫停 / 人工持有等）返回 null */
export function inboxGroupOf(job: JobSummary): InboxGroupKey | null {
  if (job.needs_user) return 'decide'
  if (job.state === 'done') return 'review'
  if (job.state === 'failed' || job.state === 'interrupted') return 'broken'
  if (job.state === 'running') return 'running'
  return null
}

/** 四组固定顺序：正在执行排第一，其后是等你决定、待审查、失败与中断；组内按最近活动在前 */
export function inboxGroups(jobs: JobSummary[]): InboxGroup[] {
  const buckets: Record<InboxGroupKey, JobSummary[]> = { running: [], decide: [], review: [], broken: [] }
  for (const job of jobs) {
    const key = inboxGroupOf(job)
    if (key) buckets[key].push(job)
  }
  const titles: Record<InboxGroupKey, string> = { running: '正在执行', decide: '等你决定', review: '待审查', broken: '失败与中断' }
  return (Object.keys(buckets) as InboxGroupKey[]).map(key => ({ key, title: titles[key], jobs: sortJobs(buckets[key]) }))
}

/** 待审查组里闸门状态的展示：ok=闸门已过，fail=闸门未过，none=未跑闸门 */
export function gateState(job: Pick<JobSummary, 'gate_ok'>): 'ok' | 'fail' | 'none' {
  if (job.gate_ok === true) return 'ok'
  if (job.gate_ok === false) return 'fail'
  return 'none'
}
