import { describe, expect, it } from 'vitest';

import { explainError, lastEvent, percent, stuckCount, summarize } from '@/lib/delivery';

describe('summarize', () => {
  it('counts progress cumulatively, so read messages are also delivered and sent', () => {
    // The 26 October broadcast: 7 still awaiting a report, 112 delivered, 21 read.
    const summary = summarize({ SENT: 7, DELIVERED: 112, READ: 21, FAILED: 51 });
    expect(summary).toEqual({
      total: 191,
      waiting: 0,
      sent: 140,
      awaitingReport: 7,
      delivered: 133,
      read: 21,
      failed: 51,
      skipped: 0,
    });
  });

  it('treats pending and queued as waiting', () => {
    expect(summarize({ PENDING: 2, QUEUED: 3 }).waiting).toBe(5);
  });

  it('is all zeros for an empty campaign', () => {
    expect(summarize({}).total).toBe(0);
  });
});

describe('percent', () => {
  it('rounds', () => expect(percent(133, 191)).toBe('70%'));
  it('is empty when the whole is zero', () => expect(percent(0, 0)).toBe(''));
});

describe('explainError', () => {
  it('explains the codes Meta sends on broadcasts', () => {
    expect(explainError('131049', 'This message was not delivered...')?.label).toBe(
      'Meta marketing limit',
    );
    expect(explainError('131026', 'Message undeliverable')?.label).toBe('Not on WhatsApp');
  });

  it("falls back to Meta's own message for an unknown code", () => {
    expect(explainError('999', 'Something new')).toEqual({
      label: 'Something new',
      detail: 'Something new',
    });
  });

  it('is null when there is no error', () => {
    expect(explainError(null, null)).toBeNull();
  });
});

describe('lastEvent', () => {
  it('picks the latest timestamp, not the furthest status', () => {
    expect(
      lastEvent({
        queued_at: '2026-10-26T09:37:00Z',
        sent_at: '2026-10-26T09:38:00Z',
        delivered_at: '2026-10-26T09:39:00Z',
        read_at: null,
        failed_at: null,
      }),
    ).toEqual({ label: 'Delivered', at: '2026-10-26T09:39:00Z' });
  });

  it('is null before anything happened', () => {
    expect(
      lastEvent({
        queued_at: null,
        sent_at: null,
        delivered_at: null,
        read_at: null,
        failed_at: null,
      }),
    ).toBeNull();
  });
});

describe('stuckCount', () => {
  const started = '2026-10-26T09:37:00Z';
  const waiting = summarize({ QUEUED: 29, DELIVERED: 10 });

  it('flags waiting messages once the campaign has been running a while', () => {
    expect(stuckCount(waiting, started, new Date('2026-10-26T09:53:00Z'))).toBe(29);
  });

  it('does not flag a campaign that has only just started', () => {
    expect(stuckCount(waiting, started, new Date('2026-10-26T09:40:00Z'))).toBe(0);
  });

  it('does not flag a campaign that has not started', () => {
    expect(stuckCount(waiting, null)).toBe(0);
  });
});
