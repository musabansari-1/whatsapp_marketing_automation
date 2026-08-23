/**
 * Lead state colours, shared by the list and the detail page.
 *
 * In its own module because a Next.js page file may export only the page itself —
 * anything else fails the build's route-type check.
 */

export const STATUS_STYLES: Record<string, string> = {
  QUALIFIED: 'bg-emerald-100 text-emerald-800',
  DEMO_BOOKED: 'bg-emerald-100 text-emerald-800',
  DEMO_OFFERED: 'bg-sky-100 text-sky-800',
  INTERESTED: 'bg-sky-100 text-sky-800',
  QUALIFYING: 'bg-sky-100 text-sky-800',
  RESPONDED: 'bg-slate-100 text-slate-700',
  CONTACTED: 'bg-slate-100 text-slate-700',
  NEW: 'bg-slate-100 text-slate-700',
  NOT_INTERESTED: 'bg-amber-100 text-amber-800',
  OPTED_OUT: 'bg-red-100 text-red-800',
  INVALID: 'bg-red-100 text-red-800',
  FAILED: 'bg-red-100 text-red-800',
};
