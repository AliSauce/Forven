import { beforeEach, describe, expect, it } from 'vitest';
import {
	loadLastSeenNotificationId,
	persistLastSeenNotificationId,
	splitBySeenBaseline,
} from '../lib/utils/notificationSeen';

describe('notificationSeen', () => {
	beforeEach(() => {
		localStorage.clear();
	});

	it('round-trips the last-seen id and never moves it backwards', () => {
		expect(loadLastSeenNotificationId()).toBe(0);

		persistLastSeenNotificationId(30117);
		expect(loadLastSeenNotificationId()).toBe(30117);

		persistLastSeenNotificationId(30050);
		expect(loadLastSeenNotificationId()).toBe(30117);

		persistLastSeenNotificationId(30200);
		expect(loadLastSeenNotificationId()).toBe(30200);
	});

	it('ignores invalid ids and garbage storage values', () => {
		persistLastSeenNotificationId(NaN);
		persistLastSeenNotificationId(-5);
		expect(loadLastSeenNotificationId()).toBe(0);

		localStorage.setItem('forven.notifications.lastSeenId', 'not-a-number');
		expect(loadLastSeenNotificationId()).toBe(0);
	});

	it('partitions items into new-since-baseline and earlier, preserving order', () => {
		const items = [{ id: 30133 }, { id: 30130 }, { id: 30117 }, { id: 30101 }];

		const { fresh, earlier } = splitBySeenBaseline(items, 30117);

		expect(fresh.map((i) => i.id)).toEqual([30133, 30130]);
		expect(earlier.map((i) => i.id)).toEqual([30117, 30101]);
	});

	it('treats a zero baseline as everything-new (callers decide first-visit handling)', () => {
		const { fresh, earlier } = splitBySeenBaseline([{ id: 1 }, { id: 2 }], 0);
		expect(fresh).toHaveLength(2);
		expect(earlier).toHaveLength(0);
	});
});
