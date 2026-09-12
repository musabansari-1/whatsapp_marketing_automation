/**
 * Product route tests (§28, Phase 26).
 *
 * The rule that matters most is what switching product does to the page an operator is
 * on: it must keep them in the same section and never carry a record from one product
 * into another.
 */

import { describe, expect, it } from 'vitest';

import { queryString } from '@/lib/api';
import {
  pathWithinProduct,
  productHref,
  productSlugFromPath,
  switchProductPath,
} from '@/lib/product-routes';

describe('productHref', () => {
  it('puts the product in the address', () => {
    expect(productHref('some-product')).toBe('/p/some-product');
    expect(productHref('some-product', '/campaigns')).toBe('/p/some-product/campaigns');
    expect(productHref('some-product', 'campaigns/new')).toBe('/p/some-product/campaigns/new');
  });

  it('encodes the slug', () => {
    expect(productHref('a b')).toBe('/p/a%20b');
  });
});

describe('productSlugFromPath', () => {
  it('reads the product from a product path', () => {
    expect(productSlugFromPath('/p/some-product/leads/1')).toBe('some-product');
    expect(productSlugFromPath('/p/some-product')).toBe('some-product');
  });

  it('finds none on a global page', () => {
    expect(productSlugFromPath('/')).toBeNull();
    expect(productSlugFromPath('/notifications')).toBeNull();
  });
});

describe('pathWithinProduct', () => {
  it('strips the product prefix', () => {
    expect(pathWithinProduct('/p/x/leads/1')).toBe('/leads/1');
    expect(pathWithinProduct('/p/x')).toBe('');
    expect(pathWithinProduct('/p/x/')).toBe('');
  });

  it('leaves a global path alone', () => {
    expect(pathWithinProduct('/notifications')).toBe('/notifications');
  });
});

describe('switchProductPath', () => {
  it('keeps the section', () => {
    expect(switchProductPath('/p/first/campaigns', 'second')).toBe('/p/second/campaigns');
  });

  it('never carries a record into another product', () => {
    // The first product's campaign ID means nothing in the second — and is a 404 there.
    expect(switchProductPath('/p/first/campaigns/123', 'second')).toBe('/p/second/campaigns');
    expect(switchProductPath('/p/first/leads/abc', 'second')).toBe('/p/second/leads');
  });

  it('keeps the overview', () => {
    expect(switchProductPath('/p/first', 'second')).toBe('/p/second');
  });

  it('lands on the overview from a global page', () => {
    expect(switchProductPath('/notifications', 'second')).toBe('/p/second');
  });
});

describe('queryString', () => {
  it('omits what is not set', () => {
    expect(queryString({ segment: undefined, status: '', unread_only: false })).toBe('');
  });

  it('keeps zero, because offset 0 is a real value', () => {
    expect(queryString({ limit: 50, offset: 0 })).toBe('?limit=50&offset=0');
  });
});
