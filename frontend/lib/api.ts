/**
 * Typed client for the dashboard API.
 *
 * Every call goes to the frontend origin, where the BFF proxy forwards it ([D13]).
 * Nothing here knows the API's real address, which is the point: that value is
 * server-side only and must never reach the browser bundle.
 *
 * Two halves, mirroring the API (§28, Phase 26):
 *
 * - `api` holds what belongs to the whole organisation: the session, the product list,
 *   notifications, unassigned templates and combined analytics.
 * - `productApi(slug)` holds everything scoped to one product. None of its functions takes
 *   a product argument that could be forgotten or mixed up: the product is fixed when the
 *   client is created, from the URL the operator is looking at.
 */

export type AudienceStatus = 'PENDING_CONFIRMATION' | 'CONFIRMED';
export type CampaignStatus =
  'DRAFT' | 'READY' | 'SCHEDULED' | 'PROCESSING' | 'PAUSED' | 'COMPLETED' | 'FAILED' | 'CANCELLED';
export type RecipientStatus =
  'PENDING' | 'QUEUED' | 'SENT' | 'DELIVERED' | 'READ' | 'FAILED' | 'SKIPPED';
export type ValidationStatus = 'VALID' | 'INVALID' | 'DUPLICATE';

// ---------------------------------------------------------------- products (§28)

export interface Segment {
  id: string;
  slug: string;
  display_name: string;
  display_order: number;
  is_active: boolean;
}

export interface Product {
  id: string;
  /** The permanent route identifier. Never a reason to behave differently. */
  slug: string;
  display_name: string;
  is_active: boolean;
}

export interface ProductDetail extends Product {
  segments: Segment[];
  /** Presence only, so an empty page can explain itself. */
  has_calendar: boolean;
  has_whatsapp_number: boolean;
}

export interface ProductRef {
  id: string;
  slug: string;
  display_name: string;
}

// ---------------------------------------------------------------- audiences

