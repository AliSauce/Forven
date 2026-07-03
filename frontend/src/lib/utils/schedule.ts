// Helpers for displaying and entering scheduler timing in plain minutes.
//
// The backend is unchanged: interval scheduler jobs are still stored as an
// integer-millisecond string, and routine schedules are still 5-field cron
// expressions. These helpers convert only at the UI boundary so the operator
// can think in minutes instead of milliseconds (interval jobs) or cron-step
// syntax (routines).

/** Convert a millisecond-interval string to whole minutes for display. Returns '' when not a positive interval. */
export function msToMinutes(ms: string | number | null | undefined): string {
	const raw = Number(ms);
	if (!Number.isFinite(raw) || raw <= 0) return '';
	return String(Math.max(1, Math.round(raw / 60000)));
}

/** Convert a minutes value entered in the UI to a millisecond-interval string for the backend. Returns '' when invalid. */
export function minutesToMs(minutes: string | number | null | undefined): string {
	const m = Number(minutes);
	if (!Number.isFinite(m) || m <= 0) return '';
	return String(Math.round(m) * 60000);
}

/** Human-readable interval label from a millisecond value, e.g. "every 30m" / "every 2h" / "every 1d". */
export function formatIntervalMs(ms: string | number | null | undefined): string {
	const raw = Number(ms);
	if (!Number.isFinite(raw) || raw <= 0) return String(ms ?? '').trim() || '--';
	const minutes = Math.max(1, Math.round(raw / 60000));
	if (minutes >= 60 * 24) return `every ${Math.round(minutes / (60 * 24))}d`;
	if (minutes >= 60) return `every ${Math.round(minutes / 60)}h`;
	return `every ${minutes}m`;
}

/**
 * Build a cron expression that fires every `minutes` minutes. Handles the
 * cleanly-expressible cases (sub-hour steps, whole hours, whole days). The
 * routines page renders a live "next fire times" preview, so any approximation
 * of an awkward value stays visible to the operator. Returns '' when invalid.
 */
export function minutesToCron(minutes: string | number | null | undefined): string {
	const n = Math.round(Number(minutes));
	if (!Number.isFinite(n) || n < 1) return '';
	if (n < 60) return `*/${n} * * * *`;
	if (n === 60) return '0 * * * *';
	if (n % 1440 === 0) {
		const days = n / 1440;
		return days === 1 ? '0 0 * * *' : `0 0 */${days} * *`;
	}
	if (n % 60 === 0) {
		const hours = n / 60;
		if (hours < 24) return `0 */${hours} * * *`;
		return '0 0 * * *';
	}
	// Not cleanly expressible in cron — approximate to the nearest whole hour.
	const hours = Math.max(1, Math.round(n / 60));
	return hours < 24 ? `0 */${hours} * * *` : '0 0 * * *';
}

// ---------------------------------------------------------------------------
// Friendly schedules — the routines page speaks "every N minutes / daily at
// 9:00 AM" in LOCAL time; the backend still stores a UTC 5-field cron. These
// helpers convert both ways. Crons that don't fit the friendly shapes (e.g.
// Brain-proposed expressions) fall back to the advanced raw-cron editor.
// ---------------------------------------------------------------------------

export type Frequency = 'minutes' | 'hours' | 'daily' | 'weekly' | 'monthly';

export interface FriendlySchedule {
	freq: Frequency;
	/** Interval for 'minutes' / 'hours'. */
	every: number;
	/** Local wall-clock 'HH:MM' for 'daily' / 'weekly' / 'monthly'. */
	time: string;
	/** Local day of week (0=Sunday) for 'weekly'. */
	weekday: number;
	/** Local day of month (1-31) for 'monthly'. */
	dom: number;
}

export const WEEKDAY_NAMES = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
const CRON_DOW_NAMES: Record<string, number> = { SUN: 0, MON: 1, TUE: 2, WED: 3, THU: 4, FRI: 5, SAT: 6 };

export function defaultFriendlySchedule(): FriendlySchedule {
	return { freq: 'daily', every: 30, time: '09:00', weekday: 1, dom: 1 };
}

