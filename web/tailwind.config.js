/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      // 内置 line-clamp 只到 6；Agent 笔记卡片折叠用到 8 行
      lineClamp: { 8: '8' },
    },
  },
  plugins: [],
}
