<template>
  <section class="page" data-module="drift">
    <header class="page-head">
      <div>
        <h2>数据漂移检测管理</h2>
        <p class="page-desc">
          同一字段的连续采样按表、字段与采样窗口归并：保留首次偏差、最近偏差、样本数与结论版本，
          只有跨过噪声阈值（20%）的变化才推进结论，重复采样幂等忽略。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="showSampleForm = !showSampleForm">登记采样样本</button>
        <button class="btn" type="button" @click="runMerge">归并采样窗口</button>
        <button class="btn" type="button" @click="locateNext">定位下一组</button>
        <button class="btn" type="button" @click="toggleHistory">采样历史</button>
        <button class="btn" type="button" @click="exportRows">导出清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form v-if="showSampleForm" class="filter-bar sample-form" @submit.prevent="submitSample(false)">
      <label v-for="field in sampleFields" :key="field.key" class="filter-item">
        <span>{{ field.label }}</span>
        <input
          v-model="sampleForm[field.key]"
          :type="field.type"
          :placeholder="field.placeholder"
        />
      </label>
      <button class="btn primary" type="submit">登记并归并</button>
      <button class="btn" type="button" @click="submitSample(true)">仅暂存样本</button>
    </form>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>记录编号</span>
        <input v-model="filters.keyword" placeholder="按记录编号检索" />
      </label>
      <label class="filter-item">
        <span>漂移字段</span>
        <input v-model="filters.field" placeholder="按漂移字段检索" />
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="filters.status">
          <option value="">全部状态</option>
          <option v-for="status in statuses" :key="status" :value="status">{{ status }}</option>
        </select>
      </label>
      <label class="filter-item">
        <span>检测时间起</span>
        <input v-model="filters.window_start" type="date" />
      </label>
      <label class="filter-item">
        <span>检测时间止</span>
        <input v-model="filters.window_end" type="date" />
      </label>
      <label class="filter-item checkbox-item">
        <input v-model="filters.hide_noise" type="checkbox" />
        <span>过滤噪声（保留已确认历史偏差）</span>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="row in rows"
          :key="String(row.id)"
          :class="{ 'located-row': row['记录编号'] === locatedCode }"
        >
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无数据漂移检测数据，可先登记采样样本</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条数据漂移检测记录</span>
      <span v-if="infoMessage" class="info-text">{{ infoMessage }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <section v-if="showHistory" class="history-panel">
      <h3>采样历史（含噪声样本与原基线，收口证据不重复累计）</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in historyColumns" :key="column">{{ column }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="sample in historyRows" :key="String(sample.id)">
            <td v-for="column in historyColumns" :key="column">
              {{ formatHistoryCell(sample, column) }}
            </td>
          </tr>
          <tr v-if="!historyRows.length">
            <td :colspan="historyColumns.length" class="empty-state">暂无采样历史</td>
          </tr>
        </tbody>
      </table>
    </section>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/drift'
const columns = [
  '记录编号', '表名称', '漂移字段', '历史均值', '当前数值', '偏差比例',
  '首次偏差', '最近偏差', '样本数', '结论版本', '结论', '检测时间',
]
const historyColumns = [
  '样本编号', '表名称', '漂移字段', '历史均值', '当前数值', '偏差比例',
  '采样窗口', '检测时间', '是否噪声', '窗口状态', '归属记录编号',
]
const actions = ['确认漂移', '处理漂移', '忽略漂移']
const statuses = ['已记录', '已确认', '已处理', '已忽略']
const sampleFields = [
  { key: '表名称', label: '表名称', type: 'text', placeholder: '如 库存台账表' },
  { key: '漂移字段', label: '漂移字段', type: 'text', placeholder: '如 库存数量' },
  { key: '历史均值', label: '历史均值', type: 'text', placeholder: '无冻结基线时使用' },
  { key: '当前数值', label: '当前数值', type: 'text', placeholder: '必填，数字' },
  { key: '检测时间', label: '检测时间', type: 'datetime-local', placeholder: '' },
]

const rows = ref<Row[]>([])
const total = ref(0)
const stats = ref([
  { label: '漂移记录总数', value: 0 },
  { label: '未收口记录', value: 0 },
  { label: '真实漂移记录', value: 0 },
  { label: '噪声记录', value: 0 },
])
const filters = ref<Record<string, string | boolean>>({})
const sampleForm = ref<Record<string, string>>({})
const showSampleForm = ref(false)
const showHistory = ref(false)
const historyRows = ref<Row[]>([])
const locatedCode = ref('')
const lastAnchor = ref('')
const infoMessage = ref('')
const errorMessage = ref('')

function buildQuery(): string {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(filters.value)) {
    if (value === '' || value === false || value == null) continue
    params.set(key, value === true ? 'true' : String(value))
  }
  return params.toString()
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  infoMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message || '数据漂移检测动作未生效')
    }
    infoMessage.value = payload.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据漂移检测操作失败'
  }
}

