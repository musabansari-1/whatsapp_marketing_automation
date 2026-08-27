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

import { Pager } from '@/app/pager';
import { AppShell } from '@/components/shell';
import { StatusBadge } from '@/components/status-badge';
import { Alert, EmptyState, PageHeader, Skeleton } from '@/components/ui';
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
    <AppShell>
      <PageHeader title="Leads" description={`${total} ${total === 1 ? 'lead' : 'leads'}`} />

      <div className="mb-6 flex flex-wrap gap-3">
        <input
          type="search"
          value={search}
          onChange={(event) => changeFilter(() => setSearch(event.target.value))}
          placeholder="Name, phone, email or company"
          className="ui-input w-full max-w-sm"
        />
        <select
          value={status}
          onChange={(event) => changeFilter(() => setStatus(event.target.value as LeadStatus | ''))}
          className="ui-select w-full max-w-[14rem]"
        >
          <option value="">All states</option>
          {STATUSES.map((option) => (
            <option key={option} value={option}>
              {option.replaceAll('_', ' ')}
            </option>
          ))}
        </select>
      </div>

      {error ? <Alert className="mb-6">{error}</Alert> : null}

      {loading ? (
        <Skeleton className="h-64" />
      ) : leads.length === 0 ? (
        <EmptyState
          title="No leads match"
          description="Try a different state or search. Leads appear after an audience is confirmed or a customer messages in."
        />
      ) : (
        <>
          <div className="ui-table-wrap">
            <table className="ui-table">
              <thead>
                <tr>
                  <th>Customer</th>
                  <th>Company</th>
                  <th>State</th>
                  <th>Added</th>
                </tr>
              </thead>
              <tbody>
                {leads.map((lead) => (
                  <tr key={lead.id}>
                    <td>
                      <Link
                        href={`/leads/${lead.id}`}
                        className="font-medium text-ink hover:text-forest"
                      >
                        {lead.full_name ?? lead.phone_e164}
                      </Link>
                      <div className="font-mono text-xs text-ink-muted">{lead.phone_e164}</div>
                    </td>
                    <td>{lead.company ?? '—'}</td>
                    <td>
                      <StatusBadge status={lead.status} />
                    </td>
                    <td className="text-ink-muted">
                      {new Date(lead.created_at).toLocaleDateString('en-GB')}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pager
            state={{ total, limit: DEFAULT_PAGE_SIZE, offset }}
            shown={leads.length}
            onChange={setOffset}
          />
        </>
      )}
    </AppShell>
  );
}
