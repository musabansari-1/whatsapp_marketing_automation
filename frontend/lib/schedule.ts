/**
 * Scheduled broadcast times in the browser.
 *
 * The picker is a `datetime-local` input, which shows and returns the viewer's own
 * clock time with no timezone. The API refuses a time without one, so a picked value
 * is turned into a real instant here and sent as UTC (`toISOString`). The limits mirror
 * `app/domain/campaign_schedule.py`; the API checks them again, this only explains a
 * bad pick before the request is made.
 */

/** The earliest a schedule may be set, from now. Matches the API. */
export const MIN_LEAD_MS = 2 * 60_000;
/** The furthest ahead a schedule may be set. Matches the API (Cloud Tasks holds 30). */
export const MAX_AHEAD_MS = 29 * 86_400_000;

const pad = (n: number) => String(n).padStart(2, '0');

/** A date as a `datetime-local` value (`2026-11-05T10:30`) in the viewer's clock. */
export function toLocalInputValue(date: Date): string {
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}

/** A `datetime-local` value read as the viewer's clock time, or null if malformed. */
export function fromLocalInputValue(value: string): Date | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value);
  if (!match) return null;
  const [, y, mo, d, h, mi] = match;
  const date = new Date(Number(y), Number(mo) - 1, Number(d), Number(h), Number(mi), 0, 0);
  // new Date rolls 31 Feb over to March; a value that does not round-trip is not real.
  return toLocalInputValue(date) === value ? date : null;
}

/** Why `when` cannot be scheduled, or null if it can. */
export function scheduleProblem(when: Date | null, now: Date = new Date()): string | null {
  if (!when) return 'Pick a date and time.';
  if (when.getTime() < now.getTime() + MIN_LEAD_MS) {
    return 'Pick a time at least 2 minutes from now, or use Send broadcast to send now.';
  }
  if (when.getTime() > now.getTime() + MAX_AHEAD_MS) {
    return 'A campaign can be scheduled at most 29 days ahead.';
  }
  return null;
}

/** A sensible first suggestion: the next whole hour at least 30 minutes away. */
export function defaultScheduleTime(now: Date = new Date()): Date {
  const next = new Date(now.getTime() + 30 * 60_000);
  next.setMinutes(0, 0, 0);
  next.setHours(next.getHours() + 1);
  return next;
}

/** `in 3h 20m`, `in 5m`, `in 2 days`, or `now` once the moment has passed. */
export function timeUntil(value: string, now: Date = new Date()): string {
  const left = new Date(value).getTime() - now.getTime();
  if (left <= 0) return 'now';
  const minutes = Math.ceil(left / 60_000);
  if (minutes < 60) return `in ${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `in ${hours}h ${minutes % 60}m`;
  return `in ${Math.floor(hours / 24)} days`;
}

/** `Wed 5 Nov, 10:30` in the viewer's clock — unambiguous for a send time. */
export function scheduleLabel(value: string): string {
  const when = new Date(value);
  const day = when.toLocaleDateString('en-GB', {
    weekday: 'short',
    day: 'numeric',
    month: 'short',
  });
  const time = when.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
  return `${day}, ${time}`;
}

/** Activity a campaign records when a scheduled start was handed back to a person. */
export const HANDED_BACK_EVENTS = ['SCHEDULE_FAILED', 'SCHEDULE_MISSED'] as const;
