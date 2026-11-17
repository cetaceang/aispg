<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { systemApi } from '@/api/system.ts'
import type { EgressResponse } from '@/types/system.ts'
import Button from '@/components/ui/Button.vue'
import { Globe, RefreshCw } from 'lucide-vue-next'

const egress = ref<EgressResponse | null>(null)
const loading = ref(false)
const error = ref('')

async function verify() {
  loading.value = true
  error.value = ''
  egress.value = null
  try {
    egress.value = await systemApi.getEgress()
  } catch (err: unknown) {
    error.value = err instanceof Error ? err.message : '出口验证失败'
  } finally {
    loading.value = false
  }
}

onMounted(() => { void verify() })
</script>

<template>
  <section class="bg-white border border-gray-200/80 rounded-2xl p-6 shadow-xs space-y-4">
    <div class="flex items-center justify-between gap-3">
      <h3 class="font-semibold text-gray-900 text-sm flex items-center gap-2">
        <Globe class="w-5 h-5 text-amber-600" /> Cloudflare WARP 出口
      </h3>
      <Button
        size="sm"
        variant="secondary"
        :loading="loading"
        @click="verify"
      >
        <RefreshCw class="w-4 h-4" /> 验证出口
      </Button>
    </div>
    <p
      v-if="error"
      role="alert"
      class="text-sm text-red-600"
    >
      {{ error }}；默认模式不会自动回落到直连。
    </p>
    <p
      v-else-if="loading"
      class="text-sm text-gray-500"
    >
      正在通过应用实际代理请求 Cloudflare，验证隧道与出口……
    </p>
    <div
      v-else-if="egress?.enabled"
      class="text-sm text-gray-700 space-y-2"
    >
      <p class="text-emerald-700 font-semibold">
        已实测：warp={{ egress.warp }}
      </p>
      <p class="font-mono">
        {{ egress.ip }} · {{ egress.loc }}
      </p>
      <p class="text-xs text-gray-500">
        默认本地 WAA 签名、Cookie 轮换与推理请求共享 WARP 出口。
      </p>
    </div>
    <p
      v-else-if="egress"
      class="text-sm text-amber-700"
    >
      内置 WARP 已被显式配置覆盖：{{ egress.mode === 'external_proxy' ? '外部代理' : '直连' }}。
    </p>
  </section>
</template>
