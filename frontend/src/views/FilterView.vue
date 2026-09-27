<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api.js'

const router = useRouter()
const role = ref(localStorage.getItem('role') || '')
const username = ref(localStorage.getItem('user') || '')

// 前缀框：输入值与已生效前缀分离；只有点查询/切方案才把前缀提交到服务端
const prefix = ref('')
const appliedPrefix = ref('')
const jobs = ref([])
const schemes = ref([])
const history = ref([])
const err = ref('')

// 方案编辑区
const schemeName = ref('')
const schemeNote = ref('')
const selectedId = ref(null)

let timer

async function refreshJobs() {
  if (!localStorage.getItem('tok')) return
  try {
    const q = appliedPrefix.value ? '?prefix=' + encodeURIComponent(appliedPrefix.value) : ''
    jobs.value = await api('/api/jobs' + q)
    err.value = ''
  } catch (e) {
    err.value = String(e.message || e)
  }
}

async function refreshSchemes() {
  schemes.value = await api('/api/filter-schemes')
}

async function refreshHistory() {
  history.value = await api('/api/filter-history')
}

// 切换方案：立即回填并服务端重查
function selectScheme(s) {
  selectedId.value = s.id
  schemeName.value = s.name
  schemeNote.value = s.note
  prefix.value = s.prefix
  appliedPrefix.value = s.prefix
  refreshJobs()
}

function resetEditor() {
  selectedId.value = null
  schemeName.value = ''
  schemeNote.value = ''
}

async function saveScheme() {
  err.value = ''
  const name = schemeName.value.trim()
  if (!name) {
    err.value = '方案名不能为空'
    return
  }
  const p = prefix.value.trim()
  const payload = { name, prefix: p, note: schemeNote.value.trim() }
  // 保存即把当前前缀生效：方案存什么，结果表就按什么过滤
  appliedPrefix.value = p
  try {
    if (selectedId.value) {
      const saved = await api('/api/filter-schemes/' + selectedId.value, {
        method: 'POST',
        body: JSON.stringify(payload),
      })
      selectedId.value = saved.id
    } else {
      const saved = await api('/api/filter-schemes', {
        method: 'POST',
        body: JSON.stringify(payload),
      })
      selectedId.value = saved.id
    }
    await refreshSchemes()
    await refreshHistory()
    await refreshJobs()
  } catch (e) {
    err.value = String(e.message || e)
  }
}

function canDelete(s) {
  return role.value === 'writer' || s.created_by === username.value
}

async function deleteScheme(s) {
  err.value = ''
  if (!canDelete(s)) {
    err.value = '巡检不可删除他人方案'
    return
  }
  try {
    await api('/api/filter-schemes/' + s.id + '/delete', { method: 'POST' })
    if (selectedId.value === s.id) resetEditor()
    await refreshSchemes()
    await refreshHistory()
  } catch (e) {
    err.value = String(e.message || e)
  }
}

function applyPrefix() {
  appliedPrefix.value = prefix.value.trim()
  refreshJobs()
}

function actionLabel(a) {
  return { create: '新增', update: '变动', delete: '删除' }[a] || a
}

function fmtTime(t) {
  // ISO: 2026-09-27T10:20:30.123+00:00 -> 2026-09-27 10:20
  return t ? t.slice(0, 16).replace('T', ' ') : ''
}

function goDetail(id) {
  router.push(`/jobs/${id}`)
}

