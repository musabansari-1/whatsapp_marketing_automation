import { cx } from '@/lib/cx';

const TONES = {
  success: 'bg-forest-soft text-forest ring-forest/10',
  info: 'bg-sky-50 text-sky-700 ring-sky-200/70',
  warning: 'bg-amber-50 text-amber-800 ring-amber-200/70',
  danger: 'bg-red-50 text-red-700 ring-red-200/70',
  muted: 'bg-stone-100 text-stone-600 ring-stone-200/60',
} as const;

const STATUS_TONE: Record<string, keyof typeof TONES> = {
  QUALIFIED: 'success',
  DEMO_BOOKED: 'success',
  CONFIRMED: 'success',
  READY: 'success',
  COMPLETED: 'success',
  SENT: 'success',
  DELIVERED: 'success',
  READ: 'success',
  VALID: 'success',
  ACTIVE: 'info',
  DEMO_OFFERED: 'info',
  INTERESTED: 'info',
  QUALIFYING: 'info',
  BOOKING: 'info',
  PROCESSING: 'info',
  QUEUED: 'info',
  SCHEDULED: 'info',
  RESPONDED: 'muted',
  CONTACTED: 'muted',
  NEW: 'muted',
  DRAFT: 'muted',
  CLOSED: 'muted',
  CANCELLED: 'muted',
  ARCHIVED: 'muted',
  SKIPPED: 'warning',
  NOT_INTERESTED: 'warning',
  PAUSED: 'warning',
  PENDING: 'warning',
  PENDING_CONFIRMATION: 'warning',
  WAITING_FOR_USER: 'warning',
  OPTED_OUT: 'danger',
  INVALID: 'danger',
  FAILED: 'danger',
  DUPLICATE: 'warning',
};

/** Lead-list colour map, kept for the existing list/detail pages. */
export const STATUS_STYLES: Record<string, string> = Object.fromEntries(
  Object.entries(STATUS_TONE).map(([status, tone]) => [status, TONES[tone]]),
);

export function StatusBadge({ status, className }: { status: string; className?: string }) {
  const tone = TONES[STATUS_TONE[status] ?? 'muted'];

  return (
    <span
      className={cx(
        'inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2.5 py-0.5 text-[11px] font-semibold tracking-wide ring-1',
        tone,
        className,
      )}
    >
      <span className="size-1.5 rounded-full bg-current opacity-70" />
      {status.replaceAll('_', ' ')}
    </span>
  );
}
