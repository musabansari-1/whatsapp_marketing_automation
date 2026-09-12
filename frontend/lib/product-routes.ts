/**
 * Product-scoped dashboard routes (§28, Phase 26).
 *
 * Every product view lives under `/p/{slug}`, so the product is part of the address and an
 * operator can always see — and bookmark — which product they are working in. Kept as pure
 * functions so the rules, especially what switching product does to the current page, can
 * be tested without rendering.
 *
 * The slug is a permanent route identifier. Nothing here, or anywhere in the dashboard,
 * branches on which product it names.
 */

export const PRODUCT_ROOT = '/p';

/** `/p/{slug}` plus an optional path inside the product. */
export function productHref(slug: string, path = ''): string {
  const base = `${PRODUCT_ROOT}/${encodeURIComponent(slug)}`;
  if (!path || path === '/') {
    return base;
  }
  return `${base}${path.startsWith('/') ? path : `/${path}`}`;
}

/** The product slug a path is inside, or null for a global page. */
export function productSlugFromPath(pathname: string): string | null {
  const match = /^\/p\/([^/?#]+)/.exec(pathname);
  return match?.[1] ? decodeURIComponent(match[1]) : null;
}

/**
 * The part of a path inside its product: `/p/x/leads/1` is `/leads/1`, and `/p/x` is the
 * empty string. A global path is returned unchanged.
 */
export function pathWithinProduct(pathname: string): string {
  const match = /^\/p\/[^/]+(\/.*)?$/.exec(pathname);
  if (!match) {
    return pathname;
  }
  const rest = match[1] ?? '';
  return rest === '/' ? '' : rest;
}

/**
 * Where switching to another product should land.
 *
 * The same section of the other product — never the same record. An ID from one product
 * means nothing in another, and the API deliberately answers 404 for it, so
 * `/p/a/campaigns/123` becomes `/p/b/campaigns`. From a global page the switch lands on
 * the product's overview.
 */
export function switchProductPath(pathname: string, slug: string): string {
  if (productSlugFromPath(pathname) === null) {
    return productHref(slug);
  }
  const section = pathWithinProduct(pathname).split('/').filter(Boolean)[0];
  return productHref(slug, section ? `/${section}` : '');
}
