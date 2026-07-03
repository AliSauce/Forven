import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { mount, unmount } from 'svelte';
import NavBadge from '../lib/components/NavBadge.svelte';
import type { NavMetric, NavPulse } from '../lib/stores/navMetrics';

type MountedComponent = ReturnType<typeof mount>;

function metricOf(overrides: Partial<NavMetric>): NavMetric {
	return {
		kind: 'count',
		severity: 'info',
		label: '',
		summary: '',
		count: 0,
		seenKey: '',
		seen: false,
		...overrides,
	};
}

describe('NavBadge', () => {
	let target: HTMLDivElement;
	let app: MountedComponent | null = null;

	beforeEach(() => {
		target = document.createElement('div');
		document.body.appendChild(target);
	});

	afterEach(() => {
		if (app) {
			unmount(app);
			app = null;
		}
		target.remove();
	});

	it('shows an unseen count badge', () => {
		app = mount(NavBadge, {
			target,
			props: { metric: metricOf({ kind: 'count', count: 9, severity: 'info', summary: '9 live trades open' }) },
		});

		expect(target.textContent).toContain('9');
	});

	it('hides a count badge entirely once seen — visiting the tab acknowledges it', () => {
		app = mount(NavBadge, {
			target,
			props: {
				metric: metricOf({ kind: 'count', count: 25, severity: 'danger', seen: true, summary: '25 critical issues waiting' }),
			},
		});

		expect(target.textContent?.trim()).toBe('');
	});

	it('keeps a danger status pill visible even after being seen (standing hazard)', () => {
		app = mount(NavBadge, {
			target,
			props: {
				metric: metricOf({ kind: 'status', label: 'HALT', severity: 'danger', seen: true, summary: 'Kill switch active' }),
			},
		});

		expect(target.textContent).toContain('HALT');
	});

	it('renders the pulse over the metric and hides everything while the route is active', () => {
		const pulse: NavPulse = { count: 2, severity: 'success', summary: 'Trade opened' };
		const metric = metricOf({ kind: 'count', count: 9 });

		app = mount(NavBadge, { target, props: { metric, pulse } });
		expect(target.textContent).toContain('2');
		unmount(app);

		app = mount(NavBadge, { target, props: { metric, pulse, active: true } });
		expect(target.textContent?.trim()).toBe('');
	});
});