async function submitSample(stageOnly: boolean) {
  errorMessage.value = ''
  infoMessage.value = ''
  const values: Record<string, string> = {}
  for (const field of sampleFields) {
    const value = (sampleForm.value[field.key] || '').trim()
    if (value) values[field.key] = value
  }
  try {
    const response = await request(stageOnly ? `${ENDPOINT}/samples` : ENDPOINT, {
      method: 'POST',
      body: JSON.stringify({ values }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message || '采样样本登记失败')
    }
    infoMessage.value = payload.message
    sampleForm.value = {}
    showSampleForm.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采样样本登记失败'
  }
}

async function runMerge() {
  errorMessage.value = ''
  infoMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/merge`, { method: 'POST' })
    const payload = await response.json()
    infoMessage.value = payload.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采样窗口归并失败'
  }
}

async function locateNext() {
  errorMessage.value = ''
  infoMessage.value = ''
  const query = buildQuery()
  const params = new URLSearchParams(query)
  if (lastAnchor.value) params.set('after', lastAnchor.value)
  try {
    const response = await request(`${ENDPOINT}/locate?${params.toString()}`)
    const payload = await response.json()
    infoMessage.value = payload.message
    if (payload.ok && payload.entry) {
      locatedCode.value = payload.entry['记录编号']
      lastAnchor.value = payload.entry['记录编号']
    } else {
      locatedCode.value = ''
      lastAnchor.value = ''
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '定位失败'
  }
}

async function toggleHistory() {
  showHistory.value = !showHistory.value
  if (!showHistory.value) return
  try {
    const response = await request(`${ENDPOINT}/history?size=200`)
    const payload = await response.json()
    historyRows.value = payload.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采样历史读取失败'
  }
}

function formatHistoryCell(sample: Row, column: string): string {
  const value = sample[column]
  if (value == null || value === '') return '—'
  if (typeof value === 'boolean') return value ? '是' : '否'
  return String(value)
}

async function refreshStats() {
  try {
    const response = await request(`${ENDPOINT}?size=200`)
    const payload = await response.json()
    const all: Row[] = payload.items ?? []
    stats.value = [
      { label: '漂移记录总数', value: payload.total ?? all.length },
      { label: '未收口记录', value: all.filter((row) => row.status === '已记录').length },
      { label: '真实漂移记录', value: all.filter((row) => row['结论'] === '真实漂移').length },
      { label: '噪声记录', value: all.filter((row) => row['结论'] === '采样噪声').length },
    ]
  } catch {
    // 统计卡片失败不阻塞列表
  }
}

async function reload() {
  errorMessage.value = ''
  locatedCode.value = ''
  lastAnchor.value = ''
  try {
    const response = await request(`${ENDPOINT}?${buildQuery()}`)
    if (!response.ok) {
      throw new Error('漂移记录列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    await refreshStats()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据漂移检测列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.located-row td {
  background: #e8f0fe;
}
.info-text {
  color: #067647;
}
.sample-form {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
}
.checkbox-item {
  flex-direction: row;
  align-items: center;
  gap: 6px;
}
.history-panel {
  margin-top: 16px;
}
.history-panel h3 {
  font-size: 14px;
  margin: 0 0 8px;
}
</style>
