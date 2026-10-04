<script setup lang="ts">
// 「等你决定」答复区：显示问题原文，给答复续派 / 只标记已处理 / 驳回三条路。工作台和作业详情共用。
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'
import type { JobDetail } from '../types'

const props = defineProps<{ job: JobDetail }>()
const emit = defineEmits<{ done: [] }>()

const answer = ref('')
const rejectReason = ref('')
const busy = ref(false)
const error = ref('')
const step = ref<'answer' | 'reject'>('answer')

async function run(fn: () => Promise<{ ok: boolean; message?: string }>, okText: string) {
  busy.value = true
  error.value = ''
  try {
    const res = await fn()
    ElMessage.success(res.message || okText)
    emit('done')
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

const sendReply = () => run(() => api.jobAction(props.job.name, 'reply', { text: answer.value.trim() }), '答复已发送并续派')
const clearEscalation = () => run(() => api.jobAction(props.job.name, 'clear-escalation'), '已标记为已处理')
const doReject = () => run(() => api.jobAction(props.job.name, 'reject', { reason: rejectReason.value.trim() }), '作业已驳回')

function sendOrError() {
  if (!answer.value.trim()) {
    error.value = '答复内容为空'
    return
  }
  void sendReply()
}
function rejectOrError() {
  if (!rejectReason.value.trim()) {
    error.value = '驳回原因必填'
    return
  }
  void doReject()
}
</script>

<template>
  <div class="esc">
    <div class="h">主管的问题</div>
    <div class="q-text">{{ job.needs_user }}</div>
    <div class="sub label">你的答复（会作为续派内容发给执行者）</div>
    <el-input v-model="answer" type="textarea" :rows="3" placeholder="写给执行者的答复……" />
    <div class="btns">
      <el-button type="primary" :loading="busy" @click="sendOrError">发送答复并续派</el-button>
      <el-button :loading="busy" @click="clearEscalation">只标记为已处理</el-button>
      <el-button type="danger" plain @click="step = 'reject'">驳回作业</el-button>
    </div>
    <template v-if="step === 'reject'">
      <div class="sub label">驳回原因（必填）</div>
      <el-input v-model="rejectReason" type="textarea" :rows="2" placeholder="为什么驳回" />
      <div class="btns">
        <el-button type="danger" :loading="busy" @click="rejectOrError">确认驳回</el-button>
        <el-button @click="step = 'answer'">返回</el-button>
      </div>
    </template>
    <el-alert v-if="error" type="error" :closable="false" show-icon :title="error" class="mt" />
  </div>
</template>

<style scoped>
.esc { border: 1px solid var(--el-color-warning); border-radius: 8px; padding: 10px 12px; margin-bottom: 10px; background: var(--el-color-warning-light-9); display: flex; flex-direction: column; gap: 8px; }
html.dark .esc { background: var(--el-fill-color-dark); }
.h { font-weight: 600; }
.q-text { font-weight: 500; white-space: pre-wrap; word-break: break-word; }
.label { margin-top: 2px; }
.btns { display: flex; gap: 8px; flex-wrap: wrap; }
.mt { margin-top: 2px; }
</style>
