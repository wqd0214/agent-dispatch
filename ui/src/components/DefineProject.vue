<script setup lang="ts">
// 「这个项目还没有在 agent-dispatch 里定义」的说明条 + 定义为项目的对话框。
// 角色 / 工程 / 主管规则页共用；工程列表从该项目已有作业的工作目录去重后预填。
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api, projectApi } from '../api'
import { useSharedOverview } from '../composables/usePolling'
import { useCurrentProject, useProjectScope } from '../composables/useProject'
import { uniqueRepoSamples } from '../lib/roles'

const props = defineProps<{ projectId: string; text?: string }>()
const emit = defineEmits<{ defined: [id: string] }>()

const o = useSharedOverview()
const { currentName } = useProjectScope(() => o.data.value)
const { set: setProject } = useCurrentProject()

const open = ref(false)
const form = reactive({ id: '', name: '', repos: [] as { id: string; cwd: string }[] })
const submitting = ref(false)

const jobsInProject = computed(() => {
  const data = o.data.value
  if (!data) return [] as { project: string; name: string }[]
  return data.jobs.filter(j => j.group === props.projectId).map(j => ({ project: j.project, name: j.name }))
})

watch(open, async v => {
  if (!v) return
  form.id = props.projectId
  form.name = currentName.value && currentName.value !== '全部项目' ? currentName.value : props.projectId
  form.repos = []
  for (const sample of uniqueRepoSamples(jobsInProject.value)) {
    try {
      const detail = await api.jobDetail(sample.sample)
      form.repos.push({ id: sample.name, cwd: detail.cwd })
    } catch { /* 单个作业读不到目录就跳过，不拦着定义 */ }
  }
})

function msg(e: unknown): string { return e instanceof Error ? e.message : String(e) }

async function submit() {
  submitting.value = true
  try {
    await projectApi.add({
      id: form.id.trim(), name: form.name.trim() || undefined,
      repos: Object.fromEntries(form.repos.filter(r => r.id.trim() && r.cwd.trim()).map(r => [r.id.trim(), r.cwd.trim()])),
    })
    ElMessage.success(`已定义项目 ${form.id.trim()}`)
    open.value = false
    setProject(form.id.trim())
    emit('defined', form.id.trim())
  } catch (e) { ElMessage.error(msg(e)) } finally { submitting.value = false }
}
</script>

<template>
  <el-alert type="warning" :closable="false" show-icon class="undef">
    <template #title>
      {{ props.text ?? '这个项目还没有在 agent-dispatch 里定义，这里显示的是全局角色；定义之后才能为它单独配置' }}
      <el-button size="small" type="primary" plain style="margin-left: 10px" @click="open = true">定义为项目</el-button>
    </template>
  </el-alert>

  <el-dialog v-model="open" title="定义为项目" width="640px">
    <el-form label-width="90px">
      <el-form-item label="项目 ID" required>
        <el-input v-model="form.id" placeholder="如 wms，只能字母数字和 -_." />
      </el-form-item>
      <el-form-item label="显示名">
        <el-input v-model="form.name" placeholder="默认用下拉框里的名字" />
      </el-form-item>
      <el-form-item label="工程列表">
        <div class="sub" style="margin-bottom: 6px">从该项目已有作业的工作目录预填，可增删改</div>
        <div v-for="(r, i) in form.repos" :key="i" class="repo-row">
          <el-input v-model="r.id" placeholder="工程 ID" size="small" style="width: 140px" />
          <el-input v-model="r.cwd" placeholder="工作目录" size="small" class="mono" />
          <el-button size="small" type="danger" plain @click="form.repos.splice(i, 1)">删</el-button>
        </div>
        <el-button size="small" @click="form.repos.push({ id: '', cwd: '' })">加一个工程</el-button>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="open = false">取消</el-button>
      <el-button type="primary" :disabled="!form.id.trim()" :loading="submitting" @click="submit">定义</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.undef { margin-bottom: 12px; }
.repo-row { display: flex; gap: 6px; margin-bottom: 6px; }
.mono { font-family: var(--el-font-family-mono, monospace); font-size: 12px; }
.sub { color: var(--el-text-color-secondary); font-size: 12px; }
</style>
