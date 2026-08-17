/**
 * Catch-all BFF proxy route ([D13]).
 *
 * Every dashboard API call from the browser lands here and is forwarded server-side
 * to the Cloud Run API. Because the browser's request is same-origin, the session
 * cookie is same-site and no CORS policy is involved.
 */

import { NextRequest } from 'next/server';

import {
  BffConfigurationError,
  buildForwardHeaders,
  buildResponseHeaders,
  buildTargetUrl,
  CORRELATION_ID_HEADER,
  methodHasBody,
  resolveCorrelationId,
} from '@/lib/bff';

// The proxy must never be statically optimized or cached: responses are
// per-session and authenticated.
export const dynamic = 'force-dynamic';

interface RouteContext {
  params: Promise<{ path: string[] }>;
}

async function proxy(request: NextRequest, context: RouteContext): Promise<Response> {
  const { path } = await context.params;
  const correlationId = resolveCorrelationId(request.headers);

  let targetUrl: string;
  try {
    targetUrl = buildTargetUrl(path ?? [], request.nextUrl.search);
  } catch (error) {
    if (error instanceof BffConfigurationError) {
      // Configuration faults are ours, not the caller's, and the message must not
      // leak upstream infrastructure details to the browser.
      console.error('bff_misconfigured', { correlationId, message: error.message });
      return Response.json(
        { detail: 'The API proxy is not configured.' },
        { status: 500, headers: { [CORRELATION_ID_HEADER]: correlationId } },
      );
    }
    throw error;
  }

  try {
    const upstream = await fetch(targetUrl, {
      method: request.method,
      headers: buildForwardHeaders(request.headers, correlationId),
      body: methodHasBody(request.method) ? await request.arrayBuffer() : undefined,
      redirect: 'manual',
      cache: 'no-store',
    });

    return new Response(upstream.body, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers: buildResponseHeaders(upstream.headers, correlationId),
    });
  } catch (error) {
    // The API being unreachable is a gateway failure, not a 500 from the API.
    console.error('bff_upstream_unreachable', {
      correlationId,
      message: error instanceof Error ? error.message : String(error),
    });
    return Response.json(
      { detail: 'The API is currently unreachable.' },
      { status: 502, headers: { [CORRELATION_ID_HEADER]: correlationId } },
    );
  }
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
export const HEAD = proxy;
