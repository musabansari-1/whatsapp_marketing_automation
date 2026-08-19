/**
 * Audience detail (specification §6, §17).
 *
 * Shows every parsed row with its validation outcome, filterable by status, because
 * §6 requires the admin to be able to see what was rejected and why.
 */

'use client';

import Link from 'next/link';
import { useParams } from 'next/navigation';
import { useCallback, useEffect, useState } from 'react';

import { api, type AudienceContact, type AudienceSummary, type ValidationStatus } from '@/lib/api';

const FILTERS: { label: string; value: ValidationStatus | 'ALL' }[] = [
  { label: 'All', value: 'ALL' },
  { label: 'Valid', value: 'VALID' },
  { label: 'Invalid', value: 'INVALID' },
  { label: 'Duplicate', value: 'DUPLICATE' },
];

export default function AudienceDetailPage() {
  const params = useParams<{ id: string }>();
  const id = params.id;

  const [audience, setAudience] = useState<AudienceSummary | null>(null);
  const [contacts, setContacts] = useState<AudienceContact[]>([]);
  const [total, setTotal] = useState(0);
  const [filter, setFilter] = useState<ValidationStatus | 'ALL'>('ALL');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [summary, page] = await Promise.all([
        api.getAudience(id),
        api.listContacts(id, filter === 'ALL' ? undefined : filter),
      ]);
      setAudience(summary);
      setContacts(page.items);
      setTotal(page.total);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load this audience.');
    }
  }, [id, filter]);

  useEffect(() => {
    void load();
  }, [load]);

  async function handleConfirm() {
    setBusy(true);
    setError(null);
    try {
      await api.confirmAudience(id);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Confirmation failed.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto max-w-4xl px-6 py-12">
      <Link href="/audiences" className="text-sm text-slate-600 underline">
        Back to audiences
      </Link>

      {error && (
        <p role="alert" className="mt-4 text-sm text-red-700">
          {error}
        </p>
      )}

      {audience && (
        <>
          <div className="mt-4 flex items-start justify-between gap-4">
            <div>
              <h1 className="text-xl font-semibold tracking-tight">{audience.name}</h1>
              <p className="mt-1 text-sm text-slate-600">
                {audience.total_rows} rows &middot; {audience.valid_rows} valid &middot;{' '}
                {audience.invalid_rows} invalid &middot; {audience.duplicate_rows} duplicate
              </p>
            </div>
            {audience.status === 'PENDING_CONFIRMATION' ? (
              <button
                type="button"
                onClick={handleConfirm}
                disabled={busy || audience.valid_rows === 0}
                className="shrink-0 rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-60"
              >
                {busy ? 'Confirming...' : 'Confirm audience'}
              </button>
            ) : (
              <span className="shrink-0 text-sm text-emerald-700">Confirmed</span>
            )}
          </div>

          <div className="mt-6 flex gap-2">
            {FILTERS.map((option) => (
              <button
                key={option.value}
                type="button"
                onClick={() => setFilter(option.value)}
                className={`rounded-md border px-3 py-1.5 text-sm ${
                  filter === option.value
                    ? 'border-slate-900 bg-slate-900 text-white'
                    : 'border-slate-300 text-slate-700'
                }`}
              >
                {option.label}
              </button>
            ))}
          </div>

          <p className="mt-3 text-xs text-slate-500">
            Showing {contacts.length} of {total}
          </p>

          <div className="mt-2 overflow-x-auto rounded-lg border border-slate-200 bg-white">
            <table className="w-full text-sm">
              <thead className="border-b border-slate-200 text-left text-xs uppercase text-slate-500">
                <tr>
                  <th className="px-3 py-2 font-medium">Row</th>
                  <th className="px-3 py-2 font-medium">Phone</th>
                  <th className="px-3 py-2 font-medium">Name</th>
                  <th className="px-3 py-2 font-medium">Email</th>
                  <th className="px-3 py-2 font-medium">Status</th>
                </tr>
              </thead>
              <tbody>
                {contacts.map((contact) => (
                  <tr key={contact.id} className="border-b border-slate-100 last:border-0">
                    <td className="px-3 py-2 tabular-nums">{contact.row_number}</td>
                    <td className="px-3 py-2 font-mono text-xs">{contact.phone_e164 ?? '-'}</td>
                    <td className="px-3 py-2">{contact.full_name ?? '-'}</td>
                    <td className="px-3 py-2">{contact.email ?? '-'}</td>
                    <td className="px-3 py-2">
                      {contact.validation_status === 'VALID' ? (
                        <span className="text-emerald-700">Valid</span>
                      ) : (
                        <span
                          className={
                            contact.validation_status === 'DUPLICATE'
                              ? 'text-amber-700'
                              : 'text-red-700'
                          }
                        >
                          {contact.validation_error ?? contact.validation_status}
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </main>
  );
}