export interface AudienceSummary {
  id: string;
  product_id: string;
  segment_id: string | null;
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

// ---------------------------------------------------------------- leads and conversations

export type LeadStatus =
  | 'NEW'
  | 'CONTACTED'
  | 'RESPONDED'
  | 'INTERESTED'
  | 'QUALIFYING'
  | 'QUALIFIED'
  | 'DEMO_OFFERED'
  | 'DEMO_BOOKED'
  | 'NOT_INTERESTED'
  | 'OPTED_OUT'
  | 'INVALID'
  | 'FAILED';

export type ConversationStatus = 'ACTIVE' | 'WAITING_FOR_USER' | 'BOOKING' | 'CLOSED';

export type DocumentStatus = 'PENDING' | 'PROCESSING' | 'READY' | 'FAILED' | 'ARCHIVED';

export interface Lead {
  id: string;
  product_id: string;
  phone_e164: string;
  full_name: string | null;
  email: string | null;
  company: string | null;
  designation: string | null;
  city: string | null;
  status: LeadStatus;
  /** The AI's extracted facts. Read-only here: [D18] makes it the model's target. */
  qualification_data: Record<string, unknown>;
  opted_out: boolean;
  source_audience_id: string | null;
  last_contacted_at: string | null;
  created_at: string;
}

export interface LeadPage {
  total: number;
  limit: number;
  offset: number;
  items: Lead[];
}

export interface Conversation {
  id: string;
  /** Snapshotted when the conversation began, and never re-derived (§28.5). */
  product_id: string;
  segment_id: string | null;
  lead_id: string;
  status: ConversationStatus;
  last_inbound_at: string | null;
  last_outbound_at: string | null;
  closed_at: string | null;
  created_at: string;
}

export interface ConversationTurn {
  id: string;
  role: string;
  content: string;
  message_id: string | null;
  tool_calls: Record<string, unknown> | null;
  created_at: string;
}

export interface ConversationDetail {
  conversation: Conversation;
  lead: Lead;
  /** Computed per request: whether a free-form reply is still permitted (§8). */
  within_service_window: boolean;
  total_turns: number;
  turns: ConversationTurn[];
}

export interface ConversationPage {
  total: number;
  limit: number;
  offset: number;
  items: Conversation[];
}

// ---------------------------------------------------------------- knowledge

export interface KnowledgeBase {
  id: string;
  product_id: string;
  /** Null means shared across every segment of the product ([D56]). */
  segment_id: string | null;
  name: string;
  description: string | null;
  created_at: string;
}

export interface KnowledgeDocument {
  id: string;
  knowledge_base_id: string;
  title: string;
  source_filename: string | null;
  content_type: string | null;
  byte_size: number | null;
  status: DocumentStatus;
  chunk_count: number;
  /** §17 requires a failed ingestion to say why. */
  error_message: string | null;
  created_at: string;
  archived_at: string | null;
}

// ---------------------------------------------------------------- session and analytics

export interface SessionUser {
  id: string;
  email: string;
  full_name: string | null;
  role: 'ADMIN' | 'SALESPERSON';
  is_active: boolean;
}

export interface Overview {
  campaigns: number;
  campaigns_sending: number;
  messages_sent: number;
  delivered: number;
  read: number;
  failed: number;
  /** People who replied, not messages received. */
  replies: number;
  total_leads: number;
  interested: number;
  qualified: number;
  demos_booked: number;
  unread_notifications: number;
}

export interface LeadFunnel {
  counts: Record<string, number>;
}

// ---------------------------------------------------------------- bookings and notifications

export type BookingStatus = 'PENDING' | 'CONFIRMED' | 'CANCELLED' | 'FAILED';

export interface Booking {
  id: string;
  product_id: string;
  lead_id: string;
  conversation_id: string | null;
  calendar_event_id: string | null;
  start_time: string;
  end_time: string;
  // Per booking, not from configuration: a policy change must not retroactively
  // reinterpret a booking already made ([D8]).
  timezone: string;
  status: BookingStatus;
  meeting_link: string | null;
  customer_email: string | null;
  failure_reason: string | null;
  confirmed_at: string | null;
  cancelled_at: string | null;
  created_at: string;
}

export interface BookingPage {
  total: number;
  limit: number;
  offset: number;
  items: Booking[];
}

export interface Notification {
  id: string;
  product_id: string | null;
  /** The product this notification is about, or null when it concerns none ([D47]). */
  product: ProductRef | null;
  type: 'DEMO_BOOKED';
  title: string;
  message: string;
  related_entity_type: string | null;
  related_entity_id: string | null;
  is_read: boolean;
  read_at: string | null;
  created_at: string;
}

export interface NotificationPage {
  total: number;
  /** Every unread notification, not just those on this page: the badge shows this. */
  unread: number;
  limit: number;
  offset: number;
  items: Notification[];
}

// ---------------------------------------------------------------- templates and campaigns

export interface WhatsAppTemplate {
  id: string;
  /** Null: an unassigned shared-WABA template, usable by no campaign ([D57]). */
  product_id: string | null;
  name: string;
  language: string;
  category: string;
  provider_status: string;
  variable_count: number;
  body_preview: string | null;
}

export interface ProductTemplate extends WhatsAppTemplate {
  /** Approved by Meta, MARKETING, and owned by this product — as campaign creation checks. */
  usable_for_campaigns: boolean;
}

export interface Campaign {
  id: string;
  product_id: string;
  segment_id: string | null;
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

// ---------------------------------------------------------------- transport

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

type QueryValue = string | number | boolean | null | undefined;

/** A query string from the values that are set. `false`, empty and missing are omitted. */
export function queryString(params: Record<string, QueryValue>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '' || value === false) {
      continue;
    }
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : '';
}

function postJson(body: unknown): RequestInit {
  return {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  };
}

// ---------------------------------------------------------------- organisation-wide

export const api = {
  getMe: () => request<SessionUser>('/auth/me'),

  /** Every product, active or not. The switcher offers the active ones. */
  listProducts: () => request<Product[]>('/products'),

  // Notifications are always the current user's: no route takes a user ID, so one
  // salesperson cannot read or clear another's ([D3], section 13). `product` narrows the
  // page; the unread count stays whole because it is the badge.
  listNotifications: (
    options: { unreadOnly?: boolean; product?: string; limit?: number; offset?: number } = {},
  ) =>
    request<NotificationPage>(
      `/notifications${queryString({
        unread_only: options.unreadOnly,
        product: options.product,
        limit: options.limit ?? 50,
        offset: options.offset ?? 0,
      })}`,
    ),

  markNotificationRead: (id: string) =>
    request<Notification>(`/notifications/${id}/read`, { method: 'POST' }),

  markAllNotificationsRead: () =>
    request<{ marked_read: number }>('/notifications/read-all', { method: 'POST' }),

  /** Shared-WABA templates no product owns yet ([D57]). */
  listUnassignedTemplates: () => request<WhatsAppTemplate[]>('/whatsapp-templates/unassigned'),

  /** Every product combined, asked for explicitly ([D53]). */
  getCombinedOverview: () => request<Overview>('/analytics/overview'),

  getCombinedLeadFunnel: () => request<LeadFunnel>('/analytics/leads-by-status'),
};

// ---------------------------------------------------------------- one product

export function productApi(slug: string) {
  const base = `/p/${encodeURIComponent(slug)}`;

  return {
    getProduct: () => request<ProductDetail>(base),

    audienceSchema: () => request<AudienceSchema>(`${base}/audiences/schema`),

    listAudiences: (segment?: string) =>
      request<AudienceSummary[]>(`${base}/audiences${queryString({ segment })}`),

    getAudience: (id: string) => request<AudienceSummary>(`${base}/audiences/${id}`),

    listContacts: (id: string, status?: ValidationStatus, limit = 50, offset = 0) =>
      request<AudienceContactPage>(
        `${base}/audiences/${id}/contacts${queryString({ validation_status: status, limit, offset })}`,
      ),

    uploadAudience: (file: File, name: string, segmentId?: string) => {
      const form = new FormData();
      form.append('file', file);
      form.append('name', name);
      if (segmentId) {
        form.append('segment_id', segmentId);
      }
      // No Content-Type header: the browser must set the multipart boundary itself.
      return request<{ audience: AudienceSummary; sample: AudienceContact[] }>(
        `${base}/audiences/upload`,
        { method: 'POST', body: form },
      );
    },

    confirmAudience: (id: string) =>
      request<{ audience: AudienceSummary; leads_upserted: number }>(
        `${base}/audiences/${id}/confirm`,
        { method: 'POST' },
      ),

    // Every template this product owns, with whether its campaigns could use it.
    listTemplates: () => request<ProductTemplate[]>(`${base}/whatsapp-templates`),

    /** ADMIN-only. Gives an unassigned template to this product; never moves an owned one. */
    assignTemplate: (templateId: string) =>
      request<ProductTemplate>(`${base}/whatsapp-templates/${templateId}/assign`, {
        method: 'POST',
      }),

    listCampaigns: (segment?: string) =>
      request<Campaign[]>(`${base}/campaigns${queryString({ segment })}`),

    getCampaign: (id: string) => request<CampaignDetail>(`${base}/campaigns/${id}`),

    createCampaign: (name: string, audienceId: string, templateId: string) =>
      request<CampaignDetail>(
        `${base}/campaigns`,
        postJson({ name, audience_id: audienceId, template_id: templateId }),
      ),

    listCampaignRecipients: (id: string, status?: RecipientStatus, limit = 50, offset = 0) =>
      request<CampaignRecipientPage>(
        `${base}/campaigns/${id}/recipients${queryString({ recipient_status: status, limit, offset })}`,
      ),

    cancelCampaign: (id: string) =>
      request<Campaign>(`${base}/campaigns/${id}/cancel`, { method: 'POST' }),

    sendCampaign: (id: string) =>
      request<{ campaign: Campaign; queued: number; skipped_as_suppressed: number }>(
        `${base}/campaigns/${id}/send`,
        { method: 'POST' },
      ),

    listBookings: (
      options: {
        status?: BookingStatus;
        segment?: string;
        leadId?: string;
        limit?: number;
        offset?: number;
      } = {},
    ) =>
      request<BookingPage>(
        `${base}/bookings${queryString({
          status: options.status,
          segment: options.segment,
          lead_id: options.leadId,
          limit: options.limit ?? 50,
          offset: options.offset ?? 0,
        })}`,
      ),

    getBooking: (id: string) => request<Booking>(`${base}/bookings/${id}`),

    listLeads: (
      options: {
        status?: LeadStatus;
        search?: string;
        segment?: string;
        limit?: number;
        offset?: number;
      } = {},
    ) =>
      request<LeadPage>(
        `${base}/leads${queryString({
          status: options.status,
          search: options.search,
          segment: options.segment,
          limit: options.limit ?? 50,
          offset: options.offset ?? 0,
        })}`,
      ),

    getLead: (id: string) => request<Lead>(`${base}/leads/${id}`),

    getLeadConversation: (id: string) =>
      request<ConversationDetail>(`${base}/leads/${id}/conversation`),

    listConversations: (
      options: {
        status?: ConversationStatus;
        segment?: string;
        limit?: number;
        offset?: number;
      } = {},
    ) =>
      request<ConversationPage>(
        `${base}/conversations${queryString({
          status: options.status,
          segment: options.segment,
          limit: options.limit ?? 50,
          offset: options.offset ?? 0,
        })}`,
      ),

    getConversation: (id: string) => request<ConversationDetail>(`${base}/conversations/${id}`),

    listKnowledgeBases: () => request<KnowledgeBase[]>(`${base}/knowledge-bases`),

    createKnowledgeBase: (name: string, description: string | null, segmentId: string | null) =>
      request<KnowledgeBase>(
        `${base}/knowledge-bases`,
        postJson({ name, description, segment_id: segmentId }),
      ),

    listDocuments: (knowledgeBaseId: string, includeArchived = false) =>
      request<KnowledgeDocument[]>(
        `${base}/knowledge-bases/${knowledgeBaseId}/documents${queryString({
          include_archived: includeArchived,
        })}`,
      ),

    uploadDocument: (knowledgeBaseId: string, file: File) => {
      const form = new FormData();
      form.append('file', file);
      // No Content-Type header: the browser must set the multipart boundary itself.
      return request<KnowledgeDocument>(`${base}/knowledge-bases/${knowledgeBaseId}/documents`, {
        method: 'POST',
        body: form,
      });
    },

    archiveDocument: (knowledgeBaseId: string, documentId: string) =>
      request<KnowledgeDocument>(
        `${base}/knowledge-bases/${knowledgeBaseId}/documents/${documentId}/archive`,
        { method: 'POST' },
      ),

    getOverview: (segment?: string) =>
      request<Overview>(`${base}/analytics/overview${queryString({ segment })}`),

    getLeadFunnel: (segment?: string) =>
      request<LeadFunnel>(`${base}/analytics/leads-by-status${queryString({ segment })}`),
  };
}

export type ProductApi = ReturnType<typeof productApi>;
