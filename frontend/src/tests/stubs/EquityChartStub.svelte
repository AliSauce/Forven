<script lang="ts">
	// Test stub for EquityChart: lightweight-charts needs a real canvas 2D context,
	// which jsdom does not provide. Mirrors the real prop surface and exposes the
	// interesting inputs as data-attributes for assertions.
	export let data: unknown[] = [];
	export let benchmarkData: unknown = null;
	export let oosStartTimestamp: string | null = null;
	export let showDrawdown = false;
	export let height = 0;
</script>

<div
	data-testid="equity-chart-stub"
	data-points={Array.isArray(data) ? data.length : 0}
	data-benchmark={benchmarkData ? 'yes' : 'no'}
	data-oos={oosStartTimestamp ?? ''}
	data-drawdown={showDrawdown ? 'yes' : 'no'}
	data-height={height}
></div>
