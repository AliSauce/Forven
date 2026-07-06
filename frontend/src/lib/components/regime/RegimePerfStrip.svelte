<script lang="ts">
	import { getRobustnessResult } from '$lib/api/backtesting';
	import type { StrategyContainerHistoryItem } from '$lib/api/lifecycle';
	import { formatRegimeLabel, regimeBadgeClass } from '$lib/utils/labRegime';

	/** Container validation history — the strip finds the latest regime_split. */
	export let validationHistory: StrategyContainerHistoryItem[] = [];

	interface RegimeEntry {
		name: string;
		trade_count?: number;
		win_rate?: number;
		avg_return_pct?: number;
		total_return_pct?: number;
		best_return_pct?: number;
		worst_return_pct?: number;
	}

	let entries: RegimeEntry[] = [];
	let sourceId = '';
	let sourceDate = '';
	let expanded = false;
	let loading = false;
	let loadedFor = '';

	const REGIME_ORDER = ['TREND_UP', 'RANGE_BOUND', 'RANGE', 'TREND_DOWN', 'HIGH_VOL'];

	$: latest = (validationHistory ?? [])
		.filter((item) => item.result_type === 'regime_split' && !item.deleted_at)
		.sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))[0];

	$: if (latest && latest.result_id !== loadedFor) {
		void load(latest);
	} else if (!latest && entries.length) {
		entries = [];
		sourceId = '';
	}

	function coerceEntries(raw: unknown): RegimeEntry[] {
		if (!Array.isArray(raw)) return [];
		return raw
			.filter((item): item is Record<string, unknown> => !!item && typeof item === 'object')
			.map((item) => ({
				name: String(item.name ?? ''),
				trade_count: numberOrUndefined(item.trade_count),
				win_rate: numberOrUndefined(item.win_rate),
				avg_return_pct: numberOrUndefined(item.avg_return_pct),
				total_return_pct: numberOrUndefined(item.total_return_pct),
				best_return_pct: numberOrUndefined(item.best_return_pct),
				worst_return_pct: numberOrUndefined(item.worst_return_pct)
			}))
			.filter((entry) => entry.name)
			.sort(
				(a, b) =>
					(REGIME_ORDER.indexOf(a.name) + 100) - (REGIME_ORDER.indexOf(b.name) + 100) ||
					a.name.localeCompare(b.name)
			);
	}

	function numberOrUndefined(value: unknown): number | undefined {
		const numeric = Number(value);
		return Number.isFinite(numeric) ? numeric : undefined;
	}

	async function load(item: StrategyContainerHistoryItem) {
		loadedFor = item.result_id;
		sourceId = item.result_id;
		sourceDate = String(item.created_at ?? '').slice(0, 10);
		const inline = coerceEntries((item.metrics as Record<string, unknown> | undefined)?.regimes);
		if (inline.length) {
			entries = inline;
			return;
		}
		loading = true;
		try {
			const result = await getRobustnessResult(item.result_id);
			const payload = (result?.payload ?? result?.metrics ?? {}) as Record<string, unknown>;
			entries = coerceEntries(payload.regimes);
		} catch {
			entries = [];
		} finally {
			loading = false;
		}
	}

	function tone(value: number | undefined): string {
		if (value === undefined) return 'text-[#888]';
		return value > 0 ? 'text-emerald-400' : value < 0 ? 'text-red-400' : 'text-[#888]';
	}

	function fmt(value: number | undefined, digits = 2): string {
		return value === undefined ? '—' : value.toFixed(digits);
	}
</script>

{#if latest}
	<div class="border border-[#1d1d1d] bg-[#090909] p-3" data-testid="regime-perf-strip">
		<div class="mb-2 flex items-baseline justify-between gap-2">
			<h3 class="text-[10px] font-bold uppercase tracking-wider text-[#777]">
				Performance by regime
			</h3>
			<span class="text-[9px] text-[#555]">gauntlet regime_split · {sourceDate}</span>
		</div>
		{#if loading}
			<div class="h-14 animate-pulse bg-[#111]"></div>
		{:else if entries.length}
			<div class="grid gap-px border border-[#1d1d1d] bg-[#1d1d1d]" style="grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));">
				{#each entries as entry (entry.name)}
					<div class="bg-[#0d0d0d] px-2.5 py-2">
						<span class={`inline-block border px-1 py-0.5 text-[9px] uppercase tracking-wider ${regimeBadgeClass(entry.name)}`}>
							{formatRegimeLabel(entry.name)}
						</span>
						<div class={`mt-1 text-sm font-bold ${tone(entry.avg_return_pct)}`}>
							{entry.avg_return_pct === undefined ? '—' : `${entry.avg_return_pct > 0 ? '+' : ''}${fmt(entry.avg_return_pct)}%/t`}
						</div>
						<div class="text-[10px] text-[#666]">
							n={entry.trade_count ?? '—'} · win {fmt(entry.win_rate, 0)}%
						</div>
					</div>
				{/each}
			</div>
			<button
				class="mt-2 text-[10px] uppercase tracking-wider text-[#666] hover:text-white"
				on:click={() => (expanded = !expanded)}
			>
				{expanded ? '− details' : '+ details'}
			</button>
			{#if expanded}
				<div class="mt-2 overflow-x-auto">
					<table class="w-full text-[11px]">
						<thead>
							<tr class="border-b border-[#222] text-left text-[9px] uppercase tracking-wider text-[#555]">
								<th class="px-2 py-1">Regime</th>
								<th class="px-2 py-1 text-right">Trades</th>
								<th class="px-2 py-1 text-right">Win %</th>
								<th class="px-2 py-1 text-right">Avg %</th>
								<th class="px-2 py-1 text-right">Total %</th>
								<th class="px-2 py-1 text-right">Best %</th>
								<th class="px-2 py-1 text-right">Worst %</th>
							</tr>
						</thead>
						<tbody>
							{#each entries as entry (entry.name)}
								<tr class="border-b border-[#161616]">
									<td class="px-2 py-1">{formatRegimeLabel(entry.name)}</td>
									<td class="px-2 py-1 text-right text-[#888]">{entry.trade_count ?? '—'}</td>
									<td class="px-2 py-1 text-right text-[#888]">{fmt(entry.win_rate, 1)}</td>
									<td class={`px-2 py-1 text-right ${tone(entry.avg_return_pct)}`}>{fmt(entry.avg_return_pct, 3)}</td>
									<td class={`px-2 py-1 text-right ${tone(entry.total_return_pct)}`}>{fmt(entry.total_return_pct)}</td>
									<td class="px-2 py-1 text-right text-[#888]">{fmt(entry.best_return_pct)}</td>
									<td class="px-2 py-1 text-right text-[#888]">{fmt(entry.worst_return_pct)}</td>
								</tr>
							{/each}
						</tbody>
					</table>
					<p class="mt-1.5 text-[9px] text-[#555]">
						source: {sourceId} · per-trade net returns bucketed by the causal entry-bar regime
					</p>
				</div>
			{/if}
		{:else}
			<p class="text-[11px] text-[#555]">regime_split artifact has no per-regime buckets</p>
		{/if}
	</div>
{/if}
