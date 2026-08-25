/**
 * Leads (specification §17).
 *
 * §17 asks for customer information, source, state, conversation, booking and
 * timestamps. This list carries the first three and links to the rest — a lead row
 * that showed everything would be unreadable, and the detail page is where a
 * salesperson actually works.
 */

'use client';

import Link from 'next/link';
import { useCallback, useEffect, useState } from 'react';

import { Nav } from '@/app/nav';
import { STATUS_STYLES } from '@/app/leads/status-styles';
import { Pager } from '@/app/pager';
import { api, type Lead, type LeadStatus } from '@/lib/api';
import { clampOffset, DEFAULT_PAGE_SIZE } from '@/lib/pagination';

const STATUSES: LeadStatus[] = [
  'NEW',
  'CONTACTED',
  'RESPONDED',
  'INTERESTED',
  'QUALIFYING',
  'QUALIFIED',
  'DEMO_OFFERED',
  'DEMO_BOOKED',
  'NOT_INTERESTED',
  'OPTED_OUT',
  'INVALID',
  'FAILED',
];

export default function LeadsPage() {
  const [leads, setLeads] = useState<Lead[]>([]);
  const [total, setTotal] = useState(0);
  const [status, setStatus] = useState<LeadStatus | ''>('');
  const [search, setSearch] = useState('');
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    setLoading(true);
    api
      .listLeads(status || undefined, search || undefined, DEFAULT_PAGE_SIZE, offset)
      .then((page) => {
        setLeads(page.items);
        setTotal(page.total);
        // A narrower filter can leave the offset past the end — the user is on page 5
        // and would otherwise see an empty list with no indication why.
        const corrected = clampOffset(offset, page.total, DEFAULT_PAGE_SIZE);
        if (corrected !== offset) {
          setOffset(corrected);
        }
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, [status, search, offset]);

  useEffect(load, [load]);

  // Changing a filter returns to the first page: staying on page 5 of a new, shorter
  // result set is never what was meant.
  function changeFilter(apply: () => void) {
    setOffset(0);
    apply();
  }

  return (
    <>
      <Nav />
      <main className="mx-auto max-w-5xl px-6 py-10">
        <h1 className="text-xl font-semibold tracking-tight">Leads</h1>
        <p className="mt-1 text-sm text-slate-600">
          {total} {total === 1 ? 'lead' : 'leads'}
        </p>

        <div className="mt-5 flex flex-wrap gap-3">
          <input
            type="search"
            value={search}
            onChange={(event) => changeFilter(() => setSearch(event.target.value))}
            placeholder="Name, phone, email or company"
            className="w-72 rounded border border-slate-300 px-3 py-2 text-sm"
          />
          <select
            value={status}
            onChange={(event) =>
              changeFilter(() => setStatus(event.target.value as LeadStatus | ''))
            }
            className="rounded border border-slate-300 px-3 py-2 text-sm"
          >
            <option value="">All states</option>
            {STATUSES.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </div>

        {error ? (
          <p className="mt-6 rounded border border-red-200 bg-red-50 p-4 text-sm text-red-700">
            {error}
          </p>
        ) : null}

        {loading ? (
          <p className="mt-6 text-sm text-slate-600">Loading…</p>
        ) : leads.length === 0 ? (
          <p className="mt-6 rounded border border-slate-200 bg-white p-6 text-sm text-slate-600">
            No leads match.
          </p>
        ) : (
          <table className="mt-6 w-full border-collapse text-sm">
            <thead>
              <tr className="border-b border-slate-200 text-left text-slate-600">
                <th className="py-2">Customer</th>
                <th className="py-2">Company</th>
                <th className="py-2">State</th>
                <th className="py-2">Added</th>
              </tr>
            </thead>
            <tbody>
              {leads.map((lead) => (
                <tr key={lead.id} className="border-b border-slate-100">
                  <td className="py-2">
                    <Link href={`/leads/${lead.id}`} className="underline">
                      {lead.full_name ?? lead.phone_e164}
                    </Link>
                    <div className="text-xs text-slate-500">{lead.phone_e164}</div>
                  </td>
                  <td className="py-2">{lead.company ?? '—'}</td>
                  <td className="py-2">
                    <span
                      className={`rounded px-2 py-1 text-xs font-medium ${
                        STATUS_STYLES[lead.status] ?? 'bg-slate-100 text-slate-700'
                      }`}
                    >
                      {lead.status}
                    </span>
                  </td>
                  <td className="py-2 text-slate-600">
                    {new Date(lead.created_at).toLocaleDateString('en-GB')}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {leads.length > 0 ? (
          <Pager
            state={{ total, limit: DEFAULT_PAGE_SIZE, offset }}
            shown={leads.length}
            onChange={setOffset}
          />
        ) : null}
      </main>
    </>
  );
}
