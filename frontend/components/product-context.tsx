/**
 * The active product (§28, Phase 26).
 *
 * Everything under `/p/{slug}` reads its product from here: the scoped API client, the
 * product's segments for filters, and the list of products for the switcher. The slug
 * comes from the URL, never from local state, so a reload or a shared link always opens
 * the same product — and there is no way to be "in" one product while a request goes to
 * another.
 *
 * Products are loaded from the API. Nothing here knows how many there are or what they
 * are called.
 */

'use client';

import Link from 'next/link';
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';

import { ShellSkeleton } from '@/components/shell-skeleton';
import { Alert } from '@/components/ui';
import {
  api,
  ApiError,
  productApi,
  type Product,
  type ProductApi,
  type ProductDetail,
  type SessionUser,
} from '@/lib/api';

export interface ProductContextValue {
  slug: string;
  product: ProductDetail;
  /** Every product, for the switcher. Inactive ones are included and labelled. */
  products: Product[];
  user: SessionUser | null;
  api: ProductApi;
  /** A segment's display name within this product, or null when there is none. */
  segmentName: (segmentId: string | null | undefined) => string | null;
}

const ProductContext = createContext<ProductContextValue | null>(null);

export function ProductProvider({ slug, children }: { slug: string; children: ReactNode }) {
  const scoped = useMemo(() => productApi(slug), [slug]);
  const [product, setProduct] = useState<ProductDetail | null>(null);
  const [products, setProducts] = useState<Product[]>([]);
  const [user, setUser] = useState<SessionUser | null>(null);
  const [error, setError] = useState<{ message: string; notFound: boolean } | null>(null);

  useEffect(() => {
    let cancelled = false;
    setProduct(null);
    setError(null);

    scoped
      .getProduct()
      .then((detail) => {
        if (!cancelled) setProduct(detail);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError({
          message: e instanceof Error ? e.message : String(e),
          notFound: e instanceof ApiError && e.status === 404,
        });
      });
    api
      .listProducts()
      .then((all) => {
        if (!cancelled) setProducts(all);
      })
      .catch(() => undefined);
    api
      .getMe()
      .then((me) => {
        if (!cancelled) setUser(me);
      })
      .catch(() => undefined);

    return () => {
      cancelled = true;
    };
  }, [scoped]);

  const value = useMemo<ProductContextValue | null>(() => {
    if (!product) return null;
    return {
      slug,
      product,
      products,
      user,
      api: scoped,
      segmentName: (segmentId) =>
        segmentId ? (product.segments.find((s) => s.id === segmentId)?.display_name ?? null) : null,
    };
  }, [product, products, scoped, slug, user]);

  if (error) {
    return (
      <div className="mx-auto max-w-xl space-y-4 px-4 py-16">
        <Alert>{error.notFound ? 'There is no product at this address.' : error.message}</Alert>
        <Link href="/" className="text-sm font-semibold text-forest hover:underline">
          Choose a product
        </Link>
      </div>
    );
  }

  if (!value) {
    return <ShellSkeleton />;
  }

  return <ProductContext.Provider value={value}>{children}</ProductContext.Provider>;
}

/** The active product. Only valid inside `/p/{slug}`. */
export function useProduct(): ProductContextValue {
  const value = useContext(ProductContext);
  if (!value) {
    throw new Error('useProduct is only available inside a product route');
  }
  return value;
}

/** The active product, or null on a global page such as Notifications. */
export function useOptionalProduct(): ProductContextValue | null {
  return useContext(ProductContext);
}
