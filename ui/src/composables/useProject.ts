// 当前项目（侧栏选择），跨页面记在 localStorage；'' 表示全部项目
import { computed, ref } from 'vue'
import type { Overview, ProjectDef } from '../types'
import { jobInProject, projectEntries } from '../lib/jobs'
import type { JobSummary } from '../types'

const KEY = 'agent-dispatch-project'
const selected = ref<string>(localStorage.getItem(KEY) || '')

export function useCurrentProject() {
  function set(id: string) {
    selected.value = id
    localStorage.setItem(KEY, id)
  }
  return { selected, set }
}

/** 从总览里取当前项目的作业与定义（供所有页面复用） */
export function useProjectScope(overview: () => Overview | null) {
  const jobs = computed(() => {
    const o = overview()
    if (!o) return [] as JobSummary[]
    return o.jobs.filter(j => jobInProject(j, selected.value, o.definitions))
  })
  const definitions = computed<ProjectDef[]>(() => overview()?.definitions ?? [])
  const entries = computed(() => {
    const o = overview()
    return o ? projectEntries(o.jobs, o.definitions) : projectEntries([], [])
  })
  const currentName = computed(() => entries.value.find(e => e.id === selected.value)?.label ?? '全部项目')
  return { jobs, definitions, entries, currentName }
}
