import type { LifecycleStrategy, PipelineSettings, StrategyContainerHistoryItem } from '$lib/api/lifecycle';
import {
	asMetricRecord,
	readMetricFromSources,
	readNestedMetricFromSources,
	readPercentMetricFromSources,
} from '$lib/utils/strategy';

export type QuickScreenEvidenceStatus = 'passed' | 'failed' | 'warning' | 'skipped';

export interface QuickScreenEvidenceRow {
	key: string;
	label: string;
	status: QuickScreenEvidenceStatus;
	actual: string;
	required: string;
	detail: string;
}

type QuickScreenEvidenceSource = Record<string, unknown>;

function formatDecimal(value: number | null, digits = 2): string {
	if (value === null || !Number.isFinite(value)) return 'Unavailable';
	return value.toFixed(digits);
}

function formatCompactPercentValue(value: number, digits = 2): string {
	return value
		.toFixed(digits)
		.replace(/\.?0+$/, '');
}

function formatPercent(value: number | null, digits = 2): string {
	if (value === null || !Number.isFinite(value)) return 'Unavailable';
	return `${formatCompactPercentValue(value, digits)}%`;
}

function formatPercentThreshold(value: number | null): string {
	if (value === null || !Number.isFinite(value)) return 'Unavailable';
	return `${formatCompactPercentValue(value)}%`;
}

function buildMetricSources(
	strategy: LifecycleStrategy | null,
	backtests: StrategyContainerHistoryItem[]
): QuickScreenEvidenceSource[] {
	const sources: QuickScreenEvidenceSource[] = [];

	if (strategy) {
		sources.push(asMetricRecord(strategy.metrics));
		sources.push(asMetricRecord(strategy.metrics_json));
	}

	for (const item of backtests) {
		sources.push(asMetricRecord(item.metrics));
		sources.push(asMetricRecord(item.config));
	}

	return sources.filter((source) => Object.keys(source).length > 0);
}

function buildThresholdRow(args: {
	key: string;
	label: string;
	actual: string;
	required: string;
	detail: string;
	status: QuickScreenEvidenceStatus;
}): QuickScreenEvidenceRow {
	return {
		key: args.key,
		label: args.label,
		status: args.status,
		actual: args.actual,
		required: args.required,
		detail: args.detail,
	};
}

function compareThreshold(actual: number | null, required: number, passIfAbove: boolean): QuickScreenEvidenceStatus {
	if (actual === null) return 'warning';
	if (passIfAbove) return actual > required ? 'passed' : 'failed';
	return actual < required ? 'passed' : 'failed';
}

// Quick-screen is the ENTRY gate (quick_screen -> gauntlet) and judges the strategy's
// own backtest evidence only: in-sample Sharpe, return, and drawdown. The robustness
// validation suite (walk-forward / monte-carlo / param-jitter / cost-stress / regime-split)
// is PRODUCED INSIDE the gauntlet, so it can never be present at quick-screen — gating on
// it here false-blocks every candidate at 0/5. That requirement is correctly surfaced and
// enforced one stage later (gauntlet -> paper) via the backend promotion readiness
// (`validation_artifacts` step), so it must NOT appear in the quick-screen rows.
export function buildQuickScreenEvidenceRows(args: {
	strategy: LifecycleStrategy | null;
	backtests: StrategyContainerHistoryItem[];
	pipelineSettings: Partial<PipelineSettings> | null;
}): QuickScreenEvidenceRow[] {
	const { strategy, backtests, pipelineSettings } = args;
	const strategyRecord = strategy ? asMetricRecord(strategy) : {};
	const sources = buildMetricSources(strategy, backtests);

	const inSampleSharpe = readMetricFromSources(
		strategyRecord,
		sources,
		['in_sample_sharpe', 'is_sharpe', 'is_sharpe_ratio'],
		readNestedMetricFromSources(strategyRecord, sources, ['in_sample', 'is_metrics', 'inSample'], ['sharpe_ratio', 'sharpe'])
	);
	const totalReturn = readPercentMetricFromSources(strategyRecord, sources, ['total_return_pct', 'total_return', 'pnl_pct', 'return_pct']);
	const maxDrawdown = readPercentMetricFromSources(strategyRecord, sources, ['max_drawdown_pct', 'drawdown_pct', 'max_drawdown']);
	const sharpeThreshold = pipelineSettings?.min_sharpe_ratio ?? 0.5;
	const drawdownLimit = pipelineSettings?.max_drawdown_pct ?? 40;

	const sharpeStatus = compareThreshold(inSampleSharpe, sharpeThreshold, true);
	const returnStatus = totalReturn === null ? 'warning' : totalReturn > 0 ? 'passed' : 'failed';
	const drawdownStatus = compareThreshold(maxDrawdown, drawdownLimit, false);

	return [
		buildThresholdRow({
			key: 'is_sharpe_ratio',
			label: 'IS Sharpe Ratio',
			status: sharpeStatus,
			actual: formatDecimal(inSampleSharpe),
			required: `> ${formatDecimal(sharpeThreshold)}`,
			detail: inSampleSharpe === null
				? `Unavailable | Required > ${formatDecimal(sharpeThreshold)}`
				: `Actual ${formatDecimal(inSampleSharpe)} | Required > ${formatDecimal(sharpeThreshold)}`,
		}),
		buildThresholdRow({
			key: 'minimum_return',
			label: 'Minimum Return',
			status: returnStatus,
			actual: formatPercent(totalReturn),
			required: '> 0%',
			detail: totalReturn === null
				? 'Unavailable | Required > 0%'
				: `Actual ${formatPercent(totalReturn)} | Required > 0%`,
		}),
		buildThresholdRow({
			key: 'max_drawdown',
			label: 'Max Drawdown',
			status: drawdownStatus,
			actual: formatPercent(maxDrawdown),
			required: `< ${formatPercentThreshold(drawdownLimit)}`,
			detail: maxDrawdown === null
				? `Unavailable | Required < ${formatPercentThreshold(drawdownLimit)}`
				: `Actual ${formatPercent(maxDrawdown)} | Required < ${formatPercentThreshold(drawdownLimit)}`,
		}),
	];
}
