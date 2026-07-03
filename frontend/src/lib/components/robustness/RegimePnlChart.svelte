<script lang="ts">
	import type { RegimeSplitEntry } from '$lib/api/backtesting';

	export let regimes: RegimeSplitEntry[] = [];
	export let width = 600;
	export let height = 220;

	let canvas: HTMLCanvasElement | null = null;

	// The regime verdict is computed in return space (position-size-invariant); dollar
	// PnL can be synthesized when the baseline trades lack real PnL. Plot returns when
	// the payload carries them; fall back to $ PnL only for legacy persisted results.
	$: usingReturns = regimes.some((r) => r.total_return_pct != null);

	function regimeValue(r: RegimeSplitEntry, useReturns: boolean): number {
		return useReturns ? Number(r.total_return_pct ?? 0) : Number(r.total_pnl || 0);
	}

	function draw() {
		if (!canvas || !regimes?.length) return;
		const ctx = canvas.getContext('2d');
		if (!ctx) return;
		const w = canvas.width, h = canvas.height;
		ctx.clearRect(0, 0, w, h);

		const useReturns = regimes.some((r) => r.total_return_pct != null);
		const pad = { top: 30, right: 20, bottom: 45, left: 55 };
		const chartW = w - pad.left - pad.right;
		const chartH = h - pad.top - pad.bottom;
		const barW = Math.min(60, chartW / regimes.length - 10);
		const maxValue = Math.max(...regimes.map((r) => Math.abs(regimeValue(r, useReturns))), useReturns ? 0.1 : 1);

		ctx.fillStyle = '#fff';
		ctx.font = 'bold 11px monospace';
		ctx.textAlign = 'center';
		ctx.fillText(useReturns ? 'Return & Win Rate by Regime' : 'PnL & Win Rate by Regime', w / 2, 16);

		const zeroY = pad.top + chartH / 2;
		ctx.strokeStyle = '#333';
		ctx.lineWidth = 1;
		ctx.beginPath(); ctx.moveTo(pad.left, zeroY); ctx.lineTo(w - pad.right, zeroY); ctx.stroke();

		const groupW = chartW / regimes.length;
		regimes.forEach((r, i: number) => {
			const cx = pad.left + i * groupW + groupW / 2;
			const value = regimeValue(r, useReturns);
			const winRate = Number(r.win_rate || 0);
			const scaleY = (val: number) => zeroY - (val / maxValue) * (chartH / 2);
			const by = scaleY(value);
			ctx.fillStyle = value >= 0 ? 'rgba(34,197,94,0.7)' : 'rgba(239,68,68,0.7)';
			ctx.fillRect(cx - barW / 2, Math.min(by, zeroY), barW, Math.abs(by - zeroY));
			ctx.beginPath();
			ctx.arc(cx, pad.top + chartH - (winRate / 100) * chartH, 4, 0, Math.PI * 2);
			ctx.fillStyle = '#fbbf24';
			ctx.fill();
			ctx.fillStyle = '#9ca3af';
			ctx.font = '9px monospace';
			ctx.textAlign = 'center';
			const shortName = String(r.name || '').replace('TREND_', '').replace('RANGE_', 'RNG_').replace('HIGH_', 'H_');
			ctx.fillText(shortName, cx, h - pad.bottom + 12);
			ctx.fillText(`${r.trade_count}t`, cx, h - pad.bottom + 24);
			ctx.fillStyle = value >= 0 ? '#4ade80' : '#f87171';
			ctx.fillText(useReturns ? `${value.toFixed(1)}%` : `$${value.toFixed(0)}`, cx, Math.min(by, zeroY) - 4);
		});

		ctx.font = '9px monospace';
		ctx.fillStyle = '#4ade80'; ctx.fillRect(w - 150, 8, 8, 8);
		ctx.fillStyle = '#9ca3af'; ctx.textAlign = 'left'; ctx.fillText(useReturns ? 'Total Ret%' : 'Total PnL', w - 138, 16);
		ctx.fillStyle = '#fbbf24';
		ctx.beginPath(); ctx.arc(w - 65, 12, 3, 0, Math.PI * 2); ctx.fill();
		ctx.fillStyle = '#9ca3af'; ctx.fillText('Win Rate', w - 58, 16);
	}

	// Redraw whenever the canvas mounts or the regimes change.
	$: if (canvas && regimes?.length) {
		void [regimes, width, height, usingReturns];
		draw();
	}
</script>

<canvas bind:this={canvas} {width} {height} class="cursor-crosshair"></canvas>
