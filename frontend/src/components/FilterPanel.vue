<script setup>
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { api } from '../api.js'

const props = defineProps({ visible: { type: Boolean, default: false } })
const emit = defineEmits(['close'])

const role = ref(localStorage.getItem('role') || '')
const user = ref(localStorage.getItem('user') || '')
const prefix = ref('')
const name = ref('')
const description = ref('')
const schemes = ref([])
const events = ref([])
const jobs = ref([])
const selectedId = ref(null)
const err = ref('')
const msg = ref('')
let debounceTimer
let pollTimer

async function loadJobs() {
  const q = prefix.value.trim()
  jobs.value = await api('/api/jobs' + (q ? `?prefix=${encodeURIComponent(q)}` : ''))
}

async function loadSchemes() {
  schemes.value = await api('/api/filter/schemes')
}

async function loadEvents() {
  events.value = await api('/api/filter/events')
}

async function loadAll() {
  try {
    await Promise.all([loadJobs(), loadSchemes(), loadEvents()])
    err.value = ''
  } catch (e) {
    err.value = String(e.message || e)
  }
}

function onPrefixInput() {
  // 每次变更都重新向服务端查询，不在浏览器内藏行
  clearTimeout(debounceTimer)
  debounceTimer = setTimeout(async () => {
    try {
      await loadJobs()
      err.value = ''
    } catch (e) {
      err.value = String(e.message || e)
    }
  }, 300)
}

function clearPrefix() {
  prefix.value = ''
  onPrefixInput()
}

async function saveNew() {
  err.value = ''
  msg.value = ''
  try {
    await api('/api/filter/schemes', {
      method: 'POST',
      body: JSON.stringify({
        name: name.value,
        prefix: prefix.value,
        description: description.value,
      }),
    })
    msg.value = `方案「${name.value.trim()}」已保存`
    await Promise.all([loadSchemes(), loadEvents()])
  } catch (e) {
    err.value = String(e.message || e)
  }
}

async function updateSelected() {
  err.value = ''
  msg.value = ''
  if (!selectedId.value) return
  try {
    await api(`/api/filter/schemes/${selectedId.value}`, {
      method: 'PUT',
      body: JSON.stringify({
        name: name.value,
        prefix: prefix.value,
        description: description.value,
      }),
    })
    msg.value = `方案「${name.value.trim()}」已更新`
    await Promise.all([loadSchemes(), loadEvents()])
  } catch (e) {
    err.value = String(e.message || e)
  }
}

async function applyScheme(s) {
  err.value = ''
  msg.value = ''
  selectedId.value = s.id
  name.value = s.name
  description.value = s.description
  prefix.value = s.prefix
  try {
    await loadJobs()
  } catch (e) {
    err.value = String(e.message || e)
  }
}

async function removeScheme(s) {
  err.value = ''
  msg.value = ''
  try {
    await api(`/api/filter/schemes/${s.id}`, { method: 'DELETE' })
    if (selectedId.value === s.id) selectedId.value = null
    msg.value = `方案「${s.name}」已删除`
    await Promise.all([loadSchemes(), loadEvents()])
  } catch (e) {
    err.value = String(e.message || e)
  }
}

function canDelete(s) {
  return role.value === 'writer' || s.created_by === user.value
}

function actionLabel(a) {
  return { created: '新建', updated: '修改', deleted: '删除' }[a] || a
}

function startPolling() {
  stopPolling()
  pollTimer = setInterval(loadAll, 2000)
}

function stopPolling() {
  clearInterval(pollTimer)
  pollTimer = undefined
}

watch(
  () => props.visible,
  (v) => {
    if (v) {
      loadAll()
      startPolling()
    } else {
      stopPolling()
    }
  }
)

onMounted(() => {
  role.value = localStorage.getItem('role') || ''
  user.value = localStorage.getItem('user') || ''
  if (props.visible) {
    loadAll()
    startPolling()
  }
})
onUnmounted(stopPolling)
</script>

