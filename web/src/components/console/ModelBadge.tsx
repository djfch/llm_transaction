/**
 * 模型身份徽标：显示本轮调用的模型名（+ 思考强度档位），title 悬浮补充凭证名/Provider。
 * 模型名为空串（功能上线前的历史数据无记录）时不渲染，避免老数据挂空徽标。
 * 模型名/档位属英文技术标识，按项目规范保持原样不翻译。
 */

interface ModelBadgeProps {
  model: string // 模型名（空串 = 无记录，不渲染）
  thinkingEffort: string // 思考强度档位（空串 = 无此配置，不拼接）
  credentialName: string // 凭证名（仅进 title 悬浮提示）
  provider: string // Provider 类型（仅进 title 悬浮提示）
}

export default function ModelBadge({ model, thinkingEffort, credentialName, provider }: ModelBadgeProps) {
  if (model === '') return null
  const title = [
    credentialName !== '' ? `凭证 ${credentialName}` : '',
    provider !== '' ? `Provider ${provider}` : '',
  ]
    .filter((s) => s !== '')
    .join(' · ')
  return (
    <span
      title={title !== '' ? title : undefined}
      className="rounded border border-zinc-600/50 bg-zinc-700/30 px-2 py-0.5 font-mono text-[10px] text-zinc-400"
    >
      {model}
      {thinkingEffort !== '' ? ` · ${thinkingEffort}` : ''}
    </span>
  )
}
