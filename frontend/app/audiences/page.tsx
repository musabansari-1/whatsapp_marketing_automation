/**
 * Audiences list (specification §17).
 *
 * Minimal by design: the full dashboard is Phase 13. This exists so Phase 4 is
 * usable end to end rather than API-only.
 */

'use client';

import Link from 'next/link';
import { useEffect, useState } from 'react';

import { api, type AudienceSummary } from '@/lib/api';

export default function AudiencesPage() {
  const [audiences, setAudiences] = useState<AudienceSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api
      .listAudiences()
      .then(setAudiences)
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  return (
    <main className="mx-auto max-w-4xl px-6 py-12">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Audiences</h1>
          <p className="mt-1 text-sm text-slate-600">
            Uploading validates a spreadsheet. It never sends messages.
          </p>
        </div>
        <Link
          href="/audiences/upload"
          className="rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white"
        >
          Upload Excel
        </Link>
      </div>

      {error && (
        <p role="alert" className="mt-6 text-sm text-red-700">
          {error}
        </p>
      )}

      {loading ? (
        <p className="mt-6 text-sm text-slate-500">Loading…</p>
      ) : audiences.length === 0 ? (
        <p className="mt-6 text-sm text-slate-500">No audiences yet.</p>
      ) : (
        <div className="mt-6 overflow-x-auto rounded-lg border border-slate-200 bg-white">
          <table className="w-full text-sm">
            <thead className="border-b border-slate-200 text-left text-xs uppercase text-slate-500">
              <tr>
                <th className="px-4 py-3 font-medium">Name</th>
                <th className="px-4 py-3 font-medium">Status</th>
                <th className="px-4 py-3 text-right font-medium">Valid</th>
                <th className="px-4 py-3 text-right font-medium">Invalid</th>
                <th className="px-4 py-3 text-right font-medium">Duplicate</th>
              </tr>
            </thead>
            <tbody>
              {audiences.map((audience) => (
                <tr key={audience.id} className="border-b border-slate-100 last:border-0">
                  <td className="px-4 py-3">
                    <Link href={`/audiences/${audience.id}`} className="underline">
                      {audience.name}
                    </Link>
                  </td>
                  <td className="px-4 py-3">
                    <span
                      className={
                        audience.status === 'CONFIRMED' ? 'text-emerald-700' : 'text-amber-700'
                      }
                    >
                      {audience.status === 'CONFIRMED' ? 'Confirmed' : 'Awaiting confirmation'}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right tabular-nums">{audience.valid_rows}</td>
                  <td className="px-4 py-3 text-right tabular-nums">{audience.invalid_rows}</td>
                  <td className="px-4 py-3 text-right tabular-nums">{audience.duplicate_rows}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </main>
  );
}