onMounted(async () => {
  role.value = localStorage.getItem('role') || ''
  username.value = localStorage.getItem('user') || ''
  await Promise.all([refreshJobs(), refreshSchemes().catch((e) => (err.value = String(e.message || e))), refreshHistory().catch(() => {})])
  timer = setInterval(refreshJobs, 2000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div>
    <p v-if="err" style="color:#b00020">{{ err }}</p>

    <section class="panel">
      <h3>过滤台 · 灯种前缀</h3>
      <div class="row">
        <label>前缀 <input v-model="prefix" placeholder="空前缀返回全部" @keyup.enter="applyPrefix" /></label>
        <button type="button" @click="applyPrefix">服务端查询</button>
        <span class="hint">过滤在服务端执行，空前缀返回全部行</span>
      </div>
    </section>

    <section class="panel">
      <h3>命名方案</h3>
      <div class="row">
        <label>方案名 <input v-model="schemeName" placeholder="如：方案甲" /></label>
        <label>方案说明
          <input v-model="schemeNote" class="note-input" placeholder="方案说明" />
        </label>
        <button type="button" @click="saveScheme">{{ selectedId ? '保存变动' : '保存新方案' }}</button>
        <button type="button" v-if="selectedId" @click="resetEditor">新建</button>
      </div>

      <table border="1" cellpadding="6" class="grid">
        <thead>
          <tr><th>方案名</th><th>前缀</th><th>说明</th><th>创建人</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="s in schemes" :key="s.id" :class="{ sel: s.id === selectedId }">
            <td><a href="#" @click.prevent="selectScheme(s)">{{ s.name }}</a></td>
            <td>{{ s.prefix || '（空）' }}</td>
            <td>{{ s.note }}</td>
            <td>{{ s.created_by }}</td>
            <td>
              <button type="button" @click="selectScheme(s)">切换</button>
              <button
                type="button"
                :disabled="!canDelete(s)"
                :title="canDelete(s) ? '删除方案' : '巡检不可删除他人方案'"
                @click="deleteScheme(s)"
              >删除</button>
            </td>
          </tr>
          <tr v-if="!schemes.length"><td colspan="5" class="hint">暂无方案</td></tr>
        </tbody>
      </table>
    </section>

    <section class="panel">
      <h3>结果表（服务端过滤，共 {{ jobs.length }} 行）</h3>
      <table border="1" cellpadding="6" class="grid">
        <thead>
          <tr>
            <th>编号</th><th>灯种</th><th>标称</th><th>实测</th><th>状态</th><th>结论</th><th>理由</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="j in jobs" :key="j.id" style="cursor:pointer" @click="goDetail(j.id)">
            <td>{{ j.id }}</td>
            <td>{{ j.lamp }}</td>
            <td>{{ j.nominal_nm }}</td>
            <td>{{ j.measured_nm }}</td>
            <td>{{ j.status }}</td>
            <td>{{ j.verdict }}</td>
            <td>{{ j.reason }}</td>
          </tr>
          <tr v-if="!jobs.length"><td colspan="7" class="hint">无匹配灯种</td></tr>
        </tbody>
      </table>
    </section>

    <section class="panel">
      <h3>变更履历</h3>
      <table border="1" cellpadding="6" class="grid">
        <thead>
          <tr><th>时间</th><th>方案</th><th>动作</th><th>前缀</th><th>说明</th><th>操作人</th></tr>
        </thead>
        <tbody>
          <tr v-for="h in history" :key="h.id">
            <td>{{ fmtTime(h.created_at) }}</td>
            <td>{{ h.scheme_name }}</td>
            <td>{{ actionLabel(h.action) }}</td>
            <td>{{ h.prefix || '（空）' }}</td>
            <td>{{ h.note }}</td>
            <td>{{ h.actor }}</td>
          </tr>
          <tr v-if="!history.length"><td colspan="6" class="hint">暂无履历</td></tr>
        </tbody>
      </table>
    </section>
  </div>
</template>

<style scoped>
.panel {
  margin: 16px 0;
  padding: 12px;
  border: 1px solid #ccc;
}
.row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.note-input {
  width: 260px;
}
.grid {
  border-collapse: collapse;
  width: 100%;
  margin-top: 10px;
}
.sel {
  background: #eef4ff;
}
.hint {
  color: #666;
  font-size: 13px;
}
button {
  cursor: pointer;
}
button:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}
</style>
