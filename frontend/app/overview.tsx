/**
 * Overview (specification §17).
 *
 * The nine §17 metrics, arranged as the funnel they actually describe: reach, then
 * engagement, then outcomes. A flat grid of nine numbers is a report; this is meant to
 * answer "is it working?" at a glance.
 */

'use client';

import Link from 'next/link';
import { useEffect, useState } from 'react';

import { api, type LeadFunnel, type Overview } from '@/lib/api';

function Metric({
  label,
  value,
  hint,
  href,
}: {
  label: string;
  value: number;
  hint?: string;
  href?: string;
}) {
  const body = (
    <div className="rounded-lg border border-slate-200 bg-white p-4">
      <p className="text-sm text-slate-600">{label}</p>
      <p className="mt-1 text-2xl font-semibold tabular-nums">{value.toLocaleString('en-GB')}</p>
      {hint ? <p className="mt-1 text-xs text-slate-500">{hint}</p> : null}
    </div>
  );

  return href ? (
    <Link href={href} className="block hover:opacity-80">
      {body}
    </Link>
  ) : (
    body
  );
}

export function OverviewMetrics() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [funnel, setFunnel] = useState<LeadFunnel | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([api.getOverview(), api.getLeadFunnel()])
      .then(([metrics, leadFunnel]) => {
        setOverview(metrics);
        setFunnel(leadFunnel);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  if (error) {
    return (
      <p className="rounded border border-red-200 bg-red-50 p-4 text-sm text-red-700">{error}</p>
    );
  }

  if (!overview) {
    return <p className="text-sm text-slate-600">Loading…</p>;
  }

  // Rates are shown against what was actually sent, not against the audience: a
  // campaign that skipped suppressed numbers has not "failed" to reach them.
  const deliveryRate =
    overview.messages_sent > 0
      ? Math.round((overview.delivered / overview.messages_sent) * 100)
      : null;
  const replyRate =
    overview.messages_sent > 0
      ? Math.round((overview.replies / overview.messages_sent) * 100)
      : null;

  return (
    <div className="space-y-8">
      <section>
        <h2 className="text-sm font-medium text-slate-900">Reach</h2>
        <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Metric
            label="Campaigns"
            value={overview.campaigns}
            hint={
              overview.campaigns_sending > 0 ? `${overview.campaigns_sending} sending` : undefined
            }
            href="/campaigns"
          />
          <Metric label="Messages sent" value={overview.messages_sent} />
          <Metric
            label="Delivered"
            value={overview.delivered}
            hint={deliveryRate !== null ? `${deliveryRate}% of sent` : undefined}
          />
          <Metric label="Read" value={overview.read} />
        </div>
      </section>

      <section>
        <h2 className="text-sm font-medium text-slate-900">Engagement</h2>
        <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Metric
            label="Replies"
            value={overview.replies}
            hint={replyRate !== null ? `${replyRate}% of sent` : 'People, not messages'}
            href="/conversations"
          />
          <Metric label="Leads" value={overview.total_leads} href="/leads" />
          <Metric label="Interested" value={overview.interested} />
          <Metric label="Failed" value={overview.failed} />
        </div>
      </section>

      <section>
        <h2 className="text-sm font-medium text-slate-900">Outcomes</h2>
        <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Metric label="Qualified" value={overview.qualified} />
          <Metric label="Demos booked" value={overview.demos_booked} href="/bookings" />
        </div>
        {overview.qualified === 0 && overview.interested > 0 ? (
          <p className="mt-3 rounded border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
            No leads have qualified. Qualification criteria are configuration and are currently
            unset, so nothing can reach QUALIFIED ([D37]).
          </p>
        ) : null}
      </section>

      {funnel ? (
        <section>
          <h2 className="text-sm font-medium text-slate-900">Lead states</h2>
          <div className="mt-3 rounded-lg border border-slate-200 bg-white p-4">
            <ul className="space-y-1 text-sm">
              {Object.entries(funnel.counts)
                .filter(([, count]) => count > 0)
                .map(([state, count]) => (
                  <li key={state} className="flex justify-between">
                    <span className="text-slate-600">{state}</span>
                    <span className="tabular-nums">{count}</span>
                  </li>
                ))}
            </ul>
            {Object.values(funnel.counts).every((count) => count === 0) ? (
              <p className="text-sm text-slate-600">No leads yet.</p>
            ) : null}
          </div>
        </section>
      ) : null}
    </div>
  );
}
