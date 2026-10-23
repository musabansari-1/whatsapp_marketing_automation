import { describe, expect, it } from 'vitest';

import {
  dayLabel,
  factLabel,
  factValue,
  formatPhone,
  humanizeStatus,
  replyWindowLeft,
  splitLinks,
  whatsappUrl,
} from '@/lib/format';

describe('formatPhone', () => {
  it('groups an Indian mobile number', () => {
    expect(formatPhone('+919800000101')).toBe('+91 98000 00101');
  });

  it('leaves other numbers as stored', () => {
    expect(formatPhone('+447700900123')).toBe('+447700900123');
    expect(formatPhone(null)).toBe('');
  });
});

describe('whatsappUrl', () => {
  it('drops the plus sign', () => {
    expect(whatsappUrl('+919800000101')).toBe('https://wa.me/919800000101');
  });
});

describe('labels', () => {
  it('humanizes a state', () => {
    expect(humanizeStatus('NOT_INTERESTED')).toBe('Not interested');
  });

  it('names known facts and humanizes the rest', () => {
    expect(factLabel('registration_intent')).toBe('Wants to register');
    expect(factLabel('preferred_city')).toBe('Preferred city');
  });

  it('shows booleans as Yes and No', () => {
    expect(factValue(true)).toBe('Yes');
    expect(factValue(false)).toBe('No');
    expect(factValue(['a', 'b'])).toBe('a, b');
    expect(factValue('')).toBe('—');
  });
});

describe('splitLinks', () => {
  it('shortens a tracked link but keeps the full address', () => {
    const href =
      'https://lifeherbagroup.com/join/doctor?utm_source=whatsapp&utm_campaign=lifeherba_doctors';
    expect(splitLinks(`Register here: ${href}. Thanks!`)).toEqual([
      { kind: 'text', text: 'Register here: ' },
      { kind: 'link', href, label: 'lifeherbagroup.com/join/doctor' },
      { kind: 'text', text: '. Thanks!' },
    ]);
  });

  it('returns plain text untouched', () => {
    expect(splitLinks('Hello')).toEqual([{ kind: 'text', text: 'Hello' }]);
  });
});

describe('replyWindowLeft', () => {
  const now = Date.parse('2026-10-23T12:00:00Z');

  it('counts down from the last customer message', () => {
    expect(replyWindowLeft('2026-10-23T11:00:00Z', now)).toBe('23h 0m');
    expect(replyWindowLeft('2026-10-22T12:20:00Z', now)).toBe('20m');
  });

  it('is null once the window has closed or before any message', () => {
    expect(replyWindowLeft('2026-10-22T11:00:00Z', now)).toBeNull();
    expect(replyWindowLeft(null, now)).toBeNull();
  });
});

describe('dayLabel', () => {
  const now = new Date(2026, 9, 23, 15, 0);

  it('names today and yesterday', () => {
    expect(dayLabel(new Date(2026, 9, 23, 9, 0).toISOString(), now)).toBe('Today');
    expect(dayLabel(new Date(2026, 9, 22, 23, 0).toISOString(), now)).toBe('Yesterday');
    expect(dayLabel(new Date(2026, 9, 20, 9, 0).toISOString(), now)).toBe('20 Oct 2026');
  });
});
