/**
 * Typed client for the dashboard API.
 *
 * Every call goes to the frontend origin, where the BFF proxy forwards it ([D13]).
 * Nothing here knows the API's real address, which is the point: that value is
 * server-side only and must never reach the browser bundle.
 */

export type AudienceStatus = 'PENDING_CONFIRMATION' | 'CONFIRMED';
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
  full_name: string | null;
  phone_e164: string | null;
  email: string | null;
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
};
