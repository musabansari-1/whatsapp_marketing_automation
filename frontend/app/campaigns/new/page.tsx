/**
 * Campaign creation (specification §7).
 *
 * Selects a confirmed audience and an approved template. The template list comes from
 * Meta via the Phase 6 synchronization ([D6]); until that exists the list is empty and
 * a campaign cannot be created, which this page states plainly rather than failing
 * with an unexplained empty dropdown.
 */

'use client';

import { useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';

import { AppShell } from '@/components/shell';
import { Alert, Button, Card, PageHeader, Skeleton } from '@/components/ui';
import { api, type AudienceSummary, type WhatsAppTemplate } from '@/lib/api';

export default function NewCampaignPage() {
  const router = useRouter();
  const [audiences, setAudiences] = useState<AudienceSummary[]>([]);
  const [templates, setTemplates] = useState<WhatsAppTemplate[]>([]);
  const [name, setName] = useState('');
  const [audienceId, setAudienceId] = useState('');
  const [templateId, setTemplateId] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([api.listAudiences(), api.listTemplates()])
      .then(([allAudiences, allTemplates]) => {
        // Only a confirmed audience has leads to snapshot (§6).
        setAudiences(allAudiences.filter((a) => a.status === 'CONFIRMED'));
        setTemplates(allTemplates);
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const detail = await api.createCampaign(name, audienceId, templateId);
      router.push(`/campaigns/${detail.campaign.id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not create the campaign.');
    } finally {
      setBusy(false);
    }
  }

  const selectedTemplate = templates.find((t) => t.id === templateId);

  return (
    <AppShell>
      <PageHeader
        back={{ href: '/campaigns', label: 'Campaigns' }}
        title="New campaign"
        description="Creating prepares the campaign and freezes its recipients. It does not send anything."
      />

      {loading ? (
        <Skeleton className="h-64" />
      ) : (
        <div className="max-w-xl space-y-4">
          {templates.length === 0 ? (
            <Alert tone="warning">
              No approved marketing templates are available. Templates are approved by Meta and
              synchronized into this platform; that synchronization arrives in Phase 6. A campaign
              cannot be created until at least one approved template exists.
            </Alert>
          ) : null}

          {audiences.length === 0 ? (
            <Alert tone="warning">No confirmed audiences. Upload and confirm one first.</Alert>
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

            <Button type="submit" disabled={busy || !audienceId || !templateId}>
              {busy ? 'Preparing…' : 'Create campaign'}
            </Button>
          </form>
        </div>
      )}
    </AppShell>
  );
}
