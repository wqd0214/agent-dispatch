<script setup lang="ts">
// 作业操作区：按状态给出按钮，每个按钮各自的小表单对话框。作业详情和工作台共用。
import { computed, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, ApiError } from '../api'
import { availableActions, canAttach, gateHint } from '../lib/actions'
import { gateState } from '../lib/inbox'
import type { JobDetail } from '../types'

const props = defineProps<{ job: JobDetail }>()
const emit = defineEmits<{ done: [] }>()

const actions = computed(() => availableActions(props.job))
const attachable = computed(() => canAttach(props.job))
const attachCommand = computed(() => `agent-dispatch attach ${props.job.name}`)
const gst = computed(() => gateState(props.job))
const hint = computed(() => gateHint(gst.value))
const busy = ref(false)
const gateRunning = ref(false)
const gateResult = ref<{ passed: boolean; message: string } | null>(null)
const dlgError = ref('')

function copyAttach() {
  navigator.clipboard?.writeText(attachCommand.value).then(() => ElMessage.success('已复制接入命令'))
}

async function runGate() {
  gateRunning.value = true
  gateResult.value = null
  try {
    const res = await api.jobAction(props.job.name, 'gate')
    gateResult.value = { passed: !!res.passed, message: res.message || '' }
    emit('done')
  } catch (e) {
    gateResult.value = { passed: false, message: e instanceof Error ? e.message : String(e) }
  } finally {
    gateRunning.value = false
  }
}

// 每个操作一个对话框：dialog 记录当前打开的操作，form 是各表单字段
type DlgId = 'stop' | 'accept' | 'rework' | 'reject' | null
const dialog = ref<DlgId>(null)
const form = reactive({ text: '' })

function open(id: Exclude<DlgId, null>) {
  form.text = ''
  dlgError.value = ''
  dialog.value = id
}

async function submit(id: Exclude<DlgId, null>) {
  const text = form.text.trim()
  if ((id === 'rework' || id === 'reject') && !text) {
    dlgError.value = id === 'rework' ? '返工要求必填' : '驳回原因必填'
    return
  }
  busy.value = true
  dlgError.value = ''
  try {
    let res: { ok: boolean; message?: string }
    if (id === 'stop') res = await api.jobAction(props.job.name, 'stop', text ? { reason: text } : {})
    else if (id === 'accept') res = await api.jobAction(props.job.name, 'accept', text ? { note: text } : {})
    else if (id === 'rework') res = await api.jobAction(props.job.name, 'reply', { text })
    else res = await api.jobAction(props.job.name, 'reject', { reason: text })
    ElMessage.success(res.message || '操作成功')
    dialog.value = null
    emit('done')
  } catch (e) {
    dlgError.value = e instanceof ApiError || e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

const titles: Record<string, string> = { stop: '叫停作业', accept: '验收通过', rework: '返工续派', reject: '驳回作业' }
const confirms: Record<string, string> = { stop: '确认叫停', accept: '确认验收', rework: '发送并续派', reject: '确认驳回' }
const labels: Record<string, string> = { stop: '原因（可选）', accept: '备注（可选）', rework: '返工要求（必填）', reject: '驳回原因（必填）' }
const placeholders: Record<string, string> = {
  stop: '为什么不跑了',
  accept: '验收说明，会记进裁决记录',
  rework: '这次哪里不行、要改成什么样',
  reject: '为什么驳回',
}
</script>

<template>
  <div class="acts">
    <template v-if="job.state === 'held'">
      <div class="sub">人工持有中：作业被人为接管，客户端不提供操作。</div>
    </template>
    <template v-else-if="!actions.length && !job.needs_user">
      <div class="sub">这个状态没有可用的操作。</div>
    </template>
    <div class="btns">
      <el-button v-if="attachable" size="small" @click="copyAttach">复制接入命令</el-button>
      <el-button v-for="a in actions" :key="a.id" size="small" :type="a.kind === 'danger' ? 'danger' : a.kind === 'primary' ? 'primary' : 'default'"
        :plain="a.kind === 'default'" @click="open(a.id)">{{ a.label }}…</el-button>
    </div>
    <div v-if="attachable" class="attach sub mono">接入界面：<code>{{ attachCommand }}</code></div>
    <div v-if="hint" class="gate-line">
      <span class="sub">{{ hint }}</span>
      <el-button size="small" :loading="gateRunning" @click="runGate">跑闸门</el-button>
      <span v-if="gateResult" :class="gateResult.passed ? 'g-ok' : 'g-bad'">
        {{ gateResult.passed ? '闸门通过' : '闸门未过' }}{{ gateResult.message ? `：${gateResult.message.split('\n')[0]}` : '' }}
      </span>
    </div>

    <el-dialog :model-value="dialog !== null" :title="dialog ? titles[dialog] : ''" width="460px" append-to-body
      @update:model-value="(v: boolean) => !v && (dialog = null)">
      <template v-if="dialog">
        <el-alert v-if="dialog === 'stop'" type="warning" :closable="false" show-icon class="mb"
          title="叫停会中断正在运行的执行者，作业进入「已叫停」。" />
        <el-alert v-else-if="dialog === 'reject'" type="warning" :closable="false" show-icon class="mb"
          title="驳回后作业固定为「已驳回」，原因会记进裁决记录。" />
        <el-alert v-else-if="dialog === 'accept' && hint" type="warning" :closable="false" show-icon class="mb" :title="hint" />
        <div class="dlg-form">
          <div class="sub">{{ dialog ? labels[dialog] : '' }}</div>
          <el-input v-model="form.text" type="textarea" :rows="4" :placeholder="dialog ? placeholders[dialog] : ''" />
        </div>
        <el-alert v-if="dlgError" type="error" :closable="false" show-icon class="mt" :title="dlgError" />
      </template>
      <template #footer>
        <el-button @click="dialog = null">取消</el-button>
        <el-button :loading="busy" :type="dialog === 'stop' || dialog === 'reject' ? 'danger' : 'primary'"
          @click="dialog && submit(dialog)">{{ dialog ? confirms[dialog] : '' }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.acts { display: flex; flex-direction: column; gap: 6px; }
.btns { display: flex; gap: 8px; flex-wrap: wrap; }
.attach code { background: var(--el-fill-color-light); border-radius: 4px; padding: 1px 5px; }
.gate-line { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.g-ok { color: var(--el-color-success); font-size: 13px; }
.g-bad { color: var(--el-color-danger); font-size: 13px; }
.dlg-form { display: flex; flex-direction: column; gap: 6px; }
.mb { margin-bottom: 10px; }
.mt { margin-top: 10px; }
</style>
