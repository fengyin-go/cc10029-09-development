<template>
  <section class="page" data-module="crawler">
    <header class="page-head">
      <div><h2>爬虫引擎</h2><p class="page-desc">管理爬虫任务的调度配置、并发度和批次参数，驱动采集引擎执行。</p></div>
      <div class="page-actions">
        <button class="btn primary" type="button" data-testid="open-create" @click="openCreate">登记爬虫任务</button>
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

    <section class="batch-panel" data-testid="dispatch-panel">
      <header class="batch-head">
        <div>
          <h3>待下发任务清单</h3>
          <p class="page-desc">消费调度批次清单生成，下发时间必须落在批次声明的执行窗口内。</p>
        </div>
        <div class="page-actions">
          <label class="batch-picker"><span>调度计划</span>
            <select v-model.number="selectedPlanId" data-testid="dispatch-plan-select" @change="loadDispatches">
              <option :value="null">全部计划</option>
              <option v-for="plan in planOptions" :key="plan.id" :value="plan.id">{{ plan.计划编号 }} · {{ plan.关联任务 }}</option>
            </select>
          </label>
          <button class="btn primary" type="button" data-testid="generate-dispatches" @click="generateDispatches">生成待下发任务</button>
          <button class="btn" type="button" data-testid="sync-conclusions" @click="syncConclusions">同步批次校验结论</button>
          <button class="btn" type="button" data-testid="export-dispatches" @click="exportDispatches">导出任务清单</button>
        </div>
      </header>

      <table class="data-table batch-table">
        <thead>
          <tr><th>任务清单编号</th><th>批次编号</th><th>执行窗口</th><th>下发时间</th><th>触发来源</th><th>状态</th><th>校验结论</th><th>可执行动作</th></tr>
        </thead>
        <tbody>
          <tr v-for="task in dispatches" :key="task.id" :data-testid="`dispatch-row-${task.id}`" :data-status="task.状态">
            <td>{{ task.任务清单编号 }}</td>
            <td>{{ task.批次编号 }}</td>
            <td>{{ task.窗口开始 }} ~ {{ task.窗口结束 }}</td>
            <td>{{ task.下发时间 }}</td>
            <td>
              <span data-testid="dispatch-trigger">{{ task.触发来源 }}</span>
              <span v-if="(task.合并触发来源 || []).length > 1" class="merge-tag">已合并</span>
            </td>
            <td><span class="status-chip" :data-status="task.状态">{{ task.状态 }}</span></td>
            <td class="batch-conclusion">{{ task.校验结论 }}</td>
            <td class="row-actions">
              <button class="link" type="button" :data-testid="`dispatch-action-${task.id}`" @click="dispatchTask(task)">下发任务</button>
            </td>
          </tr>
          <tr v-if="!dispatches.length">
            <td colspan="8" class="empty-state">暂无待下发任务，可在定时调度生成批次后同步生成</td>
          </tr>
        </tbody>
      </table>
      <p class="page-foot">
        <span>任务清单与调度批次同事务写入，批次未完整提交前不会出现在这里。</span>
        <span v-if="dispatchMessage" data-testid="dispatch-message">{{ dispatchMessage }}</span>
        <span v-if="dispatchError" class="error-text" data-testid="dispatch-error">{{ dispatchError }}</span>
      </p>
    </section>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate"><h3>登记爬虫任务</h3>
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
import { onMounted, ref } from 'vue'
import { fetchJson, request } from '@/api/client'
const ENDPOINT = '/api/crawler'
const columns = ["任务编号", "任务名称", "关联站点", "关联规则", "并发线程", "调度周期", "超时秒数", "下发批次"]
const actions = ["提交校验", "启用配置", "停用配置"]
const timelineItems = ref<any[]>([])
const selectedItem = ref<any>(null)
const dateFrom = ref(''); const dateTo = ref('')
const errorMessage = ref(''); const showCreate = ref(false)
const createValues = ref<Record<string,string>>({})
const allItems = ref<any[]>([])
const dispatches = ref<any[]>([])
const planOptions = ref<any[]>([])
const selectedPlanId = ref<number | null>(null)
const dispatchMessage = ref(''); const dispatchError = ref('')
function barLeft(item: any) { const all = allItems.value.filter(i => i.start && i.end); if(!all.length) return 0; const min = Math.min(...all.map(i => new Date(i.start).getTime())); const max = Math.max(...all.map(i => new Date(i.end).getTime())); const range = max - min || 86400000; return ((new Date(item.start).getTime() - min) / range) * 100 }
function barWidth(item: any) { const all = allItems.value.filter(i => i.start && i.end); if(!all.length) return 20; const min = Math.min(...all.map(i => new Date(i.start).getTime())); const max = Math.max(...all.map(i => new Date(i.end).getTime())); const range = max - min || 86400000; return Math.max(5, ((new Date(item.end).getTime() - new Date(item.start).getTime()) / range) * 100) }
function selectItem(item: any) { selectedItem.value = selectedItem.value?.id === item.id ? null : item }
async function reload() {
  try { const res = await request(ENDPOINT); const data = await res.json(); allItems.value = data.items || []; applyFilter() } catch(e) { errorMessage.value = '加载失败' }
  try {
    const plans = await fetchJson<any>('/api/schedule/plans')
    planOptions.value = plans.items || []
  } catch(e) { planOptions.value = [] }
  await loadDispatches()
}
async function loadDispatches() {
  dispatchError.value = ''
  const query = selectedPlanId.value === null ? '' : `?plan_id=${selectedPlanId.value}`
  try {
    const payload = await fetchJson<any>(`${ENDPOINT}/dispatches${query}`)
    dispatches.value = payload.items || []
  } catch(e: any) { dispatches.value = []; dispatchError.value = e.message || '任务清单加载失败' }
}
async function generateDispatches() {
  dispatchError.value = ''; dispatchMessage.value = ''
  try {
    const values: Record<string, unknown> = {}
    if (selectedPlanId.value !== null) values.计划ID = selectedPlanId.value
    const res = await request(`${ENDPOINT}/dispatches`, { method: 'POST', body: JSON.stringify({ values }) })
    const payload = await res.json()
    if (!payload.ok) throw new Error(payload.message || '生成待下发任务失败')
    dispatchMessage.value = payload.message
    await loadDispatches()
  } catch(e: any) { dispatchError.value = e.message || '生成待下发任务失败' }
}
async function syncConclusions() {
  dispatchError.value = ''; dispatchMessage.value = ''
  try {
    const res = await request(`${ENDPOINT}/dispatches/sync`, { method: 'POST' })
    const payload = await res.json()
    if (!payload.ok) throw new Error(payload.message || '同步失败')
    dispatchMessage.value = payload.message
    await loadDispatches()
  } catch(e: any) { dispatchError.value = e.message || '同步失败' }
}
async function dispatchTask(task: any) {
  dispatchError.value = ''; dispatchMessage.value = ''
  try {
    const res = await request(`${ENDPOINT}/dispatches/${task.id}/actions`, {
      method: 'POST', body: JSON.stringify({ action: '下发任务' }),
    })
    const payload = await res.json()
    if (!payload.ok) throw new Error(payload.message || '下发失败')
    dispatchMessage.value = payload.message
    await loadDispatches()
  } catch(e: any) { dispatchError.value = e.message || '下发失败' }
}
function exportDispatches() {
  const query = selectedPlanId.value === null ? '' : `?plan_id=${selectedPlanId.value}`
  window.open(`${ENDPOINT}/dispatches/export${query}`, '_blank')
}
function applyFilter() {
  let items = allItems.value
  if(dateFrom.value) items = items.filter((i: any) => i.start >= dateFrom.value)
  if(dateTo.value) items = items.filter((i: any) => i.end <= dateTo.value)
  timelineItems.value = items; selectedItem.value = null
}
function openCreate() { createValues.value = {}; showCreate.value = true }
async function submitCreate() {
  try { const res = await request(ENDPOINT, { method: 'POST', body: JSON.stringify({ values: createValues.value }) }); if(!res.ok) throw new Error('创建失败'); showCreate.value = false; await reload() } catch(e: any) { errorMessage.value = e.message }
}
function exportRows() { window.open(`${ENDPOINT}/export`, '_blank') }
onMounted(reload)
</script>
