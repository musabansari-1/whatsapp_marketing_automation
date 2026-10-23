import Link from 'next/link';
import type { ButtonHTMLAttributes, ReactNode } from 'react';

import { cx } from '@/lib/cx';

export function PageHeader({
  title,
  description,
  actions,
  back,
}: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
  back?: { href: string; label: string };
}) {
  return (
    <div className="mb-8 flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
      <div className="min-w-0">
        {back ? (
          <Link
            href={back.href}
            className="group mb-2 inline-flex items-center gap-1.5 text-xs font-semibold text-ink-muted transition hover:text-forest"
          >
            <span aria-hidden className="transition-transform group-hover:-translate-x-0.5">
              ←
            </span>
            {back.label}
          </Link>
        ) : null}
        <h1 className="text-[1.75rem] font-extrabold tracking-tight text-ink">{title}</h1>
        {description ? (
          <div className="mt-2 max-w-2xl text-sm leading-relaxed text-ink-muted">{description}</div>
        ) : null}
      </div>
      {actions ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
    </div>
  );
}

type ButtonVariant = 'primary' | 'secondary' | 'danger' | 'ghost' | 'danger-outline';

const BUTTON_VARIANTS: Record<ButtonVariant, string> = {
  primary:
    'bg-gradient-to-b from-forest-bright to-forest text-white shadow-glow-sm hover:brightness-[1.08] hover:shadow-glow disabled:hover:brightness-100 disabled:hover:shadow-glow-sm',
  secondary:
    'border border-line bg-white text-ink shadow-input hover:border-ink/20 hover:bg-canvas disabled:hover:bg-white',
  danger: 'bg-red-700 text-white shadow-sm hover:bg-red-800 disabled:hover:bg-red-700',
  ghost: 'text-ink-muted hover:bg-canvas hover:text-ink',
  'danger-outline':
    'border border-red-200 bg-white text-red-800 shadow-input hover:bg-red-50 disabled:hover:bg-white',
};

function buttonBase(): string {
  return 'inline-flex items-center justify-center gap-2 rounded-xl px-4 py-2.5 text-sm font-semibold transition-all duration-200 ease-out-expo disabled:cursor-not-allowed disabled:opacity-50 active:scale-[0.98]';
}

export function Button({
  variant = 'primary',
  className,
  type = 'button',
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant }) {
  return (
    <button
      type={type}
      className={cx(buttonBase(), BUTTON_VARIANTS[variant], className)}
      {...props}
    />
  );
}

export function ButtonLink({
  href,
  variant = 'primary',
  className,
  children,
}: {
  href: string;
  variant?: ButtonVariant;
  className?: string;
  children: ReactNode;
}) {
  return (
    <Link href={href} className={cx(buttonBase(), BUTTON_VARIANTS[variant], className)}>
      {children}
    </Link>
  );
}

export function Card({
  children,
  className,
  padding = 'p-5',
}: {
  children: ReactNode;
  className?: string;
  padding?: string;
}) {
  return (
    <div className={cx('rounded-2xl border border-line bg-white shadow-card', padding, className)}>
      {children}
    </div>
  );
}

export function Alert({
  tone = 'danger',
  children,
  className,
}: {
  tone?: 'danger' | 'warning' | 'success' | 'info';
  children: ReactNode;
  className?: string;
}) {
  const styles = {
    danger: 'border-red-200 bg-red-50 text-red-800',
    warning: 'border-amber-200 bg-amber-50 text-amber-950',
    success: 'border-emerald-200 bg-emerald-50 text-emerald-900',
    info: 'border-line bg-white text-ink',
  } as const;

  return (
    <p
      role={tone === 'danger' ? 'alert' : undefined}
      className={cx(
        'rounded-2xl border px-4 py-3 text-sm leading-relaxed shadow-input',
        styles[tone],
        className,
      )}
    >
      {children}
    </p>
  );
}

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <Card className="px-6 py-16 text-center">
      <div className="mx-auto flex size-11 items-center justify-center rounded-2xl bg-forest-soft text-forest">
        <span className="text-lg leading-none">◆</span>
      </div>
      <p className="mt-4 text-sm font-semibold text-ink">{title}</p>
      <p className="mx-auto mt-1.5 max-w-md text-sm text-ink-muted">{description}</p>
      {action ? <div className="mt-5">{action}</div> : null}
    </Card>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      className={cx(
        'animate-pulse rounded-xl bg-gradient-to-r from-line/60 via-line/80 to-line/60 bg-[length:200px_100%]',
        className,
      )}
    />
  );
}

export function StatCard({
  label,
  value,
  hint,
  href,
  tone,
}: {
  label: string;
  value: number;
  hint?: string;
  href?: string;
  tone?: string;
}) {
  const body = (
    <Card className="group relative h-full overflow-hidden p-5 transition-all duration-200 ease-out-expo hover:-translate-y-0.5 hover:border-ink/15 hover:shadow-lift">
      <div className="pointer-events-none absolute -right-8 -top-8 size-24 rounded-full bg-forest-bright/10 opacity-0 blur-2xl transition-opacity duration-300 group-hover:opacity-100" />
      <div className="relative">
        <p className="text-[11px] font-bold uppercase tracking-section text-ink-muted">{label}</p>
        <p
          className={cx(
            'mt-2 text-[1.9rem] font-extrabold leading-none tabular-nums tracking-tight',
            tone,
          )}
        >
          {value.toLocaleString('en-GB')}
        </p>
        {hint ? <p className="mt-1.5 text-xs font-medium text-ink-muted">{hint}</p> : null}
      </div>
    </Card>
  );

  return href ? (
    <Link href={href} className="block focus-visible:outline-none">
      {body}
    </Link>
  ) : (
    body
  );
}

export function FilterChip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cx(
        'rounded-full px-3.5 py-1.5 text-sm font-semibold transition-all duration-200',
        active
          ? 'bg-ink text-white shadow-sm'
          : 'border border-line bg-white text-ink-muted hover:border-ink/25 hover:text-ink hover:shadow-input',
      )}
    >
      {children}
    </button>
  );
}

export function SectionLabel({ children }: { children: ReactNode }) {
  return (
    <h2 className="text-[11px] font-bold uppercase tracking-section text-ink-muted">{children}</h2>
  );
}
