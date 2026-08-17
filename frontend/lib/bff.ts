/**
 * Backend-for-frontend proxy helpers ([D13]).
 *
 * The browser only ever talks to the frontend origin. This module forwards those
 * requests, server-side, to the Cloud Run API. That is what keeps the session cookie
 * same-site and lets [D2] hold without resorting to `SameSite=None`.
 *
 * Only browser traffic passes through here. The WhatsApp webhook and Cloud Tasks
 * reach the API directly and must never be routed through this proxy.
 */

export const CORRELATION_ID_HEADER = 'x-correlation-id';

/** Hop-by-hop and encoding headers that must not be forwarded verbatim. */
const STRIPPED_REQUEST_HEADERS = new Set([
  'connection',
  'content-length',
  'host',
  'keep-alive',
  'transfer-encoding',
  'upgrade',
  // Let fetch negotiate its own encoding; forwarding the browser's leads to
  // responses we would have to decode ourselves.
  'accept-encoding',
]);

const STRIPPED_RESPONSE_HEADERS = new Set([
  'connection',
  'content-encoding',
  'content-length',
  'keep-alive',
  'transfer-encoding',
  'upgrade',
]);

/** Methods that carry no request body. */
const BODYLESS_METHODS = new Set(['GET', 'HEAD']);

export class BffConfigurationError extends Error {}

/**
 * Resolve the API base URL.
 *
 * Server-side only: this value must never reach the browser bundle, which is why it
 * is read from a non-`NEXT_PUBLIC_` variable.
 */
export function getInternalApiBaseUrl(
  env: Readonly<Record<string, string | undefined>> = process.env,
): string {
  const value = env.INTERNAL_API_BASE_URL;
  if (!value) {
    throw new BffConfigurationError(
      'INTERNAL_API_BASE_URL is not set. The BFF proxy cannot reach the API without it.',
    );
  }
  return value.replace(/\/+$/, '');
}

/**
 * Build the upstream URL for a proxied request.
 *
 * `segments` are the path parts after `/api/`, so `/api/v1/leads?x=1` targets
 * `<base>/api/v1/leads?x=1`. Segments are encoded to prevent a crafted path from
 * escaping the API prefix.
 */
export function buildTargetUrl(
  segments: string[],
  search: string,
  baseUrl: string = getInternalApiBaseUrl(),
): string {
  const path = segments
    .filter((segment) => segment.length > 0 && segment !== '.' && segment !== '..')
    .map((segment) => encodeURIComponent(segment))
    .join('/');
  return `${baseUrl}/api/${path}${search}`;
}

/**
 * Headers to send upstream.
 *
 * Cookies are forwarded so the API can authenticate the session, and a correlation
 * ID is attached so one identifier spans the proxy hop (§21).
 */
export function buildForwardHeaders(incoming: Headers, correlationId: string): Headers {
  const headers = new Headers();
  incoming.forEach((value, key) => {
    if (!STRIPPED_REQUEST_HEADERS.has(key.toLowerCase())) {
      headers.set(key, value);
    }
  });
  headers.set(CORRELATION_ID_HEADER, correlationId);
  return headers;
}

/**
 * Headers to return to the browser.
 *
 * `set-cookie` is preserved so the API's session cookie reaches the browser. The API
 * sets it without a `Domain` attribute, so the browser scopes it to the frontend
 * origin — the mechanism that makes the cookie same-site.
 */
export function buildResponseHeaders(upstream: Headers, correlationId: string): Headers {
  const headers = new Headers();
  upstream.forEach((value, key) => {
    if (!STRIPPED_RESPONSE_HEADERS.has(key.toLowerCase())) {
      headers.set(key, value);
    }
  });

  const setCookie = upstream.getSetCookie?.() ?? [];
  if (setCookie.length > 0) {
    headers.delete('set-cookie');
    for (const cookie of setCookie) {
      headers.append('set-cookie', cookie);
    }
  }

  headers.set(CORRELATION_ID_HEADER, correlationId);
  return headers;
}

export function resolveCorrelationId(incoming: Headers): string {
  return incoming.get(CORRELATION_ID_HEADER) ?? crypto.randomUUID().replace(/-/g, '');
}

export function methodHasBody(method: string): boolean {
  return !BODYLESS_METHODS.has(method.toUpperCase());
}
