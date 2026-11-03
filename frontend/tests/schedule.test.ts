import { describe, expect, it } from 'vitest';

import {
  defaultScheduleTime,
  fromLocalInputValue,
  MAX_AHEAD_MS,
  MIN_LEAD_MS,
  scheduleProblem,
  timeUntil,
  toLocalInputValue,
} from '@/lib/schedule';

describe('local input values', () => {
  it('round-trips a picked time in the viewer clock', () => {
    const picked = fromLocalInputValue('2026-11-05T10:30');
    expect(picked).not.toBeNull();
    expect(picked!.getHours()).toBe(10);
    expect(picked!.getMinutes()).toBe(30);
    expect(toLocalInputValue(picked!)).toBe('2026-11-05T10:30');
  });

  it('sends an instant with a timezone, never a bare clock time', () => {
    const picked = fromLocalInputValue('2026-11-05T10:30')!;
    expect(picked.toISOString()).toMatch(/Z$/);
    expect(new Date(picked.toISOString()).getTime()).toBe(picked.getTime());
  });

  it('refuses malformed and impossible values', () => {
    expect(fromLocalInputValue('')).toBeNull();
    expect(fromLocalInputValue('2026-11-05')).toBeNull();
    expect(fromLocalInputValue('2026-02-31T10:00')).toBeNull();
    expect(fromLocalInputValue('2026-11-05T25:00')).toBeNull();
  });
});

describe('scheduleProblem', () => {
  const now = new Date('2026-11-04T06:00:00Z');

  it('accepts a time comfortably ahead', () => {
    expect(scheduleProblem(new Date(now.getTime() + 3_600_000), now)).toBeNull();
  });

  it('accepts the minimum lead itself', () => {
    expect(scheduleProblem(new Date(now.getTime() + MIN_LEAD_MS), now)).toBeNull();
  });

  it('refuses a time inside the minimum lead', () => {
    expect(scheduleProblem(new Date(now.getTime() + MIN_LEAD_MS - 1000), now)).toMatch(/2 minutes/);
  });

  it('refuses a past time', () => {
    expect(scheduleProblem(new Date(now.getTime() - 60_000), now)).not.toBeNull();
  });

  it('refuses a time past 29 days', () => {
    expect(scheduleProblem(new Date(now.getTime() + MAX_AHEAD_MS + 1000), now)).toMatch(/29 days/);
  });

  it('asks for a time when none is picked', () => {
    expect(scheduleProblem(null, now)).toMatch(/Pick/);
  });
});

describe('defaultScheduleTime', () => {
  it('suggests a whole hour at least 30 minutes away', () => {
    const now = new Date(2026, 10, 4, 9, 45);
    const suggested = defaultScheduleTime(now);
    expect(suggested.getMinutes()).toBe(0);
    expect(suggested.getTime() - now.getTime()).toBeGreaterThanOrEqual(30 * 60_000);
    expect(scheduleProblem(suggested, now)).toBeNull();
  });
});

describe('timeUntil', () => {
  const now = new Date('2026-11-04T06:00:00Z');
  const at = (ms: number) => new Date(now.getTime() + ms).toISOString();

  it('counts minutes, hours and days', () => {
    expect(timeUntil(at(5 * 60_000), now)).toBe('in 5m');
    expect(timeUntil(at(200 * 60_000), now)).toBe('in 3h 20m');
    expect(timeUntil(at(3 * 86_400_000), now)).toBe('in 3 days');
  });

  it('says now once the moment has passed', () => {
    expect(timeUntil(at(-1000), now)).toBe('now');
  });
});