function parseTime(time: string): { hour: number; minute: number } | null {
	const m = /^(\d{1,2}):(\d{2})$/.exec((time || '').trim());
	if (!m) return null;
	const hour = Number(m[1]);
	const minute = Number(m[2]);
	if (hour < 0 || hour > 23 || minute < 0 || minute > 59) return null;
	return { hour, minute };
}

/** A local wall-clock time today, as a Date (used to read the UTC equivalent). */
function localToday(hour: number, minute: number): Date {
	const d = new Date();
	d.setHours(hour, minute, 0, 0);
	return d;
}

/** Convert a friendly local-time schedule to a UTC 5-field cron. '' when invalid. */
export function friendlyToCron(s: FriendlySchedule): string {
	if (s.freq === 'minutes') return minutesToCron(s.every);
	if (s.freq === 'hours') {
		const h = Math.round(Number(s.every));
		if (!Number.isFinite(h) || h < 1 || h > 23) return '';
		return h === 1 ? '0 * * * *' : `0 */${h} * * *`;
	}
	const t = parseTime(s.time);
	if (!t) return '';
	if (s.freq === 'daily') {
		const d = localToday(t.hour, t.minute);
		return `${d.getUTCMinutes()} ${d.getUTCHours()} * * *`;
	}
	if (s.freq === 'weekly') {
		// Next local occurrence of (weekday, time); its UTC day-of-week absorbs
		// any midnight crossing introduced by the timezone offset.
		const d = localToday(t.hour, t.minute);
		d.setDate(d.getDate() + ((((s.weekday - d.getDay()) % 7) + 7) % 7));
		return `${d.getUTCMinutes()} ${d.getUTCHours()} * * ${d.getUTCDay()}`;
	}
	if (s.freq === 'monthly') {
		const dom = Math.round(Number(s.dom));
		if (!Number.isFinite(dom) || dom < 1 || dom > 31) return '';
		// Detect whether the local->UTC conversion crosses midnight using a
		// mid-month reference day, then shift the requested day accordingly
		// (wrapping within 1..31; the live fire-time preview shows the truth).
		const ref = new Date();
		ref.setDate(15);
		ref.setHours(t.hour, t.minute, 0, 0);
		const shift = ref.getUTCDate() - 15;
		const utcDom = ((dom - 1 + shift + 31) % 31) + 1;
		return `${ref.getUTCMinutes()} ${ref.getUTCHours()} ${utcDom} * *`;
	}
	return '';
}

/**
 * Inverse of friendlyToCron for the shapes it produces (plus plain
 * "m h * * dow/dom" crons from other authors). UTC fields are converted back
 * to local wall-clock. Returns null when the cron doesn't fit — the caller
 * falls back to the advanced raw-cron editor.
 */
export function cronToFriendly(expr: string | null | undefined): FriendlySchedule | null {
	const parts = (expr || '').trim().split(/\s+/);
	if (parts.length !== 5) return null;
	const [min, hour, dom, mon, dow] = parts;
	if (mon !== '*') return null;
	const base = defaultFriendlySchedule();

	const asMinutes = cronToMinutes(expr);
	if (asMinutes !== null && asMinutes < 60) return { ...base, freq: 'minutes', every: asMinutes };
	if (asMinutes !== null && asMinutes % 60 === 0 && asMinutes < 1440)
		return { ...base, freq: 'hours', every: asMinutes / 60 };

	const m = /^\d+$/.test(min) ? Number(min) : null;
	const h = /^\d+$/.test(hour) ? Number(hour) : null;
	if (m === null || h === null || m > 59 || h > 23) return null;

	// Reconstruct local wall-clock from the stored UTC time.
	const utcRef = new Date();
	utcRef.setUTCHours(h, m, 0, 0);
	const localTime = `${String(utcRef.getHours()).padStart(2, '0')}:${String(utcRef.getMinutes()).padStart(2, '0')}`;

	if (dom === '*' && dow === '*') return { ...base, freq: 'daily', time: localTime };

	if (dom === '*' && dow !== '*') {
		let utcDow = CRON_DOW_NAMES[dow.toUpperCase()] ?? (/^\d+$/.test(dow) ? Number(dow) % 7 : null);
		if (utcDow === null) return null;
		// Roll a reference date to that UTC weekday and read its local weekday.
		const d = new Date();
		d.setUTCHours(h, m, 0, 0);
		d.setUTCDate(d.getUTCDate() + ((((utcDow - d.getUTCDay()) % 7) + 7) % 7));
		return { ...base, freq: 'weekly', time: localTime, weekday: d.getDay() };
	}

	if (dom !== '*' && dow === '*' && /^\d+$/.test(dom)) {
		const utcDom = Number(dom);
		if (utcDom < 1 || utcDom > 31) return null;
		const ref = new Date();
		ref.setUTCDate(15);
		ref.setUTCHours(h, m, 0, 0);
		const shift = ref.getDate() - 15;
		const localDom = ((utcDom - 1 + shift + 31) % 31) + 1;
		return { ...base, freq: 'monthly', time: localTime, dom: localDom };
	}
	return null;
}

