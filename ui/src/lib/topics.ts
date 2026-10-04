// 事项（批次）分组与 follows 关联树的纯函数（可单测）

import type { JobSummary } from '../types'
import { inboxGroupOf, type InboxGroupKey } from './inbox'

export interface DispatcherInfo {
  kind: string
  session_id: string
  title: string | null
}

export interface TopicGroup {
  batch: string
  title: string       // 事项标题；没有就空（界面退回批次名 + 未命名）
  reason: string
  dispatcher: DispatcherInfo | null
  running: JobSummary[]
  pending: JobSummary[]   // 等你决定 / 待审查 / 失败与中断
  doneCount: number       // 已验收 / 已驳回 / 已叫停，工作台不展开
}

const SETTLED_DONE = new Set(['accepted', 'rejected', 'stopped'])

function dispatcherOf(jobs: JobSummary[]): DispatcherInfo | null {
  for (const j of jobs) {
    if (j.dispatcher && (j.dispatcher.kind || j.dispatcher.session_id)) return j.dispatcher
  }
  return null
}

/** 工作台按事项分组：只列出有「正在执行 / 等你决定 / 待审查 / 失败与中断」单子的事项；有正在执行的排前面 */
export function topicGroups(jobs: JobSummary[]): TopicGroup[] {
  const byBatch = new Map<string, TopicGroup>()
  for (const job of jobs) {
    const key = job.batch || ''
    const key2 = inboxGroupOf(job)
    if (!key2) {
      if (SETTLED_DONE.has(job.state) || job.state === 'held') {
        const g = byBatch.get(key)
        if (g) g.doneCount += 1
      }
      continue
    }
    let group = byBatch.get(key)
    if (!group) {
      group = { batch: key, title: job.batch_title || '', reason: job.batch_reason || '', dispatcher: null,
               running: [], pending: [], doneCount: 0 }
      byBatch.set(key, group)
    }
    if (!group.title && job.batch_title) group.title = job.batch_title
    if (!group.reason && job.batch_reason) group.reason = job.batch_reason
    if (key2 === 'running') group.running.push(job)
    else group.pending.push(job)
  }
  const groups = [...byBatch.values()].filter(g => g.running.length || g.pending.length)
  for (const g of groups) {
    g.dispatcher = dispatcherOf([...g.running, ...g.pending])
  }
  // 有正在执行的事项排前面；其后按最近活动（组内最大 updated）
  const activity = (g: TopicGroup) => [...g.running, ...g.pending].reduce((m, j) => {
    const t = j.updated || j.created || ''
    return t > m ? t : m
  }, '')
  return groups.sort((a, b) =>
    (b.running.length > 0 ? 1 : 0) - (a.running.length > 0 ? 1 : 0) || activity(b).localeCompare(activity(a)))
}

export interface FollowsNode {
  job: JobSummary
  depth: number        // 0 = 顶层；每跟随一层加一
  follows: string[]    // 它跟随的作业名（可能不在列表里）
}

/** 按派出顺序排成树：带 follows 的单子缩进排在它跟随的单子下面 */
export function followsTree(jobs: JobSummary[]): FollowsNode[] {
  const byName = new Map(jobs.map(j => [j.name, j]))
  const ordered = [...jobs].sort((a, b) => (a.created || '').localeCompare(b.created || '') || a.name.localeCompare(b.name))
  const childrenOf = new Map<string, JobSummary[]>()
  const followed = new Set<string>()
  for (const job of ordered) {
    const parent = (job.follows || []).find(f => byName.has(f))
    if (parent) {
      childrenOf.set(parent, [...(childrenOf.get(parent) || []), job])
      followed.add(job.name)
    }
  }
  const out: FollowsNode[] = []
  const walk = (job: JobSummary, depth: number) => {
    out.push({ job, depth, follows: job.follows || [] })
    for (const child of childrenOf.get(job.name) || []) walk(child, depth + 1)
  }
  for (const job of ordered) if (!followed.has(job.name)) walk(job, 0)
  return out
}

/** 事项一行的状态计数（与后端口径一致：held 算执行中，interrupted 算失败） */
export function topicCounts(jobs: JobSummary[]): { running: number; review: number; done: number; failed: number } {
  const counts = { running: 0, review: 0, done: 0, failed: 0 }
  for (const j of jobs) {
    if (j.state === 'running' || j.state === 'held') counts.running += 1
    else if (j.state === 'done') counts.review += 1
    else if (SETTLED_DONE.has(j.state)) counts.done += 1
    else counts.failed += 1
  }
  return counts
}

/** 通道占用：从总览的 channels 里挑出被占着的（通道名 → 占用作业名），按工程分组 */
export interface BusyChannel { project: string; channel: string; holder: string }

export function busyChannels(channels: { project: string; table: Record<string, string | null> }[]): BusyChannel[] {
  const out: BusyChannel[] = []
  for (const c of channels) {
    for (const [channel, holder] of Object.entries(c.table || {})) {
      if (holder) out.push({ project: c.project, channel, holder })
    }
  }
  return out
}

export type { InboxGroupKey }
