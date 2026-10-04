<script setup lang="ts">
// 工作台：默认按事项（批次）分组，可切回按状态；组内先列正在执行的，再列待处理的。
// 顶部有通道占用小条（有通道被占用时才显示）。右侧是选中条目的处理区（复用作业详情组件）。
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useSharedOverview } from '../composables/usePolling'
import { useProjectScope } from '../composables/useProject'
import { gateState, inboxGroups } from '../lib/inbox'
import { busyChannels, topicGroups } from '../lib/topics'
import { relativeTime } from '../lib/time'
import JobDetailPane from '../components/JobDetailPane.vue'

const route = useRoute()
const router = useRouter()
const overview = useSharedOverview()
const scope = useProjectScope(() => overview.data.value)

// 分组方式：按事项 / 按状态；默认按事项，记在 localStorage
const MODE_KEY = 'agent-dispatch-inbox-mode'
const mode = ref<'topic' | 'state'>(localStorage.getItem(MODE_KEY) === 'state' ? 'state' : 'topic')
function setMode(m: 'topic' | 'state') {
  mode.value = m
  localStorage.setItem(MODE_KEY, m)
}

const groups = computed(() => inboxGroups(scope.jobs.value))
const topics = computed(() => topicGroups(scope.jobs.value))
const total = computed(() => groups.value.reduce((n, g) => n + g.jobs.length, 0))

// 通道占用：有被占用才显示，可折叠
const busy = computed(() => busyChannels(overview.data.value?.channels ?? []))
const showChannels = ref(true)

// 支持 ?job= 预选（便于深链和界面自查）；没有预选时默认选中第一张正在执行的单
const selected = ref(typeof route.query.job === 'string' ? route.query.job : '')
const listedNames = computed(() =>
  mode.value === 'topic'
    ? topics.value.flatMap(t => [...t.running, ...t.pending].map(j => j.name))
    : groups.value.flatMap(g => g.jobs.map(j => j.name)))
watch(listedNames, names => {
  if (!selected.value && names.length) selected.value = names[0]
  // 选中条目处理完离开分组后，自动选下一条，没了就收起处理区
  if (selected.value && !names.includes(selected.value)) selected.value = ''
})

function gateText(job: { gate_ok: boolean | null }): string {
  const s = gateState(job)
  return s === 'ok' ? '闸门已过' : s === 'fail' ? '闸门未过' : '未跑闸门'
}
function dispatcherText(d: { kind: string; title: string | null; session_id: string } | null): string {
  if (!d || (!d.kind && !d.session_id)) return '未登记'
  return `${d.kind}${d.title ? ` · ${d.title}` : ''}`
}
</script>

<template>
  <div class="dash">
    <div class="queue">
      <div v-if="busy.length && showChannels" class="chanbar">
        <b>通道占用</b>
        <span v-for="c in busy" :key="c.project + c.channel" class="chan">
          {{ c.project }}/{{ c.channel}} →
          <a @click.prevent="router.push(`/jobs/${c.holder}`)" href="#">{{ c.holder }}</a>
        </span>
        <el-button link size="small" class="fold" @click="showChannels = false">收起</el-button>
      </div>
      <el-button v-else-if="busy.length" link size="small" class="foldbar" @click="showChannels = true">
        {{ busy.length }} 个通道被占用，点开查看
      </el-button>

      <div class="mode">
        <el-radio-group :model-value="mode" size="small" @update:model-value="setMode($event as 'topic' | 'state')">
          <el-radio-button value="topic">按事项</el-radio-button>
          <el-radio-button value="state">按状态</el-radio-button>
        </el-radio-group>
      </div>

      <div v-if="!total" class="empty sub">没有需要你处理的事</div>

      <template v-if="mode === 'topic'">
        <div v-for="t in topics" :key="t.batch || '__none__'" class="group">
          <div class="g-head">
            <span class="t-title">{{ t.title || t.batch || '（未分批次）' }}</span>
            <span v-if="!t.title && t.batch" class="unnamed">未命名</span>
          </div>
          <div class="sub g-meta">
            <span v-if="t.reason" class="g-reason">{{ t.reason }}</span>
            <span>{{ dispatcherText(t.dispatcher) }} · 执行中 {{ t.running.length }} · 待处理 {{ t.pending.length }}
              <template v-if="t.doneCount"> · <a @click.prevent="router.push({ path: '/batches', query: t.batch ? { batch: t.batch } : {} })" href="#">已完成 {{ t.doneCount }}</a></template>
            </span>
          </div>
          <div v-for="job in [...t.running, ...t.pending]" :key="job.name" class="card" :class="{ sel: job.name === selected }"
            @click="selected = job.name">
            <div class="row">
              <b class="name">{{ job.name }}</b>
              <span v-if="job.needs_user" class="q-tag">待你决定</span>
              <span v-else-if="job.state === 'done'" class="gate" :class="gateState(job)">{{ gateText(job) }}</span>
              <span v-else-if="job.state === 'running'" class="run">{{ job.state_label }}</span>
              <span v-else class="bad">{{ job.state_label }}</span>
            </div>
            <div class="sub">{{ job.role_title || job.role || '-' }} · {{ job.project || '-' }} · {{ relativeTime(job.updated || job.created) }}</div>
            <div v-if="job.needs_user" class="q">{{ job.needs_user }}</div>
            <div v-else-if="job.reason" class="sub reason">{{ job.reason }}</div>
            <div v-if="job.state === 'running' && job.activity" class="sub act mono">正在：{{ job.activity }}</div>
          </div>
        </div>
      </template>

      <template v-else>
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
      </template>
    </div>
    <div class="detail">
      <JobDetailPane v-if="selected" :name="selected" />
      <el-empty v-else :description="total ? '从左边选一件事来处理' : '没有需要你处理的事'" />
    </div>
  </div>
