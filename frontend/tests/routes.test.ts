/**
 * Route protection tests ([D2], [D13]).
 */

import { describe, expect, it } from 'vitest';

import { isGuardedPath, isPublicPath, LOGIN_PATH, resolveRedirect } from '@/lib/routes';

describe('isGuardedPath', () => {
  it('guards application pages', () => {
    expect(isGuardedPath('/')).toBe(true);
    expect(isGuardedPath('/campaigns')).toBe(true);
  });

  it('leaves the BFF proxy alone', () => {
    // The API answers 401 to a fetch; redirecting it to an HTML login page would
    // turn an authentication error into a confusing parse failure.
    expect(isGuardedPath('/api/v1/auth/login')).toBe(false);
  });

  it('leaves Next internals and assets alone', () => {
    expect(isGuardedPath('/_next/static/chunk.js')).toBe(false);
    expect(isGuardedPath('/favicon.ico')).toBe(false);
  });
});

describe('isPublicPath', () => {
  it('login is public', () => {
    expect(isPublicPath(LOGIN_PATH)).toBe(true);
  });

  it('everything else is not', () => {
    expect(isPublicPath('/')).toBe(false);
    expect(isPublicPath('/leads')).toBe(false);
  });

  it('there is no public registration route', () => {
    // [D2]: accounts come from an admin or the bootstrap command.
    expect(isPublicPath('/register')).toBe(false);
    expect(isPublicPath('/signup')).toBe(false);
  });
});

describe('resolveRedirect', () => {
  it('sends anonymous visitors to login', () => {
    expect(resolveRedirect('/', false)).toBe(LOGIN_PATH);
    expect(resolveRedirect('/campaigns', false)).toBe(LOGIN_PATH);
  });

  it('lets anonymous visitors reach login', () => {
    expect(resolveRedirect(LOGIN_PATH, false)).toBeNull();
  });

  it('lets signed-in users through', () => {
    expect(resolveRedirect('/', true)).toBeNull();
    expect(resolveRedirect('/campaigns', true)).toBeNull();
  });

  it('sends signed-in users away from the login page', () => {
    expect(resolveRedirect(LOGIN_PATH, true)).toBe('/');
  });

  it('never redirects proxy or asset requests', () => {
    expect(resolveRedirect('/api/v1/leads', false)).toBeNull();
    expect(resolveRedirect('/_next/static/x.js', false)).toBeNull();
  });
});
