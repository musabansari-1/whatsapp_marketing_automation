/**
 * Route protection policy.
 *
 * Kept as pure functions so the rules can be tested without spinning up Next.js,
 * and so middleware and pages cannot drift from one another.
 */

export const LOGIN_PATH = '/login';

/** Paths reachable without a session. Everything else requires one. */
const PUBLIC_PATHS = new Set<string>([LOGIN_PATH]);

/** Prefixes middleware never touches. */
// `/icon` is the browser tab icon and `/brand` holds product logos: both are needed
// on the login page, before there is a session.
const UNGUARDED_PREFIXES = ['/_next', '/favicon', '/icon', '/brand', '/api'];

/**
 * Whether middleware should evaluate this path at all.
 *
 * `/api` is excluded because it is the BFF proxy: the API performs its own
 * authentication and must be free to answer 401 to a fetch, rather than being
 * redirected to an HTML login page.
 */
export function isGuardedPath(pathname: string): boolean {
  return !UNGUARDED_PREFIXES.some((prefix) => pathname.startsWith(prefix));
}

export function isPublicPath(pathname: string): boolean {
  return PUBLIC_PATHS.has(pathname);
}

/**
 * Decide where a request should go.
 *
 * `hasSession` reflects only the presence of a cookie. Middleware deliberately does
 * not validate it — that would mean a network call on every navigation, and the
 * server components and API behind it verify properly. A forged cookie gets past
 * middleware and is then rejected by the API, so this is a routing convenience, not
 * the security boundary.
 */
export function resolveRedirect(pathname: string, hasSession: boolean): string | null {
  if (!isGuardedPath(pathname)) {
    return null;
  }
  if (!hasSession && !isPublicPath(pathname)) {
    return LOGIN_PATH;
  }
  if (hasSession && pathname === LOGIN_PATH) {
    // Already signed in; no reason to show the login form again.
    return '/';
  }
  return null;
}