</template>

<style scoped>
.dash { display: grid; grid-template-columns: 420px 1fr; gap: 12px; height: 100%; min-height: 0; }
.queue { border: 1px solid var(--el-border-color); border-radius: 8px; background: var(--el-bg-color); padding: 10px; overflow: auto; }
.detail { border: 1px solid var(--el-border-color); border-radius: 8px; background: var(--el-bg-color); padding: 10px; overflow: hidden; min-width: 0; }
.empty { padding: 30px 4px; text-align: center; }
.empty-run { padding: 6px 4px 8px; text-align: center; border: 1px dashed var(--el-border-color); border-radius: 8px; margin-bottom: 8px; }
.mode { margin-bottom: 8px; }
.group { margin-bottom: 12px; }
.g-head { display: flex; align-items: baseline; gap: 8px; font-weight: 600; margin-bottom: 2px; }
.t-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.unnamed { font-size: 11px; color: var(--el-text-color-secondary); border: 1px dashed var(--el-border-color); border-radius: 8px; padding: 0 6px; }
.g-meta { display: flex; flex-direction: column; gap: 1px; margin-bottom: 6px; }
.g-reason { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cnt { background: var(--el-fill-color-dark); border-radius: 10px; padding: 0 7px; font-size: 11px; color: var(--el-text-color-secondary); }
.chanbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; border: 1px solid var(--el-color-warning-light-5); border-radius: 8px; padding: 4px 8px; margin-bottom: 8px; font-size: 12px; }
.chan { color: var(--el-color-warning); }
.chan a { color: var(--el-color-primary); cursor: pointer; }
.fold { margin-left: auto; }
.foldbar { margin-bottom: 6px; }
.card { border: 1px solid var(--el-border-color); border-radius: 8px; padding: 8px 10px; margin-bottom: 8px; cursor: pointer; }
.card:hover { border-color: var(--el-color-primary-light-5); }
.card.sel { border-color: var(--el-color-primary); box-shadow: 0 0 0 2px var(--el-color-primary-light-8); }
.row { display: flex; align-items: center; gap: 8px; }
.name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.q-tag { font-size: 11px; margin-left: auto; color: var(--el-color-warning); }
.gate { font-size: 11px; margin-left: auto; color: var(--el-color-success); }
.gate.fail { color: var(--el-color-danger); }
.gate.none { color: var(--el-text-color-secondary); }
.bad { font-size: 11px; margin-left: auto; color: var(--el-color-danger); }
.run { font-size: 11px; margin-left: auto; color: var(--el-color-primary); }
.q { margin-top: 6px; padding: 6px 8px; border-radius: 6px; background: var(--el-color-warning-light-9); }
html.dark .q { background: var(--el-color-warning-dark-2); color: #fff; }
.reason { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.act { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
a { color: var(--el-color-primary); cursor: pointer; }
</style>
