<script setup lang="ts">
// 工作台：按当前项目把要主管处理的事分四组（等你决定 / 待审查 / 失败与中断 / 运行中），
// 左边队列卡片，右边选中条目的处理区（复用作业详情组件）。
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useSharedOverview } from '../composables/usePolling'
import { useProjectScope } from '../composables/useProject'
import { gateState, inboxGroups } from '../lib/inbox'
import { relativeTime } from '../lib/time'
import JobDetailPane from '../components/JobDetailPane.vue'

const route = useRoute()
const overview = useSharedOverview()
const scope = useProjectScope(() => overview.data.value)
const groups = computed(() => inboxGroups(scope.jobs.value))
const total = computed(() => groups.value.reduce((n, g) => n + g.jobs.length, 0))

// 支持 ?job= 预选（便于深链和界面自查）；没有预选时默认选中第一张正在执行的单
const selected = ref(typeof route.query.job === 'string' ? route.query.job : '')
watch(groups, gs => {
  if (!selected.value && gs[0].jobs.length) selected.value = gs[0].jobs[0].name
  // 选中条目处理完离开分组后，自动选同组下一条，没了就收起处理区
  if (selected.value && !gs.some(g => g.jobs.some(j => j.name === selected.value))) selected.value = ''
})

function gateText(job: { gate_ok: boolean | null }): string {
  const s = gateState(job)
  return s === 'ok' ? '闸门已过' : s === 'fail' ? '闸门未过' : '未跑闸门'
}
</script>

<template>
  <div class="dash">
    <div class="queue">
      <div v-if="!total" class="empty sub">没有需要你处理的事</div>
      <div v-for="g in groups" v-show="g.jobs.length || g.key === 'running'" :key="g.key" class="group">
        <div class="g-head">
          <span>{{ g.title }}</span>
          <span class="cnt">{{ g.jobs.length }}</span>
          <span v-if="g.key === 'review'" class="sub">
            闸门已过 {{ g.jobs.filter(j => j.gate_ok === true).length }} / 未跑 {{ g.jobs.filter(j => j.gate_ok == null).length }}
          </span>
        </div>
        <div v-if="g.key === 'running' && !g.jobs.length" class="sub empty-run">现在没有正在执行的派工单</div>
        <div v-for="job in g.jobs" :key="job.name" class="card" :class="{ sel: job.name === selected }"
          @click="selected = job.name">
          <div class="row">
            <b class="name">{{ job.name }}</b>
            <span v-if="g.key === 'review'" class="gate" :class="gateState(job)">{{ gateText(job) }}</span>
            <span v-else-if="g.key === 'broken'" class="bad">{{ job.state_label }}</span>
            <span v-else-if="g.key === 'running'" class="run">{{ job.state_label }}</span>
          </div>
          <div class="sub">{{ job.role_title || job.role || '-' }} · {{ job.project || '-' }} · {{ relativeTime(job.updated || job.created) }}</div>
          <div v-if="job.needs_user" class="q">{{ job.needs_user }}</div>
          <div v-else-if="job.reason" class="sub reason">{{ job.reason }}</div>
          <div v-if="job.state === 'running' && job.activity" class="sub act mono">正在：{{ job.activity }}</div>
        </div>
      </div>
    </div>
    <div class="detail">
      <JobDetailPane v-if="selected" :name="selected" />
      <el-empty v-else :description="total ? '从左边选一件事来处理' : '没有需要你处理的事'" />
    </div>
  </div>
</template>

<style scoped>
.dash { display: grid; grid-template-columns: 380px 1fr; gap: 12px; height: 100%; min-height: 0; }
.queue { border: 1px solid var(--el-border-color); border-radius: 8px; background: var(--el-bg-color); padding: 10px; overflow: auto; }
.detail { border: 1px solid var(--el-border-color); border-radius: 8px; background: var(--el-bg-color); padding: 10px; overflow: hidden; min-width: 0; }
.empty { padding: 30px 4px; text-align: center; }
.empty-run { padding: 6px 4px 8px; text-align: center; border: 1px dashed var(--el-border-color); border-radius: 8px; margin-bottom: 8px; }
.group { margin-bottom: 12px; }
.g-head { display: flex; align-items: baseline; gap: 8px; font-weight: 600; margin-bottom: 6px; }
.cnt { background: var(--el-fill-color-dark); border-radius: 10px; padding: 0 7px; font-size: 11px; color: var(--el-text-color-secondary); }
.card { border: 1px solid var(--el-border-color); border-radius: 8px; padding: 8px 10px; margin-bottom: 8px; cursor: pointer; }
.card:hover { border-color: var(--el-color-primary-light-5); }
.card.sel { border-color: var(--el-color-primary); box-shadow: 0 0 0 2px var(--el-color-primary-light-8); }
.row { display: flex; align-items: center; gap: 8px; }
.name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.gate { font-size: 11px; margin-left: auto; color: var(--el-color-success); }
.gate.fail { color: var(--el-color-danger); }
.gate.none { color: var(--el-text-color-secondary); }
.bad { font-size: 11px; margin-left: auto; color: var(--el-color-danger); }
.run { font-size: 11px; margin-left: auto; color: var(--el-color-primary); }
.q { margin-top: 6px; padding: 6px 8px; border-radius: 6px; background: var(--el-color-warning-light-9); }
html.dark .q { background: var(--el-color-warning-dark-2); color: #fff; }
.reason { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.act { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
