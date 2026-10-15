export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'ui-sans-serif', 'system-ui', 'Segoe UI', 'Roboto', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'SFMono-Regular', 'Consolas', 'monospace'],
      },
      colors: {
        // A navy, indigo-tinted grey scale: every existing slate-* class picks it up.
        slate: {
          50: '#f6f7fc',
          100: '#eef0fa',
          200: '#dde1f2',
          300: '#c3c9e3',
          400: '#98a0c3',
          500: '#6b7397',
          600: '#4b5378',
          700: '#343c63',
          800: '#232b4e',
          900: '#121935',
          950: '#080c22',
        },
        brand: {
          50: '#eef2ff',
          500: '#6366f1',
          600: '#4f46e5',
          700: '#4338ca',
          900: '#312e81',
        },
      },
      boxShadow: {
        glow: '0 0 0 1px rgba(129, 140, 248, 0.15), 0 10px 40px -12px rgba(99, 102, 241, 0.45)',
      },
    },
  },
  plugins: [],
}
