/**
 * Session helpers for server components and middleware.
 *
 * The session cookie is HttpOnly ([D2]), so client-side JavaScript cannot read it.
 * Session state is therefore resolved on the server — either by asking the API who
 * the caller is, or, in middleware, by the cookie's mere presence.
 */

import { cookies } from 'next/headers';

import { getInternalApiBaseUrl } from '@/lib/bff';

export const SESSION_COOKIE_NAME = process.env.SESSION_COOKIE_NAME ?? 'wam_session';

export type Role = 'ADMIN' | 'SALESPERSON';

export interface CurrentUser {
  id: string;
  email: string;
  full_name: string | null;
  role: Role;
  is_active: boolean;
}

/**
 * Resolve the current user by asking the API.
 *
 * Returns null when unauthenticated rather than throwing, so a caller can branch on
 * session state without exception handling.
 */
export async function getCurrentUser(): Promise<CurrentUser | null> {
  const cookieStore = await cookies();
  const token = cookieStore.get(SESSION_COOKIE_NAME);
  if (!token) {
    return null;
  }

  try {
    const response = await fetch(`${getInternalApiBaseUrl()}/api/v1/auth/me`, {
      headers: { cookie: `${SESSION_COOKIE_NAME}=${token.value}` },
      cache: 'no-store',
    });
    if (!response.ok) {
      return null;
    }
    return (await response.json()) as CurrentUser;
  } catch {
    // An unreachable API is not an authenticated session.
    return null;
  }
}

export function isAdmin(user: CurrentUser | null): boolean {
  return user?.role === 'ADMIN';
}
