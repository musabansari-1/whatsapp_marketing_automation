/**
 * Audience detail (specification §6, §17).
 *
 * Shows every parsed row with its validation outcome, filterable by status, because §6
 * requires the admin to be able to see what was rejected and why. Confirming promotes the
 * valid rows to this product's leads.
 */

'use client';

import { useParams } from 'next/navigation';
import { useCallback, useEffect, useState } from 'react';

import { Pager } from '@/app/pager';
import { useProduct } from '@/components/product-context';
import { SegmentBadge } from '@/components/segment-filter';
import { StatusBadge } from '@/components/status-badge';
import { Alert, Button, FilterChip, PageHeader, Skeleton } from '@/components/ui';
import type { AudienceContact, AudienceSummary, ValidationStatus } from '@/lib/api';
import { DEFAULT_PAGE_SIZE } from '@/lib/pagination';
import { productHref } from '@/lib/product-routes';

const FILTERS: { label: string; value: ValidationStatus | 'ALL' }[] = [
  { label: 'All', value: 'ALL' },
  { label: 'Valid', value: 'VALID' },
  { label: 'Invalid', value: 'INVALID' },
  { label: 'Duplicate', value: 'DUPLICATE' },
];

export default function AudienceDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { api, slug, product } = useProduct();

  const [audience, setAudience] = useState<AudienceSummary | null>(null);
  const [contacts, setContacts] = useState<AudienceContact[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [filter, setFilter] = useState<ValidationStatus | 'ALL'>('ALL');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [summary, page] = await Promise.all([
        api.getAudience(id),
        api.listContacts(id, filter === 'ALL' ? undefined : filter, DEFAULT_PAGE_SIZE, offset),
      ]);
      setAudience(summary);
      setContacts(page.items);
      setTotal(page.total);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load this audience.');
    }
  }, [api, id, filter, offset]);

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
    <>
      {error ? <Alert className="mb-4">{error}</Alert> : null}

      {!audience ? (
        error ? null : (
          <Skeleton className="h-64" />
        )
      ) : (
        <>
          <PageHeader
            back={{ href: productHref(slug, '/audiences'), label: 'Audiences' }}
            title={audience.name}
            description={
              <span className="flex flex-wrap items-center gap-2">
                <SegmentBadge segmentId={audience.segment_id} />
                <span>
                  {audience.total_rows} rows · {audience.valid_rows} valid · {audience.invalid_rows}{' '}
                  invalid · {audience.duplicate_rows} duplicate
                </span>
              </span>
            }
            actions={
              audience.status === 'PENDING_CONFIRMATION' ? (
                product.is_active ? (
                  <Button
                    type="button"
                    onClick={handleConfirm}
                    disabled={busy || audience.valid_rows === 0}
                  >
                    {busy ? 'Confirming…' : `Confirm into ${product.display_name}`}
                  </Button>
                ) : null
              ) : (
                <StatusBadge status="CONFIRMED" />
              )
            }
          />

          <div className="mb-4 flex flex-wrap gap-2">
            {FILTERS.map((option) => (
              <FilterChip
                key={option.value}
                active={filter === option.value}
                onClick={() => {
                  setFilter(option.value);
                  setOffset(0);
                }}
              >
                {option.label}
              </FilterChip>
            ))}
          </div>

          <div className="ui-table-wrap">
            <table className="ui-table">
              <thead>
                <tr>
                  <th>Row</th>
                  <th>Phone</th>
                  <th>Name</th>
                  <th>Email</th>
                  <th>Company</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {contacts.map((contact) => (
                  <tr key={contact.id}>
                    <td className="tabular-nums">{contact.row_number}</td>
                    <td className="font-mono text-xs">{contact.phone_e164 ?? '—'}</td>
                    <td>{contact.full_name ?? '—'}</td>
                    <td>{contact.email ?? '—'}</td>
                    <td>{contact.company ?? '—'}</td>
                    <td>
                      {contact.validation_status === 'VALID' ? (
                        <StatusBadge status="VALID" />
                      ) : (
                        <span
                          className={
                            contact.validation_status === 'DUPLICATE'
                              ? 'text-sm text-amber-800'
                              : 'text-sm text-red-800'
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
          <Pager
            state={{ total, limit: DEFAULT_PAGE_SIZE, offset }}
            shown={contacts.length}
            onChange={setOffset}
          />
        </>
      )}
    </>
  );
}
