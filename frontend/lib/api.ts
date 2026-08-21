/**
 * Typed client for the dashboard API.
 *
 * Every call goes to the frontend origin, where the BFF proxy forwards it ([D13]).
 * Nothing here knows the API's real address, which is the point: that value is
 * server-side only and must never reach the browser bundle.
 */

export type AudienceStatus = 'PENDING_CONFIRMATION' | 'CONFIRMED';
export type CampaignStatus =
  'DRAFT' | 'READY' | 'SCHEDULED' | 'PROCESSING' | 'PAUSED' | 'COMPLETED' | 'FAILED' | 'CANCELLED';
export type RecipientStatus =
  'PENDING' | 'QUEUED' | 'SENT' | 'DELIVERED' | 'READ' | 'FAILED' | 'SKIPPED';
export type ValidationStatus = 'VALID' | 'INVALID' | 'DUPLICATE';

export interface AudienceSummary {
  id: string;
  name: string;
  source_filename: string | null;
  status: AudienceStatus;
  total_rows: number;
  valid_rows: number;
  invalid_rows: number;
  duplicate_rows: number;
  created_at: string;
}

export interface AudienceContact {
  id: string;
  row_number: number;
  // The canonical V1 fields ([D25]).
  full_name: string | null;
  phone_e164: string | null;
  email: string | null;
  company: string | null;
  designation: string | null;
  city: string | null;
  validation_status: ValidationStatus;
  validation_error: string | null;
  raw_row: Record<string, unknown>;
}

export interface AudienceContactPage {
  total: number;
  limit: number;
  offset: number;
  items: AudienceContact[];
}

export interface AudienceSchema {
  columns: { source: string; field: string; required: boolean }[];
  is_placeholder: boolean;
  phone_default_region: string;
  max_file_bytes: number;
  max_rows: number;
}

export interface WhatsAppTemplate {
  id: string;
  name: string;
  language: string;
  category: string;
  provider_status: string;
  variable_count: number;
  body_preview: string | null;
}

export interface Campaign {
  id: string;
  name: string;
  status: CampaignStatus;
  audience_id: string;
  template_id: string;
  total_recipients: number;
  sent_count: number;
  delivered_count: number;
  read_count: number;
  failed_count: number;
  skipped_count: number;
  created_at: string;
}

export interface CampaignDetail {
  campaign: Campaign;
  template: WhatsAppTemplate;
  pending_recipients: number;
}

export interface CampaignRecipient {
  id: string;
  lead_id: string;
  phone_e164: string;
  status: RecipientStatus;
  attempts: number;
  provider_message_id: string | null;
  error_message: string | null;
}

export interface CampaignRecipientPage {
  total: number;
  limit: number;
  offset: number;
  items: CampaignRecipient[];
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    ...init,
    credentials: 'same-origin',
    cache: 'no-store',
  });

  if (!response.ok) {
    // The API returns {detail: "..."} for handled errors; fall back to the status
    // when it does not, so the user never sees "undefined".
    let detail = `Request failed (${response.status})`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === 'string') {
        detail = body.detail;
      }
    } catch {
      // Response had no JSON body; the status-based message stands.
    }
    throw new ApiError(detail, response.status);
  }

  return (await response.json()) as T;
}

export const api = {
  audienceSchema: () => request<AudienceSchema>('/audiences/schema'),

  listAudiences: () => request<AudienceSummary[]>('/audiences'),

  getAudience: (id: string) => request<AudienceSummary>(`/audiences/${id}`),

  listContacts: (id: string, status?: ValidationStatus, limit = 50, offset = 0) => {
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (status) {
      params.set('validation_status', status);
    }
    return request<AudienceContactPage>(`/audiences/${id}/contacts?${params}`);
  },

  uploadAudience: (file: File, name: string) => {
    const form = new FormData();
    form.append('file', file);
    form.append('name', name);
    // No Content-Type header: the browser must set the multipart boundary itself.
    return request<{ audience: AudienceSummary; sample: AudienceContact[] }>('/audiences/upload', {
      method: 'POST',
      body: form,
    });
  },

  confirmAudience: (id: string) =>
    request<{ audience: AudienceSummary; leads_upserted: number }>(`/audiences/${id}/confirm`, {
      method: 'POST',
    }),

  // Templates are read-only: [D6] puts approval with Meta, and the sync arrives in
  // Phase 6. Until then this list is empty and no campaign can be created.
  listTemplates: () => request<WhatsAppTemplate[]>('/whatsapp-templates'),

  listCampaigns: () => request<Campaign[]>('/campaigns'),

  getCampaign: (id: string) => request<CampaignDetail>(`/campaigns/${id}`),

  createCampaign: (name: string, audienceId: string, templateId: string) =>
    request<CampaignDetail>('/campaigns', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, audience_id: audienceId, template_id: templateId }),
    }),

  listCampaignRecipients: (id: string, status?: RecipientStatus, limit = 50, offset = 0) => {
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (status) {
      params.set('recipient_status', status);
    }
    return request<CampaignRecipientPage>(`/campaigns/${id}/recipients?${params}`);
  },

  cancelCampaign: (id: string) => request<Campaign>(`/campaigns/${id}/cancel`, { method: 'POST' }),

  sendCampaign: (id: string) =>
    request<{ campaign: Campaign; queued: number; skipped_as_suppressed: number }>(
      `/campaigns/${id}/send`,
      { method: 'POST' },
    ),
};
