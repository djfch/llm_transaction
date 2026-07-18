// Vitest 全局 setup：
// 1. 注册 jest-dom 断言扩展（toBeInTheDocument 等）
// 2. vitest globals:false 时 @testing-library 不会自动清理，这里手动 cleanup
import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

afterEach(() => {
  cleanup()
})
