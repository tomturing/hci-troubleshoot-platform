<script setup lang="ts">
import { computed, ref } from 'vue'
import type { SignalsDoc } from '@/utils/kbdSignalTypes'

type Profile = NonNullable<SignalsDoc['semantic_entry_profile']>
const props = defineProps<{
  initial: Profile
  busy?: boolean
  previewInitiallyOpen?: boolean
  preview: (profile: Profile, context: Record<string, string>) => Promise<Record<string, any>>
}>()
const emit = defineEmits<{ save: [profile: Profile]; cancel: [] }>()
const draft = ref<Profile>(JSON.parse(JSON.stringify(props.initial)))
const expandedSections = ref<string[]>(props.previewInitiallyOpen ? ['preview'] : [])
const fields = [
  { key: 'canonical_symptoms', label: '标准症状：客户通常怎么说', hint: '每行一条完整现象，不写根因或修复方案。' },
  { key: 'positive_anchors', label: '正向锚点：至少命中一项关键信息', hint: '每行一个特征报错或错误码；当前不是所有条件同时满足。' },
  { key: 'exclusion_anchors', label: '排除锚点：出现就不适用', hint: '可留空，必须有原文依据；避免宽泛词。明确否定和已解决历史不会计作肯定命中。' },
  { key: 'manual_evidence_request', label: '请客户补充什么证据', hint: '仅人工指引时必填；不填写未审核命令。' },
  { key: 'clarifying_questions', label: '多篇都像时先问什么', hint: '每行一个有区分力的问题，运行时一次只问一组。' },
  { key: 'source_refs', label: '原文依据', hint: '例如 kbd:problem_description；保存后仍须人工核对原文内容。' },
] as const
const scopeFields = [
  { key: 'product', label: '产品' }, { key: 'product_version', label: '版本规则（如 >=6.12、6.*）' },
  { key: 'component', label: '组件' }, { key: 'object_type', label: '对象类型' }, { key: 'operation', label: '操作' },
] as const
const splitLines = (value: string) => [...new Set(value.split('\n').map(item => item.trim()).filter(Boolean))]
function updateField(key: typeof fields[number]['key'], value: string) { draft.value[key] = value.split('\n') }
function updateScope(key: typeof scopeFields[number]['key'], value: string) {
  draft.value.applicability = { ...draft.value.applicability, [key]: value.split('\n') }
}
function cleanDraft(): Profile {
  const result: Profile = JSON.parse(JSON.stringify(draft.value))
  for (const field of fields) result[field.key] = splitLines((result[field.key] || []).join('\n'))
  for (const field of scopeFields) if (result.applicability?.[field.key]) result.applicability[field.key] = splitLines(result.applicability[field.key]!.join('\n'))
  return result
}
const valid = computed(() => draft.value.canonical_symptoms.some(item => item.trim()) && draft.value.positive_anchors.some(item => item.trim()) &&
  (draft.value.diagnosis_capability !== 'guidance_only' || draft.value.manual_evidence_request?.some(item => item.trim())))
const context = ref({ description: '', error_text: '', product: '', product_version: '', component: '', object_type: '', operation: '' })
const result = ref<Record<string, any> | null>(null)
const previewing = ref(false)
const error = ref('')
async function runPreview() {
  error.value = ''; result.value = null; previewing.value = true
  try { result.value = await props.preview(cleanDraft(), context.value) }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '试运行失败' }
  finally { previewing.value = false }
}
</script>