<template>
  <div v-show="visible" class="filter-panel">
    <div class="fp-head">
      <strong>灯种过滤台</strong>
      <span class="fp-hint">过滤在服务端执行，空前缀返回全部</span>
      <button type="button" class="fp-close" @click="emit('close')">收起</button>
    </div>
    <p v-if="err" class="fp-err">{{ err }}</p>
    <p v-if="msg" class="fp-ok">{{ msg }}</p>

    <section class="fp-controls">
      <label>前缀 <input v-model="prefix" placeholder="如：氦" @input="onPrefixInput" /></label>
      <label>方案名 <input v-model="name" placeholder="如：甲" /></label>
      <label>方案说明 <input v-model="description" placeholder="可选" /></label>
      <button type="button" @click="saveNew">保存方案</button>
      <button type="button" :disabled="!selectedId" @click="updateSelected">更新选中方案</button>
      <button type="button" @click="clearPrefix">空前缀</button>
    </section>

    <section class="fp-section">
      <h4>方案列表</h4>
      <table class="fp-table">
        <thead>
          <tr><th>方案名</th><th>前缀</th><th>说明</th><th>创建人</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="s in schemes" :key="s.id" :class="{ selected: s.id === selectedId }">
            <td>{{ s.name }}</td>
            <td>{{ s.prefix || '（空）' }}</td>
            <td>{{ s.description }}</td>
            <td>{{ s.created_by }}</td>
            <td>
              <button type="button" @click="applyScheme(s)">应用</button>
              <button
                type="button"
                :disabled="!canDelete(s)"
                :title="canDelete(s) ? '' : '巡检不可删他人方案'"
                @click="removeScheme(s)"
              >删除</button>
            </td>
          </tr>
          <tr v-if="!schemes.length"><td colspan="5" class="fp-empty">暂无方案</td></tr>
        </tbody>
      </table>
    </section>

    <section class="fp-section">
      <h4>结果表（{{ jobs.length }} 行）</h4>
      <table class="fp-table">
        <thead>
          <tr><th>编号</th><th>灯种</th><th>标称</th><th>实测</th><th>状态</th><th>结论</th><th>理由</th></tr>
        </thead>
        <tbody>
          <tr v-for="j in jobs" :key="j.id">
            <td>{{ j.id }}</td>
            <td>{{ j.lamp }}</td>
            <td>{{ j.nominal_nm }}</td>
            <td>{{ j.measured_nm }}</td>
            <td>{{ j.status }}</td>
            <td>{{ j.verdict }}</td>
            <td>{{ j.reason }}</td>
          </tr>
          <tr v-if="!jobs.length"><td colspan="7" class="fp-empty">无匹配行</td></tr>
        </tbody>
      </table>
    </section>

    <section class="fp-section">
      <h4>变更履历</h4>
      <table class="fp-table">
        <thead>
          <tr><th>时间</th><th>操作者</th><th>动作</th><th>方案</th><th>详情</th></tr>
        </thead>
        <tbody>
          <tr v-for="e in events" :key="e.id">
            <td>{{ e.created_at }}</td>
            <td>{{ e.actor }}</td>
            <td>{{ actionLabel(e.action) }}</td>
            <td>{{ e.scheme_name }}</td>
            <td>{{ e.detail }}</td>
          </tr>
          <tr v-if="!events.length"><td colspan="5" class="fp-empty">暂无履历</td></tr>
        </tbody>
      </table>
    </section>
  </div>
</template>

<style scoped>
.filter-panel {
  position: fixed;
  top: 44px;
  left: 12px;
  right: 12px;
  z-index: 99;
  max-height: calc(100vh - 60px);
  overflow: auto;
  background: #fff;
  border: 1px solid #9aa7b4;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.25);
  padding: 12px 16px;
  font-size: 14px;
}
.fp-head {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
}
.fp-hint {
  color: #666;
  font-size: 12px;
  flex: 1;
}
.fp-close {
  cursor: pointer;
}
.fp-err {
  color: #b00020;
  margin: 4px 0;
}
.fp-ok {
  color: #0a7a2f;
  margin: 4px 0;
}
.fp-controls {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  padding: 8px 0;
  border-top: 1px solid #ddd;
  border-bottom: 1px solid #ddd;
}
.fp-controls label {
  white-space: nowrap;
}
.fp-controls input {
  max-width: 140px;
}
.fp-section {
  margin-top: 10px;
}
.fp-section h4 {
  margin: 6px 0;
}
.fp-table {
  border-collapse: collapse;
  width: 100%;
}
.fp-table th,
.fp-table td {
  border: 1px solid #ccc;
  padding: 4px 8px;
  text-align: left;
}
.fp-table tr.selected td {
  background: #e8f1fb;
}
.fp-empty {
  color: #888;
  text-align: center;
}
</style>
