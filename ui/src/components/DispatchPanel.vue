<script setup lang="ts">
// 派工面板：项目 → 角色卡片 → 工程（角色负责多个工程时）→ 任务 → 批次 → 作业名；
// 右侧任务停顿 500 毫秒后实时预览派工单、将用的后端模型、通道占用。
import { computed, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api } from '../api'
import { debounce } from '../lib/debounce'
import { useCurrentProject, useProjectScope } from '../composables/useProject'
import { useSharedOverview } from '../composables/usePolling'
import type { DispatchOptionProject, JobSummary } from '../types'

const visible = defineModel<boolean>({ default: false })
const router = useRouter()

const { selected: currentProject } = useCurrentProject()
const overview = useSharedOverview()
const scope = useProjectScope(() => overview.data.value)

const options = ref<DispatchOptionProject[] | null>(null)
const form = reactive({ project: '', role: '', repo: '', task: '', batch: '', job: '' })
const preview = ref<Record<string, unknown> | null>(null)
const previewError = ref('')
const previewing = ref(false)
const sending = ref(false)
const sendError = ref('')

const projects = computed(() => options.value ?? [])
const project = computed(() => projects.value.find(p => p.id === form.project) ?? null)
const role = computed(() => project.value?.roles.find(r => r.id === form.role) ?? null)
const needRepo = computed(() => (role.value?.repos.length ?? 0) > 1)

// 最近用过的批次：当前项目里出现过的批次，最近活动在前
const recentBatches = computed(() => {
  const seen = new Map<string, string>()
  for (const j of scope.jobs.value) if (j.batch) seen.set(j.batch, j.updated || j.created || '')
  return [...seen.entries()].sort((a, b) => b[1].localeCompare(a[1])).map(([b]) => b)
})

async function loadOptions() {
  if (options.value) return
  try {
    options.value = (await api.dispatchOptions()).projects
  } catch (e) {
    ElMessage.error(`加载派工选项失败：${e instanceof Error ? e.message : e}`)
  }
}

function reset() {
  form.project = currentProject.value && projects.value.some(p => p.id === currentProject.value) ? currentProject.value : (projects.value[0]?.id ?? '')
  form.role = ''
  form.repo = ''
  form.task = ''
  form.batch = ''
  form.job = ''
  preview.value = null
  previewError.value = ''
  sendError.value = ''
}

watch(visible, v => { if (v) void loadOptions().then(reset) }, { immediate: true })

// 预览参数齐了（选了角色、写了任务）才请求；任务输入停顿 500 毫秒
const body = computed(() => ({
  project: form.project, role: form.role, repo: needRepo.value ? form.repo : (role.value?.repos.length === 1 ? role.value.repos[0] : ''),
  batch: form.batch, job: form.job, task: form.task,
}))
const ready = computed(() => !!form.project && !!form.role && (!needRepo.value || !!form.repo) && !!form.task.trim())

const runPreview = debounce(async () => {
  if (!visible.value || !ready.value) return
  previewing.value = true
  previewError.value = ''
  try {
    preview.value = await api.dispatchPreview(body.value)
  } catch (e) {
    preview.value = null
    previewError.value = e instanceof Error ? e.message : String(e)
  } finally {
    previewing.value = false
  }
}, 500)
watch([body, ready], () => {
  if (!ready.value) {
    preview.value = null
    previewError.value = ''
    return
  }
  void runPreview()
}, { deep: true })

const busyChannels = computed(() => (preview.value?.channels_busy ?? {}) as Record<string, string>)

async function send() {
  sending.value = true
  sendError.value = ''
  try {
    const res = await api.dispatch(body.value)
    ElMessage.success(res.message || `已派出 ${res.job}`)
    visible.value = false
    void router.push(`/jobs/${res.job}`)
  } catch (e) {
    sendError.value = e instanceof Error ? e.message : String(e)
  } finally {
    sending.value = false
  }
}
</script>

