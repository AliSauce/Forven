import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import { startNotificationRouter, stopNotificationRouter } from '../lib/stores/notificationRouter';
import { toasts, snoozeUntil } from '../lib/stores/processTracker';
import { navEventPulses, markNavIndicatorSeen } from '../lib/stores/navMetrics';
import { setRoute } from '../lib/stores/pageContext';

function emit(detail: Record<string, unknown>): void {
	window.dispatchEvent(new CustomEvent('forven:event', { detail }));
}

describe('notificationRouter', () => {
	beforeEach(() => {
		localStorage.clear();
		toasts.set([]);
		snoozeUntil.set(0);
		navEventPulses.set({});
		setRoute('/');
		startNotificationRouter();
	});

	afterEach(() => {
		stopNotificationRouter();
	});

	it('toasts and pulses green on a trade open', () => {
		emit({
			type: 'trade',
			data: { opened: [{ id: 't1', asset: 'BTC', direction: 'long', strategy: 'Momentum', source: 'scanner' }], closed: [] },
		});

		const list = get(toasts);
		expect(list).toHaveLength(1);
		expect(list[0].type).toBe('success');
		expect(list[0].message).toContain('LONG BTC');
		expect(list[0].href).toBe('/trading');
		expect(get(navEventPulses)['/trading']).toMatchObject({ count: 1, severity: 'success' });
	});

	it('pulses red on close and colors the toast by pnl', () => {
		emit({
			type: 'trade',
			data: { opened: [], closed: [{ id: 't1', asset: 'ETH', direction: 'short', strategy: 'MeanRev', pnl_pct: -0.0123 }] },
		});

		const list = get(toasts);
		expect(list).toHaveLength(1);
		expect(list[0].type).toBe('error');
		expect(list[0].message).toContain('-1.23%');
		expect(get(navEventPulses)['/trading']).toMatchObject({ count: 1, severity: 'danger' });
	});

	it('aggregates a mass close into a single toast', () => {
		const closed = Array.from({ length: 5 }, (_, i) => ({ id: `t${i}`, asset: 'BTC', direction: 'long', pnl_pct: 0.01 }));
		emit({ type: 'trade', data: { opened: [], closed } });

		const list = get(toasts);
		expect(list).toHaveLength(1);
		expect(list[0].message).toBe('Closed 5 positions');
		expect(get(navEventPulses)['/trading']?.count).toBe(5);
	});

	it('skips the open toast for paper-session fills (position card covers them) but still pulses', () => {
		emit({
			type: 'trade',
			data: { opened: [{ id: 'p1', asset: 'BTC', direction: 'long', execution_type: 'paper', source: 'scanner' }], closed: [] },
		});

		expect(get(toasts)).toHaveLength(0);
		expect(get(navEventPulses)['/trading']).toMatchObject({ count: 1, severity: 'success' });
	});

	it('routes bot trades to the Bot Factory tab', () => {
		emit({ type: 'trade', data: { opened: [{ id: 'b1', asset: 'DOGE', direction: 'long', source: 'bot:42' }], closed: [] } });

		expect(get(navEventPulses)['/bot-factory']).toMatchObject({ count: 1, severity: 'success' });
		expect(get(navEventPulses)['/trading']).toBeUndefined();
	});

	it('ignores activity-log shaped trade events so a fill never notifies twice', () => {
		emit({ type: 'trade', data: { level: 'trade', message: 'Opened long BTC' } });

		expect(get(toasts)).toHaveLength(0);
		expect(get(navEventPulses)).toEqual({});
	});

	it('toasts an approval and pulses the Approvals tab', () => {
		emit({
			type: 'approval_created',
			data: { id: 7, approval_type: 'strategy_promotion_approval', reason: 'Promote S0123 to paper' },
		});

		const list = get(toasts);
		expect(list).toHaveLength(1);
		expect(list[0].type).toBe('warning');
		expect(list[0].message).toContain('Promote S0123 to paper');
		expect(list[0].href).toBe('/approval');
		expect(get(navEventPulses)['/approval']).toMatchObject({ count: 1, severity: 'warn' });
	});

	it('suppresses the pulse for the route being viewed but still toasts', () => {
		setRoute('/trading');
		emit({ type: 'trade', data: { opened: [{ id: 't9', asset: 'SOL', direction: 'long' }], closed: [] } });

		expect(get(toasts)).toHaveLength(1);
		expect(get(navEventPulses)['/trading']).toBeUndefined();
	});

	it('toasts kill-switch flips only from state-diff payloads', () => {
		emit({ type: 'kill_switch_activated', data: { kill_switch_active: true, ts: 'x' } });
		// Activity-log classified copy of the same fact carries no boolean.
		emit({ type: 'kill_switch_activated', data: { message: 'Kill switch engaged (log row)' } });

		const list = get(toasts);
		expect(list).toHaveLength(1);
		expect(list[0].type).toBe('error');
		expect(get(navEventPulses)['/risk']).toMatchObject({ severity: 'danger' });
	});

	it('accumulates pulse counts and clears them when the tab is visited', () => {
		emit({ type: 'trade', data: { opened: [{ id: 'a', asset: 'BTC', direction: 'long' }], closed: [] } });
		emit({ type: 'trade', data: { opened: [], closed: [{ id: 'a', asset: 'BTC', direction: 'long', pnl_pct: 0.02 }] } });

		expect(get(navEventPulses)['/trading']).toMatchObject({ count: 2, severity: 'danger' });

		markNavIndicatorSeen('/trading');
		expect(get(navEventPulses)['/trading']).toBeUndefined();
	});
});
