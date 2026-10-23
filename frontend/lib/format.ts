/**
 * Display formatting shared by the lead, conversation and overview pages.
 *
 * Everything here is presentation only: the stored values (E.164 phones, enum states,
 * fact keys) never change, only how a person reads them.
 */

/** `+919800000101` -> `+91 98000 00101`. Other countries are left as stored. */
export function formatPhone(e164: string | null | undefined): string {
  if (!e164) return '';
  const india = /^\+91(\d{5})(\d{5})$/.exec(e164);
  return india ? `+91 ${india[1]} ${india[2]}` : e164;
}

/** A chat link for the number, opening WhatsApp on the phone or WhatsApp Web. */
export function whatsappUrl(e164: string): string {
  return `https://wa.me/${e164.replace(/\D/g, '')}`;
}

/** `NOT_INTERESTED` -> `Not interested`. */
export function humanizeStatus(status: string): string {
  const words = status.toLowerCase().replaceAll('_', ' ');
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** Labels for the facts the assistant records; any other key is humanized. */
const FACT_LABELS: Record<string, string> = {
  registration_intent: 'Wants to register',
  human_requested: 'Asked for a person',
  practice_type: 'Practice type',
  business_type: 'Business type',
};

export function factLabel(key: string): string {
  return FACT_LABELS[key] ?? humanizeStatus(key);
}

export function factValue(value: unknown): string {
  if (value === true) return 'Yes';
  if (value === false) return 'No';
  if (value === null || value === undefined || value === '') return '—';
  if (Array.isArray(value)) return value.map(factValue).join(', ');
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

export type TextPart =
  { kind: 'text'; text: string } | { kind: 'link'; href: string; label: string };

const URL_PATTERN = /https?:\/\/[^\s<>"']+/g;

/**
 * Splits a message into text and links. A link's label drops the scheme and the
 * query string (`lifeherbagroup.com/join/doctor`), so a tracked signup link does not
 * fill the bubble; the full address stays in the href.
 */
export function splitLinks(text: string): TextPart[] {
  const parts: TextPart[] = [];
  let last = 0;
  for (const match of text.matchAll(URL_PATTERN)) {
    // A sentence's closing punctuation is not part of the link.
    const href = match[0].replace(/[.,!?;:)]+$/, '');
    const start = match.index ?? 0;
    if (start > last) parts.push({ kind: 'text', text: text.slice(last, start) });
    parts.push({ kind: 'link', href, label: shortUrl(href) });
    last = start + href.length;
  }
  if (last < text.length) parts.push({ kind: 'text', text: text.slice(last) });
  return parts;
}

function shortUrl(href: string): string {
  try {
    const url = new URL(href);
    const path = url.pathname === '/' ? '' : url.pathname.replace(/\/$/, '');
    return `${url.host.replace(/^www\./, '')}${path}`;
  } catch {
    return href;
  }
}

const WINDOW_MS = 24 * 60 * 60 * 1000;

/**
 * Time left in WhatsApp's 24-hour reply window, e.g. `23h 10m`, or null once it has
 * closed (or before the customer has written at all).
 */
export function replyWindowLeft(
  lastInboundAt: string | null,
  now: number = Date.now(),
): string | null {
  if (!lastInboundAt) return null;
  const left = new Date(lastInboundAt).getTime() + WINDOW_MS - now;
  if (left <= 0) return null;
  const hours = Math.floor(left / 3_600_000);
  const minutes = Math.floor((left % 3_600_000) / 60_000);
  return hours > 0 ? `${hours}h ${minutes}m` : `${minutes}m`;
}

/** `Today`, `Yesterday` or `23 Oct 2026`, for the date dividers in a transcript. */
export function dayLabel(value: string, now: Date = new Date()): string {
  const day = new Date(value);
  const startOf = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const diffDays = Math.round((startOf(now) - startOf(day)) / 86_400_000);
  if (diffDays === 0) return 'Today';
  if (diffDays === 1) return 'Yesterday';
  return day.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
}

/** `16:52` for today, otherwise `23 Oct, 16:52`. */
export function shortWhen(value: string, now: Date = new Date()): string {
  const when = new Date(value);
  const time = when.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
  if (when.toDateString() === now.toDateString()) return time;
  return `${when.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })}, ${time}`;
}
