import type { Config } from 'tailwindcss';

const config: Config = {
  content: ['./app/**/*.{ts,tsx}', './components/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        canvas: 'rgb(var(--color-canvas) / <alpha-value>)',
        ink: {
          DEFAULT: 'rgb(var(--color-ink) / <alpha-value>)',
          muted: 'rgb(var(--color-ink-muted) / <alpha-value>)',
          faint: 'rgb(var(--color-ink-faint) / <alpha-value>)',
        },
        line: 'rgb(var(--color-line) / <alpha-value>)',
        forest: {
          DEFAULT: 'rgb(var(--color-forest) / <alpha-value>)',
          hover: '#035F45',
          bright: 'rgb(var(--color-forest-bright) / <alpha-value>)',
          faint: '#E7F8F0',
          soft: '#F0FBF6',
        },
        sidebar: {
          DEFAULT: '#0B0E0D',
          hover: '#151A18',
          active: '#1B2420',
          fg: '#F2F5F3',
          muted: '#8F9894',
          line: '#1F2623',
        },
      },
      fontFamily: {
        sans: ['var(--font-sans)', 'system-ui', 'sans-serif'],
        mono: ['var(--font-mono)', 'ui-monospace', 'monospace'],
      },
      boxShadow: {
        card: '0 1px 2px rgb(23 25 24 / 0.03), 0 12px 32px -12px rgb(23 25 24 / 0.07)',
        lift: '0 2px 4px rgb(23 25 24 / 0.04), 0 20px 44px -16px rgb(23 25 24 / 0.14)',
        input: '0 1px 2px rgb(23 25 24 / 0.04)',
        glow: '0 0 0 1px rgb(16 185 129 / 0.25), 0 8px 28px -8px rgb(16 185 129 / 0.35)',
        'glow-sm': '0 0 0 1px rgb(16 185 129 / 0.2), 0 4px 16px -4px rgb(16 185 129 / 0.3)',
      },
      letterSpacing: {
        section: '0.12em',
      },
      transitionTimingFunction: {
        'out-expo': 'cubic-bezier(0.22, 1, 0.36, 1)',
      },
    },
  },
  plugins: [],
};

export default config;
