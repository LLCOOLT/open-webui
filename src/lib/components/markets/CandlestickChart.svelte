<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import type { IChartApi, ISeriesApi, UTCTimestamp } from 'lightweight-charts';
	import type { FinanceBar } from '$lib/apis/finance';

	export let bars: FinanceBar[] = [];
	export let className = 'w-full h-80';

	let container: HTMLDivElement;
	let chart: IChartApi | null = null;
	let candleSeries: ISeriesApi<'Candlestick'> | null = null;
	let volumeSeries: ISeriesApi<'Histogram'> | null = null;
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

	const setData = (data: FinanceBar[]) => {
		if (!candleSeries || !volumeSeries) return;
		const sorted = [...(data ?? [])].sort((a, b) => a.t - b.t);
		candleSeries.setData(
			sorted.map((b) => ({ time: b.t as UTCTimestamp, open: b.o, high: b.h, low: b.l, close: b.c }))
		);
		volumeSeries.setData(
			sorted.map((b) => ({
				time: b.t as UTCTimestamp,
				value: b.v ?? 0,
				color: b.c >= b.o ? 'rgba(34,197,94,0.35)' : 'rgba(239,68,68,0.35)'
			}))
		);
		chart?.timeScale().fitContent();
	};

	$: if (chart) setData(bars);

	onMount(async () => {
		const { createChart, CandlestickSeries, HistogramSeries } = await import('lightweight-charts');
		if (destroyed || !container) return;

		chart = createChart(container, {
			autoSize: false,
			width: container.clientWidth,
			height: container.clientHeight,
			...themeOptions()
		});

		candleSeries = chart.addSeries(CandlestickSeries, {
			upColor: '#22c55e',
			downColor: '#ef4444',
			borderVisible: false,
			wickUpColor: '#22c55e',
			wickDownColor: '#ef4444'
		});

		volumeSeries = chart.addSeries(HistogramSeries, {
			priceFormat: { type: 'volume' },
			priceScaleId: 'volume'
		});
		chart.priceScale('volume').applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
		candleSeries.priceScale().applyOptions({ scaleMargins: { top: 0.05, bottom: 0.25 } });

		setData(bars);

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
		candleSeries = null;
		volumeSeries = null;
	});
</script>

<div bind:this={container} class={className}></div>
