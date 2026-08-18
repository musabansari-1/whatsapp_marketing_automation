/**
 * Route protection ([D2], [D13]).
 *
 * Redirects unauthenticated navigation to the login page and signed-in users away
 * from it. The decision logic lives in lib/routes.ts so it is unit-testable.
 *
 * This is routing convenience, not the security boundary: middleware only checks that
 * a cookie exists. Actual verification happens in the API, which rejects a forged or
 * expired session regardless of what middleware allowed through.
 */

import { NextRequest, NextResponse } from 'next/server';

import { resolveRedirect } from '@/lib/routes';
import { SESSION_COOKIE_NAME } from '@/lib/session';

export function middleware(request: NextRequest) {
  const hasSession = request.cookies.has(SESSION_COOKIE_NAME);
  const destination = resolveRedirect(request.nextUrl.pathname, hasSession);

  if (destination) {
    return NextResponse.redirect(new URL(destination, request.url));
  }
  return NextResponse.next();
}

export const config = {
  // Everything except Next internals, static assets and the BFF proxy.
  matcher: ['/((?!_next/static|_next/image|favicon.ico|api).*)'],
};
