// Exchange-style live candle building: WS price ticks paint the FORMING bar's
// high/low/close and ROLL a new candle the instant a timeframe boundary passes —
// the chart no longer waits for the 15s REST bundle poll to show a new bar, and
// the poll (closed bars, up to 15s stale) can be re-tickled afterwards so it never
// clobbers the forming candle.
//
// Pure functions — unit-tested in src/tests/liveBars.test.ts.

import type { OHLCVBar } from '$lib/api/data';

const TIMEFRAME_MS: Record<string, number> = {
	'1m': 60_000,
	'3m': 180_000,
	'5m': 300_000,
	'15m': 900_000,
	'30m': 1_800_000,
	'1h': 3_600_000,
	'2h': 7_200_000,
	'4h': 14_400_000,
	'6h': 21_600_000,
	'8h': 28_800_000,
	'12h': 43_200_000,
	'1d': 86_400_000,
};

export function timeframeToMs(timeframe: string | null | undefined): number {
	const key = String(timeframe || '').trim().toLowerCase();
	const direct = TIMEFRAME_MS[key];
	if (direct) return direct;
	const match = key.match(/^(\d+)\s*(m|h|d)$/);
	if (match) {
		const count = parseInt(match[1], 10);
		const unit = match[2] === 'm' ? 60_000 : match[2] === 'h' ? 3_600_000 : 86_400_000;
		if (Number.isFinite(count) && count > 0) return count * unit;
	}
	return 3_600_000;
}

function parseBarMs(value: string | null | undefined): number | null {
	if (!value) return null;
	const parsed = new Date(value).getTime();
	return Number.isNaN(parsed) ? null : parsed;
}

/**
 * Fold a live tick into a bar series.
 *
 * - Same bucket as the last bar (or a slightly-behind clock): update the forming
 *   bar's close and extend high/low — the last price always paints the latest candle.
 * - Past the last bar's bucket: ROLL a new candle at the epoch-aligned (UTC) bucket
 *   start, opened at the tick price. Volume starts at 0 and is corrected by the next
 *   server bundle; quiet-market gaps are left as gaps (no synthetic empty bars).
 * - Never creates a duplicate bucket, never mutates the input array; returns the
 *   SAME reference when nothing changed so Svelte reactivity stays cheap.
 */
export function applyTickToBars(
	bars: OHLCVBar[],
	price: number,
	nowMs: number,
	timeframe: string | null | undefined,
	maxBars = 2000
): OHLCVBar[] {
	if (!Array.isArray(bars) || bars.length === 0) return bars;
	if (!Number.isFinite(price) || price <= 0 || !Number.isFinite(nowMs)) return bars;

	const lastBar = bars[bars.length - 1];
	const tfMs = timeframeToMs(timeframe);
	const tickBucketMs = Math.floor(nowMs / tfMs) * tfMs;
	const lastBarMs = parseBarMs(lastBar?.timestamp);

	if (lastBarMs !== null && tickBucketMs > lastBarMs) {
		const rolled: OHLCVBar = {
			timestamp: new Date(tickBucketMs).toISOString(),
			open: price,
			high: price,
			low: price,
			close: price,
			volume: 0,
		};
		return [...bars, rolled].slice(-maxBars);
	}

	// Same bucket (or unparseable/skewed timestamps): paint the forming bar.
	const unchanged =
		Math.abs(lastBar.close - price) < 1e-12 &&
		lastBar.high >= price &&
		lastBar.low <= price;
	if (unchanged) return bars;
	const next = [...bars];
	next[next.length - 1] = {
		...lastBar,
		close: price,
		high: Math.max(lastBar.high, price),
		low: Math.min(lastBar.low, price),
	};
	return next;
}
