/** K 线显示层固定采用 UTC+8；原始 Unix 时间戳始终保持不变。 */
const UTC8_OFFSET_SECONDS = 8 * 60 * 60

/**
 * 把 Unix 秒转换成 UTC+8 的日期部件。
 * 参数：time Unix 秒。
 * 返回：按 UTC getter 读取的 UTC+8 日期对象。
 */
function utc8Date(time: number): Date {
  return new Date((time + UTC8_OFFSET_SECONDS) * 1000)
}

/**
 * 补齐两位数字。
 * 参数：value 待格式化整数。
 * 返回：至少两位的数字字符串。
 */
function pad(value: number): string {
  return String(value).padStart(2, '0')
}

/**
 * 格式化十字光标的 UTC+8 完整时间。
 * 参数：time Unix 秒。
 * 返回：YYYY-MM-DD HH:mm。
 */
export function formatUtc8Crosshair(time: number): string {
  const date = utc8Date(time)
  return `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())} ${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}`
}

/**
 * 按 Lightweight Charts 刻度粒度格式化 UTC+8 短标签。
 * 参数：time Unix 秒；tickMarkType 刻度类型（0年/1月/2日/3时分/4含秒）。
 * 返回：不超过八字符的横轴标签。
 */
export function formatUtc8Tick(time: number, tickMarkType: number): string {
  const date = utc8Date(time)
  if (tickMarkType === 0) return String(date.getUTCFullYear())
  if (tickMarkType === 1) return `${pad(date.getUTCMonth() + 1)}月`
  if (tickMarkType === 2) return `${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}`
  const clock = `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}`
  return tickMarkType === 4 ? `${clock}:${pad(date.getUTCSeconds())}` : clock
}
