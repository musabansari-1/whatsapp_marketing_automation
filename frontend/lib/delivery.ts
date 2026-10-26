/**
 * What a broadcast's delivery looks like, in the terms an admin needs.
 *
 * The recipient statuses are a progression (queued, sent, delivered, read), so the
 * headline numbers are cumulative: a READ message was also delivered and sent. Showing
 * the raw SENT count instead is what made a finished broadcast that reached 140 people
 * read "Sent 7".
 *
 * Meta's failure messages are written for developers ("not delivered to maintain
 * healthy ecosystem engagement"), so the common ones are explained here with what to
 * do next. Kept as pure functions so they can be tested without rendering.
 */

import type { CampaignRecipient, RecipientStatus } from '@/lib/api';

export type StatusCounts = Partial<Record<RecipientStatus, number>>;

export interface DeliverySummary {
  total: number;
  /** Not handed to WhatsApp yet: PENDING or QUEUED. */
  waiting: number;
  /** Accepted by WhatsApp, whatever happened next. */
  sent: number;
  /** Sent, with no delivery report yet. */
  awaitingReport: number;
  delivered: number;
  read: number;
  failed: number;
  skipped: number;
}

export function summarize(counts: StatusCounts): DeliverySummary {
  const n = (status: RecipientStatus) => counts[status] ?? 0;
  const read = n('READ');
  const delivered = n('DELIVERED') + read;
  return {
    total: Object.values(counts).reduce((sum, value) => sum + (value ?? 0), 0),
    waiting: n('PENDING') + n('QUEUED'),
    sent: n('SENT') + delivered,
    awaitingReport: n('SENT'),
    delivered,
    read,
    failed: n('FAILED'),
    skipped: n('SKIPPED'),
  };
}

/** `42%`, or an empty string when there is nothing to divide by. */
export function percent(part: number, whole: number): string {
  if (whole <= 0) return '';
  return `${Math.round((part / whole) * 100)}%`;
}

export interface ErrorExplanation {
  /** A few words for the recipients table. */
  label: string;
  /** What happened and what to do, for the failure summary. */
  detail: string;
}

// Meta Cloud API error codes seen on marketing broadcasts.
const META_ERRORS: Record<string, ErrorExplanation> = {
  '131049': {
    label: 'Meta marketing limit',
    detail:
      'Meta chose not to deliver it because this person has recently received many ' +
      'marketing messages. Not an error on our side; trying again after a few days ' +
      'sometimes works.',
  },
  '131026': {
    label: 'Not on WhatsApp',
    detail:
      'The number is not on WhatsApp, is a landline, or uses a very old WhatsApp ' +
      'version. Check the number or reach them another way.',
  },
  '130472': {
    label: 'Meta experiment',
    detail:
      'Meta holds a small share of people back from marketing messages as an ' +
      'experiment. Nothing to fix.',
  },
  '131050': {
    label: 'Stopped marketing',
    detail: 'This person has stopped marketing messages from this business on WhatsApp.',
  },
  '131056': {
    label: 'Too many, too fast',
    detail: 'Too many messages were sent to this number in a short time. Try again later.',
  },
  '131047': {
    label: 'Reply window closed',
    detail: 'More than 24 hours since they last wrote, so only a template can be sent.',
  },
  '131042': {
    label: 'Billing problem',
    detail: 'Meta refused the send because of a payment issue on the WhatsApp account.',
  },
  '132012': {
    label: 'Template mismatch',
    detail: "The message did not match the approved template's format, such as its header.",
  },
  template_media_missing: {
    label: 'No header media',
    detail: 'The template needs a header image or video and none is set on the template.',
  },
};

export function explainError(
  code: string | null | undefined,
  message: string | null | undefined,
): ErrorExplanation | null {
  if (code && META_ERRORS[code]) return META_ERRORS[code];
  if (!code && !message) return null;
  return { label: message ?? `Error ${code}`, detail: message ?? `Meta error ${code}.` };
}

const EVENTS = [
  ['read_at', 'Read'],
  ['delivered_at', 'Delivered'],
  ['failed_at', 'Failed'],
  ['sent_at', 'Sent'],
  ['queued_at', 'Queued'],
] as const;

/** The most recent thing that happened to this message, for the "Last update" column. */
export function lastEvent(
  recipient: Pick<CampaignRecipient, (typeof EVENTS)[number][0]>,
): { label: string; at: string } | null {
  let latest: { label: string; at: string } | null = null;
  for (const [field, label] of EVENTS) {
    const at = recipient[field];
    if (at && (!latest || new Date(at) > new Date(latest.at))) latest = { label, at };
  }
  return latest;
}

/** Messages still queued this long after the start have most likely been lost. */
export const STUCK_AFTER_MINUTES = 15;

export function stuckCount(
  summary: DeliverySummary,
  startedAt: string | null,
  now: Date = new Date(),
): number {
  if (!startedAt || summary.waiting === 0) return 0;
  const minutes = (now.getTime() - new Date(startedAt).getTime()) / 60_000;
  return minutes >= STUCK_AFTER_MINUTES ? summary.waiting : 0;
}