<template>
  <el-alert type="info" :closable="false" title="画像只帮助选择候选，不执行命令、不产出变量，也不能证明根因。" />
  <el-form label-position="top" class="semantic-profile-form">
    <el-form-item label="当前平台能把排查做到哪一步">
      <el-select v-model="draft.diagnosis_capability">
        <el-option label="有现有消费者可以验证" value="executable" />
        <el-option label="只能请求人工补证据" value="guidance_only" />
        <el-option label="当前没有可信处理路径" value="capability_gap" />
      </el-select>
    </el-form-item>
    <el-form-item v-for="field in fields" :key="field.key" :label="field.label">
      <el-input :model-value="(draft[field.key] || []).join('\n')" type="textarea" :rows="2" :placeholder="field.hint" @update:model-value="updateField(field.key, $event)" />
      <small>{{ field.hint }}</small>
    </el-form-item>
    <el-collapse v-model="expandedSections">
      <el-collapse-item title="原文关联与正反例：随画像版本保存，发布时重新校验" name="validation">
        <p>原文引用会检查文本和摘要是否仍一致。自动关联只找逐字匹配；改写或缺少引用的内容仍需要人工核对。</p>
        <el-table :data="draft.source_evidence || []"><el-table-column prop="field_path" label="画像字段" /><el-table-column prop="source_ref" label="原文章节" /><el-table-column prop="quote" label="引用原文" /></el-table>
        <el-button v-if="result?.source_evidence" @click="draft.source_evidence = JSON.parse(JSON.stringify(result.source_evidence))">采用最近试运行的原文关联</el-button>
        <p>正反例仅校验文字筛选，不替代消费者证据和现场仿真。每次画像变更都会重新检查。</p>
        <div v-for="(example, index) in draft.routing_examples || []" :key="index">
          <el-input v-model="example.description" type="textarea" :rows="2" :maxlength="16000" placeholder="客户会怎样描述这个正例或近似反例" />
          <el-checkbox v-model="example.expected_match">应该进入候选（不是应该确认根因）</el-checkbox>
          <el-button @click="draft.routing_examples?.splice(index, 1)">删除此测试</el-button>
        </div>
        <el-button @click="(draft.routing_examples ||= []).push({ description: '', expected_match: false })">添加正例／近似反例</el-button>
      </el-collapse-item>
      <el-collapse-item title="适用范围：留空表示不限，填写后缺少对应客户信息会阻止执行" name="scope">
        <el-form-item v-for="field in scopeFields" :key="field.key" :label="field.label">
          <el-input :model-value="(draft.applicability?.[field.key] || []).join('\n')" type="textarea" :rows="1" placeholder="每行一个允许值；多行表示任选其一" @update:model-value="updateScope(field.key, $event)" />
        </el-form-item>
      </el-collapse-item>
      <el-collapse-item title="路由试运行：不会执行消费者，不代替发布或仿真验收" name="preview">
        <p>使用当前画像草稿和同分类已发布 KBD 比较。本次假设任务、告警、弹框均已确认未命中；不会采集这些信号，也不会保存画像或发布 KBD。</p>
        <el-alert v-if="!valid" type="warning" :closable="false" title="请先补齐上方标准症状、正向锚点；仅人工指引还需填写补充证据要求，才能试运行。" />
        <el-input v-model="context.description" type="textarea" :rows="3" :maxlength="16000" placeholder="输入正例或近似反例的客户描述" />
        <el-input v-model="context.error_text" placeholder="可选：独立报错文字" />
        <el-input v-for="field in scopeFields" :key="field.key" v-model="context[field.key]" :placeholder="`客户已确认的${field.label}`" />
        <el-button :disabled="!valid || !context.description.trim()" :loading="previewing" @click="runPreview">试运行当前草稿</el-button>
        <el-alert v-if="error" type="error" :closable="false" :title="error" />
        <template v-if="result">
          <el-alert :type="result.decision === 'executable' ? 'info' : 'warning'" :closable="false" :title="`${result.reason_text || result.reason}（不是根因结论）`" />
          <p v-if="result.next_action">下一步：{{ result.next_action.question }}</p>
          <p v-if="result.degraded">向量未使用，本次按文字分数降级。</p>
          <el-table :data="result.candidates || []">
            <el-table-column prop="support_id" label="案例" /><el-table-column prop="title" label="标题" />
            <el-table-column prop="score" label="排序分" /><el-table-column prop="matched_positive_anchors" label="命中信息" />
          </el-table>
          <el-table :data="result.filtered_candidates || []">
            <el-table-column prop="support_id" label="被过滤案例" /><el-table-column prop="reason_text" label="原因" />
          </el-table>
          <details><summary>完整路由解释（含修订和各项分数）</summary><pre>{{ JSON.stringify(result, null, 2) }}</pre></details>
        </template>
      </el-collapse-item>
    </el-collapse>
  </el-form>
  <div class="actions"><el-button @click="emit('cancel')">取消</el-button><el-button type="primary" :disabled="!valid" :loading="busy" @click="emit('save', cleanDraft())">保存画像</el-button></div>
</template>

<style scoped>
.semantic-profile-form { margin-top: 16px; }
small { color: var(--el-text-color-secondary); }
pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 360px; overflow: auto; }
.actions { display: flex; justify-content: flex-end; margin-top: 16px; }
</style>
