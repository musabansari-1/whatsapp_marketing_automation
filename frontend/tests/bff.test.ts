/**
 * BFF proxy tests ([D13]).
 *
 * The security-relevant behaviours are: cookies survive both directions (so [D2]
 * session auth works through the proxy), a crafted path cannot escape the API prefix,
 * and the correlation ID spans the hop.
 */

import { describe, expect, it } from 'vitest';

import {
  BffConfigurationError,
  buildForwardHeaders,
  buildResponseHeaders,
  buildTargetUrl,
  CORRELATION_ID_HEADER,
  getInternalApiBaseUrl,
  methodHasBody,
  resolveCorrelationId,
} from '@/lib/bff';

const BASE = 'https://api.example.com';

describe('getInternalApiBaseUrl', () => {
  it('reads the configured base URL', () => {
    expect(getInternalApiBaseUrl({ INTERNAL_API_BASE_URL: BASE })).toBe(BASE);
  });

  it('strips trailing slashes', () => {
    expect(getInternalApiBaseUrl({ INTERNAL_API_BASE_URL: `${BASE}///` })).toBe(BASE);
  });

  it('throws when unset rather than defaulting', () => {
    expect(() => getInternalApiBaseUrl({})).toThrow(BffConfigurationError);
  });
});

describe('buildTargetUrl', () => {
  it('maps proxied segments onto the API path', () => {
    expect(buildTargetUrl(['v1', 'leads'], '', BASE)).toBe(`${BASE}/api/v1/leads`);
  });

  it('preserves the query string', () => {
    expect(buildTargetUrl(['v1', 'leads'], '?status=NEW', BASE)).toBe(
      `${BASE}/api/v1/leads?status=NEW`,
    );
  });

  it('drops traversal segments so the API prefix cannot be escaped', () => {
    expect(buildTargetUrl(['v1', '..', '..', 'admin'], '', BASE)).toBe(`${BASE}/api/v1/admin`);
  });

  it('encodes segments', () => {
    expect(buildTargetUrl(['v1', 'a b'], '', BASE)).toBe(`${BASE}/api/v1/a%20b`);
  });

  it('handles an empty path', () => {
    expect(buildTargetUrl([], '', BASE)).toBe(`${BASE}/api/`);
  });
});

describe('buildForwardHeaders', () => {
  it('forwards cookies so the API can authenticate the session', () => {
    const incoming = new Headers({ cookie: 'wam_session=abc123' });
    expect(buildForwardHeaders(incoming, 'corr-1').get('cookie')).toBe('wam_session=abc123');
  });

  it('attaches the correlation ID', () => {
    expect(buildForwardHeaders(new Headers(), 'corr-1').get(CORRELATION_ID_HEADER)).toBe('corr-1');
  });

  it('overrides any client-supplied correlation ID with the resolved one', () => {
    const incoming = new Headers({ [CORRELATION_ID_HEADER]: 'client-value' });
    expect(buildForwardHeaders(incoming, 'resolved').get(CORRELATION_ID_HEADER)).toBe('resolved');
  });

  it('strips hop-by-hop headers', () => {
    const incoming = new Headers({
      host: 'frontend.example.com',
      connection: 'keep-alive',
      'content-length': '42',
      'accept-encoding': 'gzip',
      'x-custom': 'kept',
    });
    const headers = buildForwardHeaders(incoming, 'corr-1');

    expect(headers.get('host')).toBeNull();
    expect(headers.get('connection')).toBeNull();
    expect(headers.get('content-length')).toBeNull();
    expect(headers.get('accept-encoding')).toBeNull();
    expect(headers.get('x-custom')).toBe('kept');
  });
});

describe('buildResponseHeaders', () => {
  it('preserves set-cookie so the session cookie reaches the browser', () => {
    const upstream = new Headers();
    upstream.append('set-cookie', 'wam_session=abc; HttpOnly; Path=/; SameSite=Lax');
    const headers = buildResponseHeaders(upstream, 'corr-1');
    expect(headers.getSetCookie()).toContain('wam_session=abc; HttpOnly; Path=/; SameSite=Lax');
  });

  it('preserves multiple set-cookie headers', () => {
    const upstream = new Headers();
    upstream.append('set-cookie', 'a=1; Path=/');
    upstream.append('set-cookie', 'b=2; Path=/');
    expect(buildResponseHeaders(upstream, 'corr-1').getSetCookie()).toHaveLength(2);
  });

  it('strips content-encoding, which fetch has already decoded', () => {
    const upstream = new Headers({
      'content-encoding': 'gzip',
      'content-type': 'application/json',
    });
    const headers = buildResponseHeaders(upstream, 'corr-1');
    expect(headers.get('content-encoding')).toBeNull();
    expect(headers.get('content-type')).toBe('application/json');
  });

  it('echoes the correlation ID', () => {
    expect(buildResponseHeaders(new Headers(), 'corr-1').get(CORRELATION_ID_HEADER)).toBe('corr-1');
  });
});

describe('resolveCorrelationId', () => {
  it('adopts an incoming ID', () => {
    const incoming = new Headers({ [CORRELATION_ID_HEADER]: 'from-client' });
    expect(resolveCorrelationId(incoming)).toBe('from-client');
  });

  it('generates one when absent', () => {
    expect(resolveCorrelationId(new Headers())).toMatch(/^[0-9a-f]{32}$/);
  });

  it('generates distinct IDs', () => {
    expect(resolveCorrelationId(new Headers())).not.toBe(resolveCorrelationId(new Headers()));
  });
});

describe('methodHasBody', () => {
  it.each(['POST', 'PUT', 'PATCH', 'DELETE'])('%s carries a body', (method) => {
    expect(methodHasBody(method)).toBe(true);
  });

  it.each(['GET', 'HEAD', 'get'])('%s carries no body', (method) => {
    expect(methodHasBody(method)).toBe(false);
  });
});
