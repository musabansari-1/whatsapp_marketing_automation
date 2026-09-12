/**
 * Campaign creation (specification §7, §28, [D57]).
 *
 * Offers only what this product may use: its own confirmed audiences, and templates it
 * owns that Meta has approved for marketing. The API enforces both rules again — the
 * picker is a convenience, not the guard.
 */

'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';

import { useProduct } from '@/components/product-context';
import { SegmentBadge } from '@/components/segment-filter';
import { Alert, Button, Card, PageHeader, Skeleton } from '@/components/ui';
import type { AudienceSummary, ProductTemplate } from '@/lib/api';
import { productHref } from '@/lib/product-routes';

export default function NewCampaignPage() {
  const router = useRouter();
  const { api, slug, product } = useProduct();
  const [audiences, setAudiences] = useState<AudienceSummary[]>([]);
  const [templates, setTemplates] = useState<ProductTemplate[]>([]);
  const [name, setName] = useState('');
  const [audienceId, setAudienceId] = useState('');
  const [templateId, setTemplateId] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([api.listAudiences(), api.listTemplates()])
      .then(([allAudiences, owned]) => {
        // Only a confirmed audience has leads to snapshot (§6).
        setAudiences(allAudiences.filter((a) => a.status === 'CONFIRMED'));
        setTemplates(owned.filter((t) => t.usable_for_campaigns));
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, [api]);

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const detail = await api.createCampaign(name, audienceId, templateId);
      router.push(productHref(slug, `/campaigns/${detail.campaign.id}`));
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not create the campaign.');
    } finally {
      setBusy(false);
    }
  }

  const selectedTemplate = templates.find((t) => t.id === templateId);
  const selectedAudience = audiences.find((a) => a.id === audienceId);

  return (
    <>
      <PageHeader
        back={{ href: productHref(slug, '/campaigns'), label: 'Campaigns' }}
        title={`New ${product.display_name} campaign`}
        description="Creating prepares the campaign and freezes its recipients. It does not send anything."
      />

      {loading ? (
        <Skeleton className="h-64" />
      ) : (
        <div className="max-w-xl space-y-4">
          {!product.is_active ? (
            <Alert tone="warning">
              {product.display_name} is inactive, so new campaigns are refused.
            </Alert>
          ) : null}

          {templates.length === 0 ? (
            <Alert tone="warning">
              {product.display_name} owns no template that Meta has approved for marketing.
              Templates come from Meta; an administrator gives them to a product on the{' '}
              <Link href={productHref(slug, '/templates')} className="font-semibold underline">
                Templates
              </Link>{' '}
              page.
            </Alert>
          ) : null}

          {audiences.length === 0 ? (
            <Alert tone="warning">
              No confirmed audiences for {product.display_name}. Upload and confirm one first.
            </Alert>
          ) : null}

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label htmlFor="name" className="block text-sm font-medium text-ink">
                Campaign name
              </label>
              <input
                id="name"
                type="text"
                required
                value={name}
                onChange={(event) => setName(event.target.value)}
                className="ui-input mt-1.5"
              />
            </div>

            <div>
              <label htmlFor="audience" className="block text-sm font-medium text-ink">
                Audience
              </label>
              <select
                id="audience"
                required
                value={audienceId}
                onChange={(event) => setAudienceId(event.target.value)}
                className="ui-select mt-1.5"
              >
                <option value="">Select a confirmed audience</option>
                {audiences.map((audience) => (
                  <option key={audience.id} value={audience.id}>
                    {audience.name} ({audience.valid_rows} contacts)
                  </option>
                ))}
              </select>
              {selectedAudience?.segment_id ? (
                <p className="mt-1.5 flex items-center gap-2 text-xs text-ink-muted">
                  Targets <SegmentBadge segmentId={selectedAudience.segment_id} />
                </p>
              ) : null}
            </div>

            <div>
              <label htmlFor="template" className="block text-sm font-medium text-ink">
                Approved template
              </label>
              <select
                id="template"
                required
                value={templateId}
                onChange={(event) => setTemplateId(event.target.value)}
                className="ui-select mt-1.5"
              >
                <option value="">Select a template</option>
                {templates.map((template) => (
                  <option key={template.id} value={template.id}>
                    {template.name} ({template.language})
                  </option>
                ))}
              </select>
            </div>

            {selectedTemplate?.body_preview ? (
              <Card>
                <h2 className="text-[11px] font-semibold uppercase tracking-section text-ink-muted">
                  Template preview
                </h2>
                <p className="mt-2 whitespace-pre-wrap text-sm leading-relaxed">
                  {selectedTemplate.body_preview}
                </p>
              </Card>
            ) : null}

            {error ? <Alert>{error}</Alert> : null}

            <Button
              type="submit"
              disabled={busy || !audienceId || !templateId || !product.is_active}
            >
              {busy ? 'Preparing…' : `Create ${product.display_name} campaign`}
            </Button>
          </form>
        </div>
      )}
    </>
  );
}
