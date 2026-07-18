/**
 * API 入口：按开关选择真实后端（http.ts）或 mock 假数据（mock.ts）。
 * 默认走真实 /api（生产与开发经 vite proxy 均可用）；
 * 仅后端未就绪需独立预览时显式设 VITE_USE_MOCK=true 使用 mock。
 */
import { httpApi } from './http'
import { mockApi } from './mock'
import type { ApiClient } from './types'

/** 是否使用 mock 数据（默认 false：真实后端；显式 'true' 才用假数据预览） */
export const USE_MOCK = import.meta.env.VITE_USE_MOCK === 'true'

export const api: ApiClient = USE_MOCK ? mockApi : httpApi

export { ApiError } from './http'
export type { ApiClient } from './types'
