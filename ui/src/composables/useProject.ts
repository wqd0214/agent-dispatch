// 当前项目（侧栏选择），跨页面记在 localStorage；'' 表示全部项目
import { computed, ref } from 'vue'
import type { Overview, ProjectDef } from '../types'
import { jobInProject, projectEntries } from '../lib/jobs'
import type { JobSummary } from '../types'

const KEY = 'agent-dispatch-project'
const selected = ref<string>(localStorage.getItem(KEY) || '')

// 地址参数 ?project=<项目ID> 可以直接选中项目（无头截图 / 分享链接用），优先于 localStorage
if (typeof location !== 'undefined') {
  const urlProject = new URLSearchParams(location.search).get('project')
  if (urlProject !== null && urlProject !== selected.value) {
    selected.value = urlProject
    localStorage.setItem(KEY, urlProject)
  }
}

export function useCurrentProject() {
  function set(id: string) {
    selected.value = id
    localStorage.setItem(KEY, id)
    // 把选择同步进地址栏，方便刷新 / 分享 / 无头截图时保持同一项目
    if (typeof location !== 'undefined') {
      const url = new URL(location.href)
      if (id) url.searchParams.set('project', id)
      else url.searchParams.delete('project')
      history.replaceState(null, '', url)
    }
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
