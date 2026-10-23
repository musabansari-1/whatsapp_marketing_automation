/**
 * Overview metrics (specification §17, [D53]).
 *
 * The §17 metrics, arranged as the funnel they describe: reach, then engagement, then
 * outcomes. One component for the product overview, a segment of it, and every product
 * combined — the scope is decided by whoever passes `load`, so the three views cannot
 * drift apart in what they show.
 */

'use client';

import { useEffect, useState } from 'react';

import { Alert, SectionLabel, Skeleton, StatCard } from '@/components/ui';
import type { LeadFunnel, Overview } from '@/lib/api';
import { humanizeStatus } from '@/lib/format';

const FUNNEL_COLORS: Record<string, string> = {
  NEW: '#A8A29A',
  CONTACTED: '#78716C',
  RESPONDED: '#57534E',
  INTERESTED: '#0EA5E9',
  QUALIFYING: '#0284C7',
  QUALIFIED: '#1F6B4A',
  DEMO_OFFERED: '#15803D',
  DEMO_BOOKED: '#166534',
  NOT_INTERESTED: '#D97706',
  OPTED_OUT: '#B91C1C',
  INVALID: '#9F1239',
  FAILED: '#991B1B',
};

export interface OverviewLinks {
  campaigns?: string;
  conversations?: string;
  leads?: string;
  bookings?: string;
}

export function OverviewMetrics({
  load,
  links = {},
  showDemos = true,
}: {
  /** Loads the overview and funnel for the scope this view is showing. Memoize it. */
  load: () => Promise<[Overview, LeadFunnel]>;
  links?: OverviewLinks;
  /** False for a product that converts by signup link and has no demo calendar. */
  showDemos?: boolean;
}) {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [funnel, setFunnel] = useState<LeadFunnel | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setOverview(null);
    setError(null);
    load()
      .then(([metrics, leadFunnel]) => {
        if (cancelled) return;
        setOverview(metrics);
        setFunnel(leadFunnel);
      })
      .catch((e: Error) => {
        if (!cancelled) setError(e.message);
      });
    return () => {
      cancelled = true;
    };
  }, [load]);

  if (error) {
    return <Alert>{error}</Alert>;
  }

  if (!overview) {
    return (
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {Array.from({ length: 8 }).map((_, i) => (
          <Skeleton key={i} className="h-28" />
        ))}
      </div>
    );
  }

  // Rates are shown against what was actually sent, not against the audience: a
  // campaign that skipped suppressed numbers has not "failed" to reach them.
  const deliveryRate =
    overview.messages_sent > 0
      ? Math.round((overview.delivered / overview.messages_sent) * 100)
      : null;
  // Replies count everyone who wrote, including people who messaged first, so the
  // rate against campaign sends is only meaningful while it stays within 100%.
  const replyRate =
    overview.messages_sent > 0 && overview.replies <= overview.messages_sent
      ? Math.round((overview.replies / overview.messages_sent) * 100)
      : null;

  const funnelEntries = Object.entries(funnel?.counts ?? {}).filter(([, count]) => count > 0);
  const funnelTotal = funnelEntries.reduce((sum, [, count]) => sum + count, 0);

  return (
    <div className="space-y-10">
      <section>
        <SectionLabel>Reach</SectionLabel>
        <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <StatCard
            label="Campaigns"
            value={overview.campaigns}
            hint={
              overview.campaigns_sending > 0 ? `${overview.campaigns_sending} sending` : undefined
            }
            href={links.campaigns}
          />
          <StatCard label="Messages sent" value={overview.messages_sent} />
          <StatCard
            label="Delivered"
            value={overview.delivered}
            hint={deliveryRate !== null ? `${deliveryRate}% of sent` : undefined}
          />
          <StatCard label="Read" value={overview.read} />
        </div>
      </section>

      <section>
        <SectionLabel>Engagement</SectionLabel>
        <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <StatCard
            label="Replies"
            value={overview.replies}
            hint={replyRate !== null ? `${replyRate}% of sent` : 'People who wrote to us'}
            href={links.conversations}
          />
          <StatCard label="Leads" value={overview.total_leads} href={links.leads} />
          <StatCard label="Interested" value={overview.interested} />
          <StatCard
            label="Failed"
            value={overview.failed}
            tone={overview.failed > 0 ? 'text-red-800' : undefined}
          />
        </div>
      </section>

      <section>
        <SectionLabel>Outcomes</SectionLabel>
        <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <StatCard label="Qualified" value={overview.qualified} />
          {showDemos ? (
            <StatCard label="Demos booked" value={overview.demos_booked} href={links.bookings} />
          ) : null}
        </div>
        {overview.qualified === 0 && overview.interested > 0 ? (
          <Alert tone="warning" className="mt-3">
            No leads have qualified. Qualification criteria are configuration and are currently
            unset, so nothing can reach QUALIFIED ([D37]).
          </Alert>
        ) : null}
      </section>

      {funnel ? (
        <section>
          <SectionLabel>Lead states</SectionLabel>
          <div className="mt-3 rounded-2xl border border-line bg-white p-6 shadow-card">
            {funnelTotal === 0 ? (
              <p className="text-sm text-ink-muted">No leads yet.</p>
            ) : (
              <>
                <div className="flex h-3.5 overflow-hidden rounded-full bg-canvas ring-1 ring-line/60">
                  {funnelEntries.map(([state, count]) => (
                    <div
                      key={state}
                      title={`${state}: ${count}`}
                      className="h-full"
                      style={{
                        width: `${Math.max((count / funnelTotal) * 100, 1.5)}%`,
                        backgroundColor: FUNNEL_COLORS[state] ?? '#78716C',
                      }}
                    />
                  ))}
                </div>
                <ul className="mt-5 grid gap-2 sm:grid-cols-2">
                  {funnelEntries.map(([state, count]) => (
                    <li key={state} className="flex items-center justify-between text-sm">
                      <span className="flex items-center gap-2 text-ink-muted">
                        <span
                          className="size-2 rounded-full"
                          style={{ backgroundColor: FUNNEL_COLORS[state] ?? '#78716C' }}
                        />
                        {humanizeStatus(state)}
                      </span>
                      <span className="tabular-nums text-ink">{count}</span>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </div>
        </section>
      ) : null}
    </div>
  );
}
