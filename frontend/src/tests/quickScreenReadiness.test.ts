import { describe, expect, it } from 'vitest';

import type { LifecycleStrategy, PipelineSettings, StrategyContainerHistoryItem } from '../lib/api/lifecycle';
import { buildQuickScreenEvidenceRows } from '../lib/utils/quickScreenReadiness';

function buildBacktest(metrics: Record<string, unknown>): StrategyContainerHistoryItem {
	return {
		result_id: 'B1001',
		strategy_id: 'S0001',
		result_type: 'backtest',
		symbol: 'BTC/USDT',
		timeframe: '1h',
		start_date: '2025-01-01T00:00:00Z',
		end_date: '2025-12-31T00:00:00Z',
		metrics,
		config: {},
		created_at: '2026-04-01T00:00:00Z',
		deleted_at: null,
	};
}

const pipelineSettings: PipelineSettings = {
	version: 1,
	autopilot_enabled: false,
	autopilot_worker_concurrency: 1,
	autopilot_generation_batch_size: 1,
	autopilot_scan_symbol: 'BTC/USDT',
	autopilot_scan_timeframe: '1h',
	promotion_mode: 'quick_screen',
	min_backtest_trades: 20,
	min_sharpe_ratio: 0.5,
	max_drawdown_pct: 40,
	min_profit_factor: 1.2,
	min_paper_days: 0,
	max_paper_divergence_pct: 0,
	min_paper_trades: 0,
	min_paper_sharpe: 0,
	failed_retention_hours: 24,
	ranking_top_n: 10,
	ranking_metric: 'sharpe_ratio',
	created_at: '2026-04-01T00:00:00Z',
	created_by: 'brain',
};

function buildStrategy(metrics: LifecycleStrategy['metrics'] = null): LifecycleStrategy {
	return {
		id: 'S0001',
		display_id: 'S0001',
		hypothesis_id: null,
		hypothesis_display_id: null,
		name: 'BTC Trend Candidate',
		type: 'rsi_momentum',
		state: 'quick_screen',
		source: 'manual',
		source_ref: null,
		owner: 'brain',
		symbol: 'BTC/USDT',
		timeframe: '1h',
		definition_json: null,
		dataset_hash: null,
		policy_version: 1,
		build_version: null,
		metrics_json: null,
		metrics,
		paper_session_id: null,
		paper_started_at: null,
		last_policy_result_json: null,
		blocked_reason: null,
		model: null,
		model_id: null,
		created_at: '2026-04-01T00:00:00Z',
		updated_at: '2026-04-01T00:00:00Z',
		state_changed_at: null,
		failed_at: null,
		retention_expires_at: null,
	};
}

describe('buildQuickScreenEvidenceRows', () => {
	it('builds the three quick-screen entry-gate rows from backtest metrics', () => {
		const rows = buildQuickScreenEvidenceRows({
			strategy: buildStrategy(),
			backtests: [
				buildBacktest({
					in_sample_sharpe: 0.82,
					total_return_pct: 12.4,
					max_drawdown_pct: 18.7,
				}),
			],
			pipelineSettings,
		});

		expect(rows).toHaveLength(3);
		expect(rows).toEqual([
			{
				key: 'is_sharpe_ratio',
				label: 'IS Sharpe Ratio',
				status: 'passed',
				actual: '0.82',
				required: '> 0.50',
				detail: 'Actual 0.82 | Required > 0.50',
			},
			{
				key: 'minimum_return',
				label: 'Minimum Return',
				status: 'passed',
				actual: '12.4%',
				required: '> 0%',
				detail: 'Actual 12.4% | Required > 0%',
			},
			{
				key: 'max_drawdown',
				label: 'Max Drawdown',
				status: 'passed',
				actual: '18.7%',
				required: '< 40%',
				detail: 'Actual 18.7% | Required < 40%',
			},
		]);
	});

	it('does NOT gate on the gauntlet validation suite at quick-screen', () => {
		// Regression: a 'Validation Coverage' row requiring 5 robustness artifacts used to be
		// emitted here, false-blocking every candidate at 0/5 since that suite only runs INSIDE
		// the gauntlet. The requirement belongs to gauntlet -> paper readiness, not quick-screen.
		const rows = buildQuickScreenEvidenceRows({
			strategy: buildStrategy(),
			backtests: [buildBacktest({ in_sample_sharpe: 0.82, total_return_pct: 12.4, max_drawdown_pct: 18.7 })],
			pipelineSettings,
		});
		expect(rows.map((r) => r.key)).toEqual(['is_sharpe_ratio', 'minimum_return', 'max_drawdown']);
		expect(rows.some((r) => r.key === 'validation_coverage')).toBe(false);
	});

	it('marks missing evidence as warning rows', () => {
		const rows = buildQuickScreenEvidenceRows({
			strategy: null,
			backtests: [],
			pipelineSettings,
		});

		expect(rows).toEqual([
			{
				key: 'is_sharpe_ratio',
				label: 'IS Sharpe Ratio',
				status: 'warning',
				actual: 'Unavailable',
				required: '> 0.50',
				detail: 'Unavailable | Required > 0.50',
			},
			{
				key: 'minimum_return',
				label: 'Minimum Return',
				status: 'warning',
				actual: 'Unavailable',
				required: '> 0%',
				detail: 'Unavailable | Required > 0%',
			},
			{
				key: 'max_drawdown',
				label: 'Max Drawdown',
				status: 'warning',
				actual: 'Unavailable',
				required: '< 40%',
				detail: 'Unavailable | Required < 40%',
			},
		]);
	});
});
