import { describe, expect, it } from 'vitest';

import { applyTickToBars, timeframeToMs } from '../lib/utils/liveBars';
import type { OHLCVBar } from '../lib/api/data';

function bar(timestamp: string, close = 100, overrides: Partial<OHLCVBar> = {}): OHLCVBar {
	return { timestamp, open: close, high: close, low: close, close, volume: 10, ...overrides };
}

const T0 = '2026-07-02T10:00:00+00:00'; // 1m bucket start
const T0_MS = new Date(T0).getTime();

describe('timeframeToMs', () => {
	it('maps the chart timeframes', () => {
		expect(timeframeToMs('1m')).toBe(60_000);
		expect(timeframeToMs('5M')).toBe(300_000);
		expect(timeframeToMs('30m')).toBe(1_800_000);
		expect(timeframeToMs('1h')).toBe(3_600_000);
		expect(timeframeToMs('4h')).toBe(14_400_000);
		expect(timeframeToMs('1d')).toBe(86_400_000);
	});

	it('falls back to 1h on unknown input', () => {
		expect(timeframeToMs('')).toBe(3_600_000);
		expect(timeframeToMs('weird')).toBe(3_600_000);
		expect(timeframeToMs(null)).toBe(3_600_000);
	});
});

describe('applyTickToBars', () => {
	it('paints the forming bar within the same bucket', () => {
		const bars = [bar(T0, 100)];
		const next = applyTickToBars(bars, 101.5, T0_MS + 30_000, '1m');
		expect(next).toHaveLength(1);
		expect(next[0].close).toBe(101.5);
		expect(next[0].high).toBe(101.5);
		expect(next[0].low).toBe(100);
		expect(next[0].open).toBe(100); // open never repainted
		expect(next[0].volume).toBe(10); // server volume preserved
	});

	it('extends the low on a down tick', () => {
		const bars = [bar(T0, 100)];
		const next = applyTickToBars(bars, 98.2, T0_MS + 10_000, '1m');
		expect(next[0].low).toBe(98.2);
		expect(next[0].high).toBe(100);
		expect(next[0].close).toBe(98.2);
	});

	it('ROLLS a new candle when the timeframe boundary passes', () => {
		const bars = [bar(T0, 100)];
		const next = applyTickToBars(bars, 102, T0_MS + 61_000, '1m'); // next minute
		expect(next).toHaveLength(2);
		const rolled = next[1];
		expect(rolled.timestamp).toBe(new Date(T0_MS + 60_000).toISOString()); // bucket-aligned
		expect(rolled.open).toBe(102);
		expect(rolled.high).toBe(102);
		expect(rolled.low).toBe(102);
		expect(rolled.close).toBe(102);
		expect(rolled.volume).toBe(0); // corrected by the next server bundle
		expect(next[0]).toEqual(bars[0]); // the closed candle is left untouched
	});

	it('never duplicates a bucket when re-applied after a bundle reload', () => {
		const bars = [bar(T0, 100)];
		const afterRoll = applyTickToBars(bars, 102, T0_MS + 61_000, '1m');
		// A second tick in the SAME new bucket must update, not append.
		const again = applyTickToBars(afterRoll, 103, T0_MS + 62_000, '1m');
		expect(again).toHaveLength(2);
		expect(again[1].close).toBe(103);
		expect(again[1].high).toBe(103);
	});

	it('folds a slightly-behind clock tick into the forming bar instead of dropping it', () => {
		// Server already delivered the 10:01 bar; our clock still reads 10:00:59.9.
		const bars = [bar(T0, 100), bar('2026-07-02T10:01:00+00:00', 100.5)];
		const next = applyTickToBars(bars, 99, T0_MS + 59_900, '1m');
		expect(next).toHaveLength(2);
		expect(next[1].close).toBe(99);
		expect(next[1].low).toBe(99);
	});

	it('aligns rolled buckets for 4h and 1d to UTC epoch boundaries', () => {
		const fourH = [bar('2026-07-02T08:00:00+00:00', 100)];
		const rolled4h = applyTickToBars(fourH, 101, new Date('2026-07-02T12:00:05Z').getTime(), '4h');
		expect(rolled4h[1].timestamp).toBe('2026-07-02T12:00:00.000Z');

		const oneD = [bar('2026-07-01T00:00:00+00:00', 100)];
		const rolled1d = applyTickToBars(oneD, 101, new Date('2026-07-02T00:00:30Z').getTime(), '1d');
		expect(rolled1d[1].timestamp).toBe('2026-07-02T00:00:00.000Z');
	});

	it('returns the same reference when nothing changes', () => {
		const bars = [bar(T0, 100)];
		expect(applyTickToBars(bars, 100, T0_MS + 5_000, '1m')).toBe(bars);
		expect(applyTickToBars([], 100, T0_MS, '1m')).toEqual([]);
		expect(applyTickToBars(bars, 0, T0_MS, '1m')).toBe(bars); // bad price
		expect(applyTickToBars(bars, Number.NaN, T0_MS, '1m')).toBe(bars);
	});

	it('caps the series length on roll', () => {
		const bars = [bar(T0, 100), bar('2026-07-02T10:01:00+00:00', 100)];
		const next = applyTickToBars(bars, 101, T0_MS + 121_000, '1m', 2);
		expect(next).toHaveLength(2);
		expect(next[1].close).toBe(101); // newest kept, oldest dropped
		expect(next[0].timestamp).toBe('2026-07-02T10:01:00+00:00');
	});
});
