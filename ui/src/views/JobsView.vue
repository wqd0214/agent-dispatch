<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useSharedOverview } from '../composables/usePolling'
import { useProjectScope } from '../composables/useProject'
import { EMPTY_FILTERS, filterJobs, filterOptions, sortJobs, type JobFilters } from '../lib/jobs'
import { relativeTime } from '../lib/time'
import JobDetailPane from '../components/JobDetailPane.vue'

const route = useRoute()
const router = useRouter()
const overview = useSharedOverview()
const scope = useProjectScope(() => overview.data.value)

const filters = ref<JobFilters>({ ...EMPTY_FILTERS })
const options = computed(() => filterOptions(scope.jobs.value))
const stateOptions = computed(() => {
  // 从接口数据里取已知状态；接口没给全量映射时用固定表兜底
  const fixed = [['running', '运行中'], ['done', '待审查'], ['failed', '运行失败'], ['interrupted', '意外中断'], ['stopped', '已叫停'], ['held', '人工持有'], ['accepted', '已验收'], ['rejected', '已驳回']]
  const seen = new Map(scope.jobs.value.map(j => [j.state, j.state_label]))
  const all = new Map<string, string>()
  for (const [id, label] of fixed) if (!seen.size || seen.has(id)) all.set(id, label)
  for (const [id, label] of seen) if (!all.has(id)) all.set(id, label)
  return [...all.entries()]
})

const visible = computed(() => sortJobs(filterJobs(scope.jobs.value, filters.value)))
const selected = computed(() => (typeof route.params.name === 'string' ? route.params.name : ''))

function select(name: string) {
  router.push({ path: `/jobs/${name}`, query: { ...route.query } })
}
watch(() => scope.jobs.value, jobs => {
  // 选中的作业从列表里消失（改名/删除）时回到列表首屏
  if (selected.value && !jobs.some(j => j.name === selected.value) && jobs.length) router.replace('/jobs')
})
</script>

<template>
  <div class="jobs">
    <div class="list">
      <div class="filters">
        <el-select v-model="filters.states" multiple collapse-tags clearable placeholder="全部状态" size="small" class="f">
          <el-option v-for="[id, label] in stateOptions" :key="id" :value="id" :label="label" />
        </el-select>
        <el-select v-model="filters.role" clearable placeholder="全部角色" size="small" class="f">
          <el-option v-for="r in options.roles" :key="r.id" :value="r.id" :label="r.title" />
        </el-select>
        <el-select v-model="filters.batch" clearable placeholder="全部批次" size="small" class="f">
          <el-option v-for="b in options.batches" :key="b" :value="b" />
        </el-select>
        <el-select v-model="filters.project" clearable placeholder="全部工程" size="small" class="f">
          <el-option v-for="p in options.projects" :key="p" :value="p" />
        </el-select>
        <el-input v-model="filters.search" placeholder="搜作业名或任务" clearable size="small" class="f grow" />
      </div>
      <div class="count sub">共 {{ visible.length }} 个作业</div>
      <div class="items">
        <div v-if="!visible.length" class="sub empty">没有符合条件的作业</div>
        <div
          v-for="job in visible" :key="job.name"
          class="item" :class="{ sel: job.name === selected }" @click="select(job.name)"
        >
          <div class="row">
            <b class="name">{{ job.name }}</b>
            <el-tag size="small" :type="(job.needs_user ? 'warning' : { running: 'primary', done: 'warning', failed: 'danger', interrupted: 'danger', accepted: 'success' }[job.state] ?? 'info') as any" effect="light">
              {{ job.needs_user ? '待你决定' : job.state_label }}
            </el-tag>
          </div>
          <div class="sub">{{ job.role_title || job.role || '-' }} · {{ job.batch || job.status || '—' }} ·
            {{ relativeTime(job.updated || job.created) }}</div>
          <div v-if="job.state === 'running' && job.activity" class="sub act mono">正在：{{ job.activity }}</div>
          <div v-if="job.needs_user" class="q">{{ job.needs_user }}</div>
        </div>
      </div>
    </div>
    <div class="detail">
      <JobDetailPane v-if="selected" :name="selected" />
      <el-empty v-else description="从左边选择一个作业查看详情" />
    </div>
  </div>
</template>

<style scoped>
.jobs { display: grid; grid-template-columns: 340px 1fr; gap: 12px; height: 100%; min-height: 0; }
.list, .detail { display: flex; flex-direction: column; min-height: 0; border: 1px solid var(--el-border-color); border-radius: 8px; background: var(--el-bg-color); padding: 10px; overflow: hidden; }
.filters { display: flex; flex-wrap: wrap; gap: 6px; }
.f { width: 100px; }
.f.grow { flex: 1; min-width: 120px; }
.count { padding: 6px 2px 2px; }
.items { overflow: auto; flex: 1; }
.empty { padding: 20px 4px; }
.item { border: 1px solid var(--el-border-color); border-radius: 8px; padding: 8px 10px; margin-bottom: 8px; cursor: pointer; }
.item:hover { border-color: var(--el-color-primary-light-5); }
.item.sel { border-color: var(--el-color-primary); box-shadow: 0 0 0 2px var(--el-color-primary-light-8); }
.item .row { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.item .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.act { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.q { margin-top: 6px; padding: 6px 8px; border-radius: 6px; background: var(--el-color-warning-light-9); color: var(--el-text-color-primary); }
html.dark .q { background: var(--el-color-warning-dark-2); color: #fff; }
</style>