/** 12-hour local time label for a 'HH:MM' string, e.g. '9:00 AM'. */
function timeLabel(time: string): string {
	const t = parseTime(time);
	if (!t) return time;
	const d = new Date();
	d.setHours(t.hour, t.minute, 0, 0);
	return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}

/** Plain-English LOCAL-time description of a friendly schedule. */
export function describeFriendly(s: FriendlySchedule): string {
	if (s.freq === 'minutes') return s.every === 1 ? 'Every minute' : `Every ${s.every} minutes`;
	if (s.freq === 'hours') return s.every === 1 ? 'Every hour' : `Every ${s.every} hours`;
	if (s.freq === 'daily') return `Every day at ${timeLabel(s.time)}`;
	if (s.freq === 'weekly') return `Every ${WEEKDAY_NAMES[s.weekday] ?? '?'} at ${timeLabel(s.time)}`;
	if (s.freq === 'monthly') return `Monthly on day ${s.dom} at ${timeLabel(s.time)}`;
	return '';
}

/**
 * Plain-English local-time description of a cron expression, for routine
 * cards. Falls back to '' when the cron doesn't fit a friendly shape (the
 * caller shows the raw expression instead).
 */
export function describeCronLocal(expr: string | null | undefined): string {
	const friendly = cronToFriendly(expr);
	return friendly ? describeFriendly(friendly) : '';
}

/**
 * Inverse of minutesToCron for the patterns it produces, so an existing routine
 * can be re-opened in "every N minutes" mode. Returns null when the expression
 * isn't a simple interval (e.g. "0 14 * * MON"), signalling the caller to fall
 * back to cron mode.
 */
export function cronToMinutes(expr: string | null | undefined): number | null {
	const parts = (expr || '').trim().split(/\s+/);
	if (parts.length !== 5) return null;
	const [min, hour, dom, mon, dow] = parts;
	if (mon !== '*' || dow !== '*') return null;

	const stepMin = /^\*\/(\d+)$/.exec(min);
	// */N * * * *  -> every N minutes (sub-hour)
	if (stepMin && hour === '*' && dom === '*') {
		const n = Number(stepMin[1]);
		return n >= 1 && n < 60 ? n : null;
	}
	// 0 * * * *  -> hourly
	if (min === '0' && hour === '*' && dom === '*') return 60;

	const stepHour = /^\*\/(\d+)$/.exec(hour);
	// 0 */H * * *  -> every H hours
	if (min === '0' && stepHour && dom === '*') {
		const h = Number(stepHour[1]);
		return h >= 1 && h < 24 ? h * 60 : null;
	}
	// 0 0 * * *  -> daily
	if (min === '0' && hour === '0' && dom === '*') return 1440;

	const stepDom = /^\*\/(\d+)$/.exec(dom);
	// 0 0 */D * *  -> every D days
	if (min === '0' && hour === '0' && stepDom) {
		const d = Number(stepDom[1]);
		return d >= 1 ? d * 1440 : null;
	}
	return null;
}
