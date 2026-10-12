<script lang="ts">
	import { getContext, onDestroy, onMount } from 'svelte';
	import type { IChartApi, ISeriesApi, UTCTimestamp } from 'lightweight-charts';
	import type { FinancePoint } from '$lib/apis/finance';
	import type i18nType from '$lib/i18n';

	const i18n: typeof i18nType = getContext('i18n');

	export let equity: FinancePoint[] = [];
	export let benchmark: FinancePoint[] = [];
	export let className = 'w-full h-80';

	const STRATEGY_COLOR = '#3b82f6';
	const BENCHMARK_COLOR = '#9ca3af';

	let container: HTMLDivElement;
	let chart: IChartApi | null = null;
	let strategySeries: ISeriesApi<'Line'> | null = null;
	let benchmarkSeries: ISeriesApi<'Line'> | null = null;
	let resizeObserver: ResizeObserver | null = null;
	let themeObserver: MutationObserver | null = null;
	let destroyed = false;

	const isDark = () => document.documentElement.classList.contains('dark');

	const themeOptions = () => {
		const dark = isDark();
		return {
			layout: {
				background: { color: 'transparent' },
				textColor: dark ? '#9ca3af' : '#4b5563'
			},
			grid: {
				vertLines: { color: dark ? 'rgba(255,255,255,0.04)' : 'rgba(0,0,0,0.04)' },
				horzLines: { color: dark ? 'rgba(255,255,255,0.04)' : 'rgba(0,0,0,0.04)' }
			},
			rightPriceScale: { borderColor: dark ? '#1f2937' : '#e5e7eb' },
			timeScale: {
				borderColor: dark ? '#1f2937' : '#e5e7eb',
				timeVisible: true,
				secondsVisible: false
			}
		};
	};

	const toLine = (points: FinancePoint[]) => {
		const seen = new Set<number>();
		return [...(points ?? [])]
			.sort((a, b) => a.t - b.t)
			.filter((p) => {
				if (seen.has(p.t) || p.v === null || p.v === undefined) return false;
				seen.add(p.t);
				return true;
			})
			.map((p) => ({ time: p.t as UTCTimestamp, value: p.v }));
	};

	const setData = (eq: FinancePoint[], bm: FinancePoint[]) => {
		if (!strategySeries || !benchmarkSeries) return;
		strategySeries.setData(toLine(eq));
		benchmarkSeries.setData(toLine(bm));
		chart?.timeScale().fitContent();
	};

	$: if (chart) setData(equity, benchmark);

	onMount(async () => {
		const { createChart, LineSeries } = await import('lightweight-charts');
		if (destroyed || !container) return;

		chart = createChart(container, {
			autoSize: false,
			width: container.clientWidth,
			height: container.clientHeight,
			...themeOptions()
		});

		benchmarkSeries = chart.addSeries(LineSeries, {
			color: BENCHMARK_COLOR,
			lineWidth: 2,
			priceLineVisible: false,
			title: ''
		});
		strategySeries = chart.addSeries(LineSeries, {
			color: STRATEGY_COLOR,
			lineWidth: 2,
			priceLineVisible: false,
			title: ''
		});

		setData(equity, benchmark);

		resizeObserver = new ResizeObserver((entries) => {
			const rect = entries[0]?.contentRect;
			if (rect && chart) chart.resize(rect.width, rect.height);
		});
		resizeObserver.observe(container);

		themeObserver = new MutationObserver(() => chart?.applyOptions(themeOptions()));
		themeObserver.observe(document.documentElement, {
			attributes: true,
			attributeFilter: ['class']
		});
	});

	onDestroy(() => {
		destroyed = true;
		resizeObserver?.disconnect();
		themeObserver?.disconnect();
		chart?.remove();
		chart = null;
		strategySeries = null;
		benchmarkSeries = null;
	});
</script>

<div class="flex flex-col gap-1">
	<div class="flex items-center gap-4 text-xs text-gray-500">
		<span class="flex items-center gap-1.5">
			<span class="inline-block h-0.5 w-4 rounded" style="background: {STRATEGY_COLOR}"></span>
			{$i18n.t('Strategy')}
		</span>
		<span class="flex items-center gap-1.5">
			<span class="inline-block h-0.5 w-4 rounded" style="background: {BENCHMARK_COLOR}"></span>
			{$i18n.t('Buy & hold')}
		</span>
	</div>
	<div bind:this={container} class={className}></div>
</div>
