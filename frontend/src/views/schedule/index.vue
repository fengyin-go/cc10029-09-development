<template>
  <section class="page" data-module="schedule">
    <header class="page-head">
      <div><h2>定时调度</h2><p class="page-desc">配置采集调度的 Cron 表达式、执行窗口和失败处理策略。</p></div>
      <div class="page-actions">
        <button class="btn primary" type="button" data-testid="open-create" @click="openCreate">登记调度计划</button>
        <button class="btn" type="button" data-testid="export" @click="exportRows">导出作业清单</button>
      </div>
    </header>
    <div class="filter-bar">
      <label><span>开始日期</span><input type="date" v-model="dateFrom" /></label>
      <label><span>结束日期</span><input type="date" v-model="dateTo" /></label>
      <button class="btn" type="button" @click="reload">查询</button>
    </div>
    <div class="timeline-wrap">
      <div v-for="item in timelineItems" :key="item.id" class="timeline-row">
        <span class="timeline-label">{{ item.name || item.label }}</span>
        <div class="timeline-track">
          <div class="timeline-bar" :class="`status-${item.status}`" :style="{ left: barLeft(item) + '%', width: barWidth(item) + '%' }" :data-testid="`bar-${item.id}`" :title="`${item.start} ~ ${item.end}`" @click="selectItem(item)">&nbsp;</div>
        </div>
        <span style="font-size:12px;color:var(--muted);margin-left:8px;">{{ item.start }} ~ {{ item.end }}</span>
      </div>
      <p v-if="!timelineItems.length" style="text-align:center;color:var(--muted)">暂无作业记录</p>
    </div>
    <div v-if="selectedItem" class="timeline-log" data-testid="timeline-log">
      <strong>{{ selectedItem.name || selectedItem.label }}</strong> — 状态：{{ selectedItem.status }}
      <div style="margin-top:4px;">{{ selectedItem.logs || '暂无日志' }}</div>
    </div>

    <section class="batch-panel" data-testid="batch-panel">
      <header class="batch-head">
        <div>
          <h3>调度批次清单</h3>
          <p class="page-desc">按调度计划查看批次序列、执行窗口与当前状态，核对手动触发与定时触发是否合并。</p>
        </div>
        <div class="page-actions">
          <label class="batch-picker"><span>调度计划</span>
            <select v-model.number="selectedPlanId" data-testid="plan-select" @change="loadPlanBatches">
              <option v-for="plan in planOptions" :key="plan.id" :value="plan.id">{{ plan.计划编号 }} · {{ plan.关联任务 }}</option>
            </select>
          </label>
          <button class="btn" type="button" data-testid="sync-batches" @click="syncBatches">按时间推进状态</button>
          <button class="btn" type="button" data-testid="export-batches" @click="exportBatches">导出批次清单</button>
        </div>
      </header>

      <div class="batch-actions">
        <label class="batch-picker"><span>触发来源</span>
          <select v-model="triggerSource" data-testid="trigger-source">
            <option value="定时触发">定时触发</option>
            <option value="手动触发">手动触发</option>
          </select>
        </label>
        <label class="batch-picker"><span>批次数量</span>
          <input type="number" min="1" max="20" v-model.number="batchCount" data-testid="batch-count" />
        </label>
        <button class="btn primary" type="button" data-testid="generate-batches" @click="generateBatches">生成调度批次</button>
        <button class="btn" type="button" data-testid="generate-manual-duplicate" @click="generateManualDuplicate">模拟手动触发命中同一窗口</button>
      </div>

      <div class="stat-row" v-if="batchSummary">
        <article v-for="card in batchCards" :key="card.label" class="stat-card" data-testid="batch-stat">
          <span class="stat-label">{{ card.label }}</span>
          <strong class="stat-value">{{ card.value }}</strong>
        </article>
      </div>

      <table class="data-table batch-table">
        <thead>
          <tr>
            <th>批次编号</th><th>执行窗口</th><th>触发来源</th><th>状态</th><th>重试次数</th><th>校验结论</th><th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="batch in batchItems" :key="batch.id" :data-testid="`batch-row-${batch.id}`" :data-status="batch.状态">
            <td>{{ batch.批次编号 }}</td>
            <td>{{ batch.窗口开始 }} ~ {{ batch.窗口结束 }}</td>
            <td>
              <span data-testid="trigger-label">{{ batch.触发来源 }}</span>
              <span v-if="(batch.合并触发来源 || []).length > 1" class="merge-tag" data-testid="merged-tag">已合并</span>
            </td>
            <td><span class="status-chip" :data-status="batch.状态">{{ batch.状态 }}</span></td>
            <td>{{ batch.重试次数 }}</td>
            <td class="batch-conclusion">{{ batch.校验结论 }}</td>
            <td class="row-actions">
              <button
                v-for="action in batchActions"
                :key="action"
                class="link"
                type="button"
                :data-testid="`batch-action-${batch.id}-${action}`"
                @click="runBatchAction(action, batch)"
              >
                {{ action }}
              </button>
            </td>
          </tr>
          <tr v-if="!batchItems.length">
            <td colspan="7" class="empty-state">该调度计划暂无批次，可先生成调度批次</td>
          </tr>
        </tbody>
      </table>
      <p class="page-foot">
        <span>按时间顺序推进：待执行 → 执行中 → 已完成 / 失败，已完成批次不能重试回退。</span>
        <span v-if="batchMessage" data-testid="batch-message">{{ batchMessage }}</span>
        <span v-if="batchError" class="error-text" data-testid="batch-error">{{ batchError }}</span>
      </p>
    </section>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate"><h3>登记调度计划</h3>
        <label v-for="field in columns.slice(0,3)" :key="field" class="create-field"><span>{{ field }}</span><input v-model="createValues[field]" :placeholder="`请输入${field}`" required /></label>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
          <button class="btn" type="button" @click="showCreate=false">取消</button>
          <button class="btn primary" type="submit" data-testid="submit-create">提交</button>
        </div>
      </form>
    </div>
  </section>