<template>
  <el-dialog v-model="visible" title="派工" width="min(1060px, 94vw)" top="4vh" append-to-body class="dispatch-dlg">
    <div class="dispatch">
      <div class="left">
        <div class="step sub">① 项目</div>
        <el-select v-model="form.project" @change="form.role = ''; form.repo = ''">
          <el-option v-for="p in projects" :key="p.id" :value="p.id" :label="p.name" />
        </el-select>

        <div class="step sub">② 选角色</div>
        <div class="roles">
          <div v-for="r in project?.roles ?? []" :key="r.id" class="role" :class="{ on: form.role === r.id }"
            @click="form.role = r.id; form.repo = ''">
            <b>{{ r.title }}</b>
            <span class="sub">{{ r.repos.join(' / ') || '不限工程' }}</span>
            <span class="sub">{{ r.pooled ? '有模型池，按比例选模型' : '固定模型' }}</span>
          </div>
          <div v-if="project && !project.roles.length" class="sub">这个项目还没有定义角色</div>
        </div>
        <template v-if="needRepo">
          <div class="step sub">③ 工程（{{ role?.title }} 负责多个工程，选一个）</div>
          <el-select v-model="form.repo" placeholder="选工程">
            <el-option v-for="r in role?.repos ?? []" :key="r" :value="r" :label="r" />
          </el-select>
        </template>

        <div class="step sub">{{ needRepo ? '④' : '③' }} 任务（必填）</div>
        <el-input v-model="form.task" type="textarea" :rows="6" placeholder="要执行者做什么，写清楚验收标准……" />

        <div class="row">
          <div class="grow">
            <div class="sub">批次（可选）</div>
            <el-select v-model="form.batch" filterable allow-create clearable placeholder="选最近批次或输入新批次" size="small">
              <el-option v-for="b in recentBatches" :key="b" :value="b" :label="b" />
            </el-select>
          </div>
          <div class="grow">
            <div class="sub">作业名（可选，留空自动生成）</div>
            <el-input v-model="form.job" placeholder="如 tester-1004-1830" size="small" />
          </div>
        </div>
      </div>

      <div class="right">
        <div class="h sub">派工单预览（执行者实际收到的内容）</div>
        <div v-if="previewing" class="sub">预览中……</div>
        <el-alert v-else-if="previewError" type="error" :closable="false" show-icon :title="previewError" />
        <template v-else-if="preview">
          <div class="kv mono">
            <div><span class="k">作业名</span>{{ String(preview.job) }}</div>
            <div><span class="k">后端</span>{{ String(preview.backend) }}</div>
            <div><span class="k">模型</span>{{ preview.model_alias ? `${String(preview.model_alias)}（${String(preview.model)}）` : String(preview.model) }}</div>
            <div><span class="k">方式</span>{{ String(preview.mode) }}</div>
          </div>
          <div v-if="(preview.channels as string[])?.length" class="channels">
            <span v-for="ch in (preview.channels as string[])" :key="ch" class="ch" :class="{ busy: busyChannels[ch] }">
              {{ ch }}<template v-if="busyChannels[ch]">（被 {{ busyChannels[ch] }} 占用）</template>
            </span>
          </div>
          <div v-else class="sub">本次不占用通道。</div>
          <pre class="brief mono">{{ String(preview.brief) }}</pre>
        </template>
        <div v-else class="sub">选好角色、写完任务后这里会显示预览。</div>
      </div>
    </div>
    <el-alert v-if="sendError" type="error" :closable="false" show-icon :title="sendError" class="mt" />
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :disabled="!ready" :loading="sending" @click="send">派出</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.dispatch { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; max-height: 72vh; overflow: auto; }
.left, .right { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.step { margin-top: 6px; }
.roles { display: flex; flex-direction: column; gap: 6px; }
.role { border: 1px solid var(--el-border-color); border-radius: 8px; padding: 8px 10px; cursor: pointer; display: flex; flex-direction: column; gap: 2px; }
.role:hover { border-color: var(--el-color-primary-light-5); }
.role.on { border-color: var(--el-color-primary); box-shadow: 0 0 0 2px var(--el-color-primary-light-8); }
.row { display: flex; gap: 10px; }
.grow { flex: 1; min-width: 0; }
.h { margin-top: 2px; }
.kv { font-size: 12px; display: flex; flex-direction: column; gap: 2px; }
.kv .k { color: var(--el-text-color-secondary); margin-right: 8px; }
.channels { display: flex; gap: 6px; flex-wrap: wrap; }
.ch { border: 1px solid var(--el-color-success); color: var(--el-color-success); border-radius: 10px; padding: 0 8px; font-size: 12px; }
.ch.busy { border-color: var(--el-color-danger); color: var(--el-color-danger); }
.brief { background: var(--el-fill-color-light); border-radius: 6px; padding: 8px 10px; max-height: 320px; overflow: auto; white-space: pre-wrap; word-break: break-word; margin: 0; font-size: 12px; }
.mt { margin-top: 10px; }
</style>
