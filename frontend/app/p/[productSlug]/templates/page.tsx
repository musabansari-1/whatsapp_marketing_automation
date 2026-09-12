/**
 * WhatsApp templates for the active product ([D6], [D57]).
 *
 * One Meta App and one shared WhatsApp Business Account give one template pool, and Meta
 * records no owner on a template. So this page shows two lists: what this product owns,
 * and what nobody owns yet. An administrator turns the second into the first, one
 * template at a time, with an explicit confirmation.
 *
 * Nothing here guesses ownership from a template's name. And an owned template cannot be
 * moved to another product from here: that would change what the first product's
 * campaigns are allowed to send.
 */

'use client';

import { useCallback, useEffect, useState } from 'react';

import { useProduct } from '@/components/product-context';
import { StatusBadge } from '@/components/status-badge';
import {
  Alert,
  Button,
  Card,
  EmptyState,
  PageHeader,
  SectionLabel,
  Skeleton,
} from '@/components/ui';
import { api as globalApi, type ProductTemplate, type WhatsAppTemplate } from '@/lib/api';

export default function TemplatesPage() {
  const { api, product, user } = useProduct();
  const [owned, setOwned] = useState<ProductTemplate[] | null>(null);
  const [unassigned, setUnassigned] = useState<WhatsAppTemplate[] | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const isAdmin = user?.role === 'ADMIN';

  const load = useCallback(() => {
    Promise.all([api.listTemplates(), globalApi.listUnassignedTemplates()])
      .then(([mine, nobodys]) => {
        setOwned(mine);
        setUnassigned(nobodys);
      })
      .catch((e: Error) => setError(e.message));
  }, [api]);

  useEffect(load, [load]);

  async function assign(template: WhatsAppTemplate) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await api.assignTemplate(template.id);
      setNotice(`${template.name} (${template.language}) now belongs to ${product.display_name}.`);
      setConfirming(null);
      load();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not assign the template.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader
        title="Templates"
        description={`Meta-approved message templates ${product.display_name}'s campaigns can use. Approval comes from Meta; ownership is assigned here.`}
      />

      {error ? <Alert className="mb-6">{error}</Alert> : null}
      {notice ? (
        <Alert tone="success" className="mb-6">
          {notice}
        </Alert>
      ) : null}

      <section>
        <SectionLabel>Owned by {product.display_name}</SectionLabel>
        <div className="mt-3">
          {owned === null ? (
            <Skeleton className="h-32" />
          ) : owned.length === 0 ? (
            <EmptyState
              title="This product owns no templates yet"
              description="Templates synchronized from Meta start unassigned. An administrator gives them to a product below."
            />
          ) : (
            <div className="ui-table-wrap">
              <table className="ui-table">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Language</th>
                    <th>Category</th>
                    <th>Meta status</th>
                    <th>Campaigns</th>
                  </tr>
                </thead>
                <tbody>
                  {owned.map((template) => (
                    <tr key={template.id}>
                      <td className="font-mono text-xs">{template.name}</td>
                      <td>{template.language}</td>
                      <td>{template.category}</td>
                      <td>{template.provider_status}</td>
                      <td>
                        {template.usable_for_campaigns ? (
                          <StatusBadge status="READY" />
                        ) : (
                          <span className="text-xs text-ink-muted">
                            Not usable — needs Meta approval as MARKETING
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </section>

      <section className="mt-10">
        <SectionLabel>Unassigned</SectionLabel>
        <p className="mt-1 max-w-2xl text-sm text-ink-muted">
          Found in the shared WhatsApp Business Account and owned by no product, so no campaign can
          use them. Assigning is permanent from this page: an owned template cannot be moved to
          another product here.
        </p>
        {!isAdmin ? (
          <p className="mt-2 text-xs text-ink-muted">Only an administrator can assign templates.</p>
        ) : null}

        <div className="mt-3">
          {unassigned === null ? (
            <Skeleton className="h-32" />
          ) : unassigned.length === 0 ? (
            <Card>
              <p className="text-sm text-ink-muted">Every synchronized template has an owner.</p>
            </Card>
          ) : (
            <ul className="space-y-2">
              {unassigned.map((template) => (
                <li
                  key={template.id}
                  className="rounded-2xl border border-line bg-white p-4 shadow-card"
                >
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="font-mono text-sm font-semibold">{template.name}</p>
                      <p className="mt-0.5 text-xs text-ink-muted">
                        {template.language} · {template.category} · {template.provider_status}
                      </p>
                      {template.body_preview ? (
                        <p className="mt-2 whitespace-pre-wrap text-sm text-ink-muted">
                          {template.body_preview}
                        </p>
                      ) : null}
                    </div>
                    {isAdmin ? (
                      confirming === template.id ? (
                        <div className="flex shrink-0 flex-col items-end gap-2">
                          <p className="max-w-xs text-right text-xs text-ink">
                            Give <strong>{template.name}</strong> to{' '}
                            <strong>{product.display_name}</strong>?
                          </p>
                          <div className="flex gap-2">
                            <Button type="button" onClick={() => assign(template)} disabled={busy}>
                              {busy ? 'Assigning…' : 'Assign'}
                            </Button>
                            <Button
                              type="button"
                              variant="secondary"
                              onClick={() => setConfirming(null)}
                            >
                              Cancel
                            </Button>
                          </div>
                        </div>
                      ) : (
                        <Button
                          type="button"
                          variant="secondary"
                          onClick={() => setConfirming(template.id)}
                        >
                          Assign to {product.display_name}
                        </Button>
                      )
                    ) : null}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      </section>
    </>
  );
}