</template>
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { request } from '@/api/client'
import { fetchJson } from '@/api/client'
const ENDPOINT = '/api/schedule'
const columns = ["计划编号", "关联任务", "Cron表达式", "执行窗口", "重试策略", "失败通知", "最大并发", "是否启用"]
const actions = ["提交校验", "启用配置", "停用配置"]
const batchActions = ["开始执行", "标记完成", "标记失败", "重试执行"]
const timelineItems = ref<any[]>([])
const selectedItem = ref<any>(null)
const dateFrom = ref(''); const dateTo = ref('')
const errorMessage = ref(''); const showCreate = ref(false)
const createValues = ref<Record<string,string>>({})
const allItems = ref<any[]>([])
const planOptions = ref<any[]>([])
const selectedPlanId = ref<number | null>(null)
const batchItems = ref<any[]>([])
const batchSummary = ref<any>(null)
const batchMessage = ref(''); const batchError = ref('')
const triggerSource = ref('定时触发'); const batchCount = ref(3)
function barLeft(item: any) { const all = allItems.value.filter(i => i.start && i.end); if(!all.length) return 0; const min = Math.min(...all.map(i => new Date(i.start).getTime())); const max = Math.max(...all.map(i => new Date(i.end).getTime())); const range = max - min || 86400000; return ((new Date(item.start).getTime() - min) / range) * 100 }
function barWidth(item: any) { const all = allItems.value.filter(i => i.start && i.end); if(!all.length) return 20; const min = Math.min(...all.map(i => new Date(i.start).getTime())); const max = Math.max(...all.map(i => new Date(i.end).getTime())); const range = max - min || 86400000; return Math.max(5, ((new Date(item.end).getTime() - new Date(item.start).getTime()) / range) * 100) }
function selectItem(item: any) { selectedItem.value = selectedItem.value?.id === item.id ? null : item }
const batchCards = computed(() => {
  const counts = batchSummary.value?.counts || {}
  return [
    { label: '批次总数', value: batchSummary.value?.total ?? 0 },
    { label: '待执行', value: counts['待执行'] ?? 0 },
    { label: '执行中', value: counts['执行中'] ?? 0 },
    { label: '已完成', value: counts['已完成'] ?? 0 },
    { label: '触发合并', value: batchSummary.value?.merged_count ?? 0 },
  ]
})
async function reload() {
  try { const res = await request(ENDPOINT); const data = await res.json(); allItems.value = data.items || []; applyFilter() } catch(e) { errorMessage.value = '加载失败' }
  try {
    const plans = await fetchJson<any>(`${ENDPOINT}/plans`)
    planOptions.value = plans.items || []
  } catch(e) { planOptions.value = allItems.value.map((item: any) => ({ id: item.id, 计划编号: `SCHE-${String(item.id).padStart(4, '0')}`, 关联任务: item.name, status: item.status })) }
  if (selectedPlanId.value === null && planOptions.value.length) {
    const enabled = planOptions.value.find((plan: any) => plan.status === '已启用') || planOptions.value[0]
    selectedPlanId.value = enabled.id
  }
  await loadPlanBatches()
}
function applyFilter() {
  let items = allItems.value
  if(dateFrom.value) items = items.filter((i: any) => i.start >= dateFrom.value)
  if(dateTo.value) items = items.filter((i: any) => i.end <= dateTo.value)
  timelineItems.value = items; selectedItem.value = null
}
async function loadPlanBatches() {
  if (selectedPlanId.value === null) return
  batchError.value = ''
  try {
    const payload = await fetchJson<any>(`${ENDPOINT}/${selectedPlanId.value}/batches`)
    batchItems.value = payload.items || []
    batchSummary.value = payload
  } catch(e: any) { batchItems.value = []; batchError.value = e.message || '批次加载失败' }
}
async function generateBatches(source?: unknown) {
  if (selectedPlanId.value === null) return
  const origin = typeof source === 'string' ? source : triggerSource.value
  batchError.value = ''; batchMessage.value = ''
  try {
    const res = await request(`${ENDPOINT}/batches?plan_id=${selectedPlanId.value}`, {
      method: 'POST',
      body: JSON.stringify({ values: { 触发来源: origin, 批次数量: batchCount.value } }),
    })
    const payload = await res.json()
    if (!payload.ok) throw new Error(payload.message || '生成调度批次失败')
    batchMessage.value = payload.message
    await loadPlanBatches()
  } catch(e: any) { batchError.value = e.message || '生成调度批次失败' }
}
function generateManualDuplicate() { return generateBatches('手动触发') }
async function runBatchAction(action: string, batch: any) {
  batchError.value = ''; batchMessage.value = ''
  try {
    const res = await request(`${ENDPOINT}/batches/${batch.id}/actions`, {
      method: 'POST', body: JSON.stringify({ action }),
    })
    const payload = await res.json()
    if (!payload.ok) throw new Error(payload.message || '批次动作未生效')
    batchMessage.value = payload.message
    await loadPlanBatches()
  } catch(e: any) { batchError.value = e.message || '批次动作未生效' }
}
async function syncBatches() {
  batchError.value = ''; batchMessage.value = ''
  try {
    const res = await request(`${ENDPOINT}/batches/sync`, { method: 'POST' })
    const payload = await res.json()
    if (!payload.ok) throw new Error(payload.message || '推进失败')
    batchMessage.value = payload.message
    await loadPlanBatches()
  } catch(e: any) { batchError.value = e.message || '推进失败' }
}
function exportBatches() {
  const query = selectedPlanId.value === null ? '' : `?plan_id=${selectedPlanId.value}`
  window.open(`${ENDPOINT}/batches/export${query}`, '_blank')
}
function openCreate() { createValues.value = {}; showCreate.value = true }
async function submitCreate() {
  try { const res = await request(ENDPOINT, { method: 'POST', body: JSON.stringify({ values: createValues.value }) }); if(!res.ok) throw new Error('创建失败'); showCreate.value = false; await reload() } catch(e: any) { errorMessage.value = e.message }
}
function exportRows() { window.open(`${ENDPOINT}/export`, '_blank') }
onMounted(reload)
</script>
