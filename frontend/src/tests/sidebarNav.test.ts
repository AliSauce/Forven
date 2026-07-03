import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, unmount } from 'svelte';

const appStoreMocks = vi.hoisted(() => ({
	page: {
		subscribe(callback: (value: { url: URL }) => void) {
			callback({ url: new URL('http://localhost/data') });
			return () => {};
		},
	},
}));

vi.mock('$app/stores', () => ({
	page: appStoreMocks.page,
}));

vi.mock('$lib/stores/navMetrics', () => ({
	navRouteMetrics: {
		subscribe(callback: (value: Record<string, unknown>) => void) {
			callback({});
			return () => {};
		},
	},
	navEventPulses: {
		subscribe(callback: (value: Record<string, unknown>) => void) {
			callback({});
			return () => {};
		},
	},
	markNavIndicatorSeen: vi.fn(),
}));

vi.mock('$lib/stores/dataFetch', () => ({
	dataFetchState: {
		subscribe(callback: (value: { status: string; label: string }) => void) {
			callback({ status: 'idle', label: '' });
			return () => {};
		},
	},
}));

import Sidebar from '../lib/components/Sidebar.svelte';

type MountedComponent = ReturnType<typeof mount>;

describe('Sidebar navigation', () => {
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
		vi.clearAllMocks();
	});

	it('places Strategy Creator between Data and Crucibles in the primary navigation', () => {
		app = mount(Sidebar, {
			target,
			props: { connectionStatus: 'connected' },
		});

		const links = Array.from(target.querySelectorAll('nav[aria-label="Primary navigation"] a[aria-label]'))
			.map((node) => node.getAttribute('aria-label'));
		const dataIndex = links.indexOf('Data');
		const creatorIndex = links.indexOf('Strategy Creator');
		const hypothesisIndex = links.indexOf('Crucibles');

		expect(dataIndex).toBeGreaterThanOrEqual(0);
		expect(creatorIndex).toBe(dataIndex + 1);
		expect(hypothesisIndex).toBe(creatorIndex + 1);
	});
});
