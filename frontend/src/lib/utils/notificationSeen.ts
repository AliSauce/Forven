/**
 * "New since last visit" tracking for the Diagnostics notifications inbox.
 *
 * The inbox captures the persisted high-water id once on mount (the baseline),
 * then persists the newest id it has shown. Everything above the baseline —
 * including items that stream in while the page is open — renders as NEW until
 * the next visit resets the baseline.
 */

const STORAGE_KEY = 'forven.notifications.lastSeenId';

export function loadLastSeenNotificationId(): number {
	if (typeof window === 'undefined') return 0;
	try {
		const raw = window.localStorage.getItem(STORAGE_KEY);
		const parsed = Number(raw);
		return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
	} catch {
		return 0;
	}
}

export function persistLastSeenNotificationId(id: number): void {
	if (typeof window === 'undefined') return;
	if (!Number.isFinite(id) || id <= 0) return;
	try {
		// Never move the high-water mark backwards (e.g. a filtered refetch
		// returning older rows than a previous one).
		if (id <= loadLastSeenNotificationId()) return;
		window.localStorage.setItem(STORAGE_KEY, String(id));
	} catch {
		// Ignore storage errors — worst case everything shows as NEW.
	}
}

export interface SplitBySeen<T> {
	fresh: T[];
	earlier: T[];
}

/** Partition (order-preserving) into items newer than the baseline vs the rest. */
export function splitBySeenBaseline<T extends { id: number }>(items: T[], baselineId: number): SplitBySeen<T> {
	const fresh: T[] = [];
	const earlier: T[] = [];
	for (const item of items) {
		(item.id > baselineId ? fresh : earlier).push(item);
	}
	return { fresh, earlier };
}
