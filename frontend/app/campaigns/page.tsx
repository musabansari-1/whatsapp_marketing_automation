/**
 * Campaigns list (specification §17).
 *
 * Minimal by design; the full dashboard is Phase 13.
 */

'use client';

import Link from 'next/link';
import { useEffect, useState } from 'react';

import { Nav } from '@/app/nav';
import { api, type Campaign } from '@/lib/api';

export default function CampaignsPage() {
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api
      .listCampaigns()
      .then(setCampaigns)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  return (
    <>
      <Nav />
      <main className="mx-auto max-w-4xl px-6 py-12">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-xl font-semibold tracking-tight">Campaigns</h1>
            <p className="mt-1 text-sm text-slate-600">
              Creating a campaign prepares it. Sending is a separate, explicit action.
            </p>
          </div>
          <Link
            href="/campaigns/new"
            className="rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white"
          >
            New campaign
          </Link>
        </div>

        {error && (
          <p role="alert" className="mt-6 text-sm text-red-700">
            {error}
          </p>
        )}

        {loading ? (
          <p className="mt-6 text-sm text-slate-500">Loading...</p>
        ) : campaigns.length === 0 ? (
          <p className="mt-6 text-sm text-slate-500">No campaigns yet.</p>
        ) : (
          <div className="mt-6 overflow-x-auto rounded-lg border border-slate-200 bg-white">
            <table className="w-full text-sm">
              <thead className="border-b border-slate-200 text-left text-xs uppercase text-slate-500">
                <tr>
                  <th className="px-4 py-3 font-medium">Name</th>
                  <th className="px-4 py-3 font-medium">Status</th>
                  <th className="px-4 py-3 text-right font-medium">Recipients</th>
                  <th className="px-4 py-3 text-right font-medium">Sent</th>
                  <th className="px-4 py-3 text-right font-medium">Skipped</th>
                </tr>
              </thead>
              <tbody>
                {campaigns.map((campaign) => (
                  <tr key={campaign.id} className="border-b border-slate-100 last:border-0">
                    <td className="px-4 py-3">
                      <Link href={`/campaigns/${campaign.id}`} className="underline">
                        {campaign.name}
                      </Link>
                    </td>
                    <td className="px-4 py-3">{campaign.status}</td>
                    <td className="px-4 py-3 text-right tabular-nums">
                      {campaign.total_recipients}
                    </td>
                    <td className="px-4 py-3 text-right tabular-nums">{campaign.sent_count}</td>
                    <td className="px-4 py-3 text-right tabular-nums">{campaign.skipped_count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </main>
    </>
  );
}
