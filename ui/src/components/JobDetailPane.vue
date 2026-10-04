<script setup lang="ts">
import { computed, nextTick, ref, toRef, watch } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api'
import { useJobDetail, useJobLog } from '../composables/useJob'
import { useSharedOverview } from '../composables/usePolling'
import EscalationBox from './EscalationBox.vue'
import JobActions from './JobActions.vue'
import { stateTag, statusTag } from '../lib/jobs'
import { formatDuration, relativeTime } from '../lib/time'
import type { JobEvent } from '../types'

const props = defineProps<{ name: string }>()
const nameRef = toRef(props, 'name')
const { data, offline, error, refresh } = useJobDetail(nameRef)
const job = computed(() => data.value)

const running = computed(() => job.value?.state === 'running')
const log = useJobLog(nameRef, running)

const tab = ref<'process' | 'output' | 'files' | 'brief' | 'decisions'>('process')

// ---- 过程：工具入参 / 返回折叠 ----
const expanded = ref(new Set<string>())
function toggle(key: string) {
  const s = new Set(expanded.value)
  s.has(key) ? s.delete(key) : s.add(key)
  expanded.value = s
}
function clip(text: string, limit = 4000): string {
  return text.length > limit ? text.slice(0, limit) + ' …（已截断）' : text
}
function valueText(v: unknown): string {
  return typeof v === 'string' ? v : JSON.stringify(v, null, 2) ?? ''
}
function eventKey(e: JobEvent, i: number): string { return `${e.t}-${i}` }

// ---- 实时输出：运行中自动滚到底，用户往上翻时不强滚 ----
const logBox = ref<HTMLElement | null>(null)
const stick = ref(true)
function onScroll() {
  const el = logBox.value
  if (el) stick.value = el.scrollHeight - el.scrollTop - el.clientHeight < 40
}
watch(() => log.state.value.text, async () => {
  if (stick.value && running.value) {
    await nextTick()
    const el = logBox.value
    if (el) el.scrollTop = el.scrollHeight
  }
})

// ---- 产物：文件列表 + 文本预览 ----
const preview = ref<{ path: string; text: string } | null>(null)
async function openFile(path: string) {
  const response = await fetch(api.jobFileUrl(props.name, path))
  preview.value = { path, text: (await response.text()).slice(0, 200_000) }
}
const TEXT_RE = /\.(md|json|jsonl|txt|log|sql|csv)$/i

const router = useRouter()
const sharedOverview = useSharedOverview()
// 用例判定：数据在总览接口里，按作业名过滤（有判定数据才显示）
const cases = computed(() => (sharedOverview.data.value?.cases ?? []).filter(c => c.job === props.name))
function gotoJob(name: string) { void router.push(`/jobs/${name}`) }
function gotoBatch(batch: string) { void router.push({ path: '/batches', query: { batch } }) }

const gate = computed(() => job.value?.gate ?? null)
const st = computed(() => (job.value ? stateTag(job.value) : null))
const sp = computed(() => statusTag(job.value?.status ?? null))
</script>

