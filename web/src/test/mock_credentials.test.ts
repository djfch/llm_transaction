/**
 * mockApi 凭证链路测试：create/update 是否把 thinking_effort 写进 config 内存态
 * （getConfig 回读校验）。防回归：真实后端 PUT 会更新凭证定义（routes_credentials.py），
 * mock 必须同构，否则 mock 预览模式下编辑思考程度会"保存成功但回显旧值"。
 */
import { describe, expect, it } from 'vitest'
import { mockApi } from '../api/mock'

describe('mockApi · 凭证 thinking_effort 链路', () => {
  it('create 落盘思考程度；update 修改思考程度后 getConfig 回读新值', async () => {
    await mockApi.createCredential({
      name: 'deep-main',
      provider: 'openai_compat',
      model: 'deepseek-v4-pro',
      max_tokens: 8192,
      openai_base_url: 'https://api.deepseek.com',
      thinking_effort: 'high',
    })

    const afterCreate = await mockApi.getConfig()
    const created = afterCreate.llm.credentials?.find((c) => c.name === 'deep-main')
    expect(created?.thinking_effort).toBe('high')

    await mockApi.updateCredential('deep-main', {
      provider: 'openai_compat',
      model: 'deepseek-v4-pro',
      max_tokens: 8192,
      openai_base_url: 'https://api.deepseek.com',
      thinking_effort: 'off',
    })

    const afterUpdate = await mockApi.getConfig()
    const updated = afterUpdate.llm.credentials?.find((c) => c.name === 'deep-main')
    expect(updated?.thinking_effort).toBe('off')
    expect(updated?.max_tokens).toBe(8192) // 其余字段不受影响
  })
})
