/**
 * 密钥配置状态徽标：只显示"已配置/未配置"，不提供输入框（密钥仅经 .env 配置）。
 */
import type { SecretsStatus } from '../../api/types'
import Badge from '../../components/Badge'

const ITEMS: Array<{ key: keyof SecretsStatus; label: string }> = [
  { key: 'gate_key', label: 'gate_key(交易所 API Key)' },
  { key: 'llm_key', label: 'llm_key(LLM API Key)' },
  { key: 'telegram', label: 'telegram(Telegram Bot Token)' },
]

export default function SecretsBadges({ status }: { status: SecretsStatus }) {
  return (
    <div>
      <ul className="flex flex-wrap gap-3">
        {ITEMS.map(({ key, label }) => (
          <li
            key={key}
            className="flex items-center gap-2 rounded-lg border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-300"
          >
            {label}
            <Badge
              text={status[key] ? '已配置' : '未配置'}
              tone={status[key] ? 'ok' : 'warn'}
            />
          </li>
        ))}
      </ul>
      <p className="mt-2 text-xs text-slate-500">
        密钥仅在服务器 .env 中配置，API 永不返回明文，前端不提供修改入口。
      </p>
    </div>
  )
}