<template>
  <div v-if="!job" class="loading sub">{{ offline ? `加载失败：${error}，自动重试中……` : '加载中……' }}</div>
  <div v-else class="pane">
    <div class="head">
      <b class="name">{{ job.name }}</b>
      <el-tag v-if="st" size="small" :type="st.type">{{ st.label }}</el-tag>
      <el-tag v-if="sp" size="small" :type="sp.type" effect="plain">{{ sp.label }}</el-tag>
    </div>
    <div class="sub meta">
      {{ job.role_title || job.role || '-' }} · {{ job.project || '-' }} ·
      {{ job.model_alias || job.model || '-' }} · 第 {{ job.turn ?? '-' }} 轮 ·
      {{ formatDuration(job.seconds) }}<template v-if="job.batch"> · 批次 {{ job.batch }}</template> ·
      {{ relativeTime(job.updated || job.created) }}
    </div>

    <div class="sub meta">
      <template v-if="job.batch">所属事项：<a @click.prevent="gotoBatch(job.batch)" href="#">{{ job.batch_title || `（未命名）${job.batch}` }}</a> · </template>
      <template v-if="job.dispatcher">派工者：{{ job.dispatcher.kind }}{{ job.dispatcher.title ? ` · ${job.dispatcher.title}` : '' }} · </template>
      <template v-if="job.follows?.length">因 <a v-for="f in job.follows" :key="f" @click.prevent="gotoJob(f)" href="#">{{ f }}</a> 而派 · </template>
      <template v-if="job.followed_by?.length">后续：<a v-for="f in job.followed_by" :key="f" class="sub" @click.prevent="gotoJob(f)" href="#">{{ f }}</a> </template>
    </div>

    <EscalationBox v-if="job.needs_user" :job="job" @done="refresh" />

    <div class="box">
      <div class="h">结论
        <el-tag v-if="gate" size="small" :type="gate.ok ? 'success' : 'danger'">{{ gate.ok ? '闸门通过' : '闸门未过' }}</el-tag>
      </div>
      <template v-if="job.report && Object.keys(job.report).length">
        <div v-for="(v, k) in job.report" :key="k" class="kv">
          <span class="k">{{ k }}</span><span>{{ v || '—' }}</span>
        </div>
      </template>
      <div v-else class="sub">还没有结论{{ running ? '（运行中）' : '' }}</div>
      <div v-if="gate" class="gate">
        <div v-for="(c, i) in gate.checks" :key="i" class="gate-row">
          <span :class="c.ok ? 'g-ok' : 'g-bad'">{{ c.ok ? '✓' : '✗' }}</span>
          <span>{{ c.gate }}</span><span class="sub">{{ c.detail }}</span>
        </div>
      </div>
    </div>

    <div v-if="cases.length" class="box">
      <div class="h">用例判定 <span class="sub">PASS {{ cases.filter(c => c.verdict === 'PASS').length }} · FAIL {{ cases.filter(c => c.verdict === 'FAIL').length }} · BLOCKED {{ cases.filter(c => c.verdict === 'BLOCKED').length }}</span></div>
      <div v-for="(c, i) in cases" :key="i" class="case">
        <el-tag size="small" :type="({ PASS: 'success', FAIL: 'danger', BLOCKED: 'warning' } as Record<string, any>)[c.verdict] ?? 'info'">{{ c.verdict }}</el-tag>
        <b class="mono">{{ c.tc }}</b>
        <span class="sub">{{ c.evidence || '-' }}</span>
        <span v-if="c.note" class="sub">（{{ c.note }}）</span>
      </div>
    </div>

    <div class="box">
      <div class="h">下一步</div>
      <JobActions :job="job" @done="refresh" />
    </div>

    <el-tabs v-model="tab" class="tabs">
      <el-tab-pane label="过程" name="process">
        <div class="events">
          <template v-for="(e, i) in job.events" :key="eventKey(e, i)">
            <div v-if="e.kind === 'tool'" class="ev">
              <div class="ev-head">
                <span class="t sub">{{ e.t?.slice(11) }}</span>
                <span class="kind">工具</span> <b>{{ e.name }}</b>
                <span v-if="e.error" class="err">✗ 失败</span>
                <span v-if="e.ms != null" class="sub">{{ e.ms }}ms</span>
                <el-link type="primary" :underline="false" style="font-size:12px" @click="toggle(eventKey(e, i))">
                  {{ expanded.has(eventKey(e, i)) ? '收起' : '展开' }}
                </el-link>
              </div>
              <pre v-if="expanded.has(eventKey(e, i))" class="payload">{{ clip('入参: ' + valueText(e.input) + '\n\n返回: ' + valueText(e.output)) }}</pre>
            </div>
            <div v-else-if="e.kind === 'text'" class="ev">
              <div class="ev-head"><span class="t sub">{{ e.t?.slice(11) }}</span><span class="kind">回复</span></div>
              <div class="reply">{{ clip(e.text || '', 2000) }}</div>
            </div>
            <div v-else class="ev">
              <div class="ev-head"><span class="t sub">{{ e.t?.slice(11) }}</span><span class="kind err">错误</span></div>
              <div class="reply err">{{ e.message }}</div>
            </div>
          </template>
          <div v-if="!job.events?.length" class="sub">没有过程记录</div>
        </div>
      </el-tab-pane>

      <el-tab-pane label="实时输出" name="output">
        <div class="log-hint sub">
          {{ log.offline.value ? '日志拉取失败，自动重试中……' : `共 ${log.state.value.text.length} 字符` }}
          <span v-if="!stick && running">（已暂停自动滚动）</span>
        </div>
        <pre ref="logBox" class="log mono" @scroll="onScroll">{{ log.state.value.text || '（暂无输出）' }}</pre>
      </el-tab-pane>

      <el-tab-pane label="产物" name="files">
        <div v-if="!job.run_files?.length" class="sub">没有 run 目录或文件</div>
        <template v-else>
          <div class="files">
            <div v-for="f in job.run_files" :key="f.path" class="file" @click="TEXT_RE.test(f.path) && openFile(f.path)">
              <span class="mono">{{ f.path }}</span>
              <span class="sub">{{ (f.size / 1024).toFixed(1) }} KB</span>
            </div>
          </div>
          <pre v-if="preview" class="log mono">{{ preview.text }}</pre>
        </template>
      </el-tab-pane>

      <el-tab-pane label="派工单" name="brief">
        <pre class="log mono">{{ job.brief || '（没有派工单）' }}</pre>
      </el-tab-pane>

      <el-tab-pane label="裁决记录" name="decisions">
        <div v-if="!job.decisions?.length" class="sub">还没有裁决记录</div>
        <div v-for="(d, i) in job.decisions" :key="i" class="decision">
          <b>{{ d.action }}</b> <span class="sub">{{ d.at }} · 第 {{ d.turn }} 轮</span>
          <div v-if="d.note" class="sub">{{ d.note }}</div>
        </div>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.pane { display: flex; flex-direction: column; min-height: 0; height: 100%; overflow: auto; }
