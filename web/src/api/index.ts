/**
 * API 入口：按开关选择真实后端（http.ts）或 mock 假数据（mock.ts）。
 * 默认走 mock，设置 VITE_USE_MOCK=false 后走真实 /api。
 */
import { httpApi } from './http'
import { mockApi } from './mock'
import type { ApiClient } from './types'

/** 是否使用 mock 数据（未设置 VITE_USE_MOCK 时默认 true，保证可独立预览） */
export const USE_MOCK = import.meta.env.VITE_USE_MOCK !== 'false'

export const api: ApiClient = USE_MOCK ? mockApi : httpApi

export { ApiError } from './http'
export type { ApiClient } from './types'