.loading { padding: 20px; }
.head { display: flex; align-items: center; gap: 8px; }
.head .name { font-size: 16px; }
.meta { margin: 2px 0 10px; }
.box { border: 1px solid var(--el-border-color); border-radius: 8px; padding: 10px 12px; margin-bottom: 10px; }
.h { font-weight: 600; margin-bottom: 6px; display: flex; justify-content: space-between; align-items: center; }
.kv { display: grid; grid-template-columns: 90px 1fr; gap: 2px 10px; font-size: 13px; margin-bottom: 4px; }
.kv .k { color: var(--el-text-color-secondary); }
.gate { margin-top: 6px; border-top: 1px dashed var(--el-border-color); padding-top: 6px; }
.gate-row { display: flex; gap: 8px; font-size: 13px; align-items: baseline; }
.g-ok { color: var(--el-color-success); }
.g-bad { color: var(--el-color-danger); }
.tabs { flex: 1; min-height: 0; }
.events { display: flex; flex-direction: column; gap: 6px; }
.ev { border: 1px solid var(--el-border-color-lighter); border-radius: 6px; padding: 6px 8px; }
.ev-head { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.kind { color: var(--el-color-primary); font-size: 12px; }
.err { color: var(--el-color-danger); }
.payload { margin: 6px 0 0; background: var(--el-fill-color-light); border-radius: 6px; padding: 8px; max-height: 300px; overflow: auto; white-space: pre-wrap; word-break: break-all; }
.reply { font-size: 13px; margin-top: 4px; white-space: pre-wrap; word-break: break-word; }
.log-hint { margin-bottom: 4px; }
.log { background: var(--el-fill-color-light); border-radius: 6px; padding: 8px 10px; max-height: 420px; overflow: auto; white-space: pre-wrap; word-break: break-all; margin: 0; }
.files { max-height: 220px; overflow: auto; margin-bottom: 8px; }
.file { display: flex; justify-content: space-between; gap: 10px; padding: 4px 6px; border-radius: 4px; cursor: pointer; }
.file:hover { background: var(--el-fill-color-light); }
.decision { border-bottom: 1px dashed var(--el-border-color-lighter); padding: 6px 2px; }
.meta a { color: var(--el-color-primary); cursor: pointer; }
.case { display: flex; align-items: baseline; gap: 8px; padding: 3px 0; font-size: 13px; flex-wrap: wrap; }
.h .sub { font-weight: 400; margin-left: 8px; }
</style>
