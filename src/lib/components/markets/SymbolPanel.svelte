<script lang="ts">
	import { getContext } from 'svelte';
	import { toast } from 'svelte-sonner';

	import {
		getFinanceBars,
		getFinanceQuote,
		type FinanceBar,
		type FinanceQuote,
		type FinanceTimeframe
	} from '$lib/apis/finance';
	import type i18nType from '$lib/i18n';

	import CandlestickChart from './CandlestickChart.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';

	const i18n: typeof i18nType = getContext('i18n');

	export let symbol: string;
	export let name = '';
	export let connectionId: string | null = null;

	const TIMEFRAMES: { value: FinanceTimeframe; label: string; days: number }[] = [
		{ value: '1Min', label: '1m', days: 2 },
		{ value: '5Min', label: '5m', days: 7 },
		{ value: '15Min', label: '15m', days: 14 },
		{ value: '1Hour', label: '1H', days: 60 },
		{ value: '1Day', label: '1D', days: 365 },
		{ value: '1Week', label: '1W', days: 365 * 5 }
	];

	let timeframe: FinanceTimeframe = '1Day';
	let quote: FinanceQuote | null = null;
	let bars: FinanceBar[] = [];
	let loadingBars = false;
	let requestId = 0;

	const fmt = (n: number | null | undefined, digits = 2) =>
		n === null || n === undefined
			? '—'
			: Number(n).toLocaleString(undefined, {
					minimumFractionDigits: digits,
					maximumFractionDigits: digits
				});

	const load = async (sym: string, tf: FinanceTimeframe, connId: string | null) => {
		const id = ++requestId;
		loadingBars = true;
		const days = TIMEFRAMES.find((t) => t.value === tf)?.days ?? 365;
		const start = new Date(Date.now() - days * 24 * 60 * 60 * 1000).toISOString();

		const [q, b] = await Promise.all([
			getFinanceQuote(localStorage.token, sym, connId).catch((err) => {
				toast.error(`${err}`);
				return null;
			}),
			getFinanceBars(localStorage.token, sym, tf, {
				start,
				limit: 1000,
				connectionId: connId
			}).catch((err) => {
				toast.error(`${err}`);
				return null;
			})
		]);
		if (id !== requestId) return;
		quote = q;
		bars = b?.bars ?? [];
		loadingBars = false;
	};

	$: if (symbol) load(symbol, timeframe, connectionId);
</script>

<div class="flex flex-col gap-2 rounded-2xl border border-gray-100 dark:border-gray-850 p-3">
	<div class="flex flex-wrap items-end justify-between gap-2">
		<div class="min-w-0">
			<div class="flex items-baseline gap-2">
				<span class="text-lg font-semibold">{symbol}</span>
				{#if name}
					<span class="text-sm text-gray-500 truncate">{name}</span>
				{/if}
			</div>
			<div class="flex flex-wrap gap-x-4 gap-y-0.5 text-xs text-gray-500">
				<span>
					{$i18n.t('Last')}:
					<span class="text-gray-900 dark:text-gray-100 text-sm font-medium"
						>{fmt(quote?.last)}</span
					>
				</span>
				<span>{$i18n.t('Bid')}: {fmt(quote?.bid)} × {fmt(quote?.bid_size, 0)}</span>
				<span>{$i18n.t('Ask')}: {fmt(quote?.ask)} × {fmt(quote?.ask_size, 0)}</span>
				{#if quote?.timestamp}
					<span>{new Date(quote.timestamp).toLocaleString()}</span>
				{/if}
			</div>
		</div>

		<div class="flex items-center gap-0.5 rounded-xl bg-gray-50 dark:bg-gray-850 p-0.5">
			{#each TIMEFRAMES as tf (tf.value)}
				<button
					class="px-2 py-0.5 text-xs rounded-lg transition {timeframe === tf.value
						? 'bg-white dark:bg-gray-800 shadow-sm'
						: 'text-gray-500'}"
					type="button"
					on:click={() => (timeframe = tf.value)}
				>
					{tf.label}
				</button>
			{/each}
		</div>
	</div>

	<div class="relative">
		<CandlestickChart {bars} className="w-full h-80" />
		{#if loadingBars}
			<div class="absolute inset-0 flex items-center justify-center pointer-events-none">
				<Spinner className="size-5" />
			</div>
		{:else if bars.length === 0}
			<div
				class="absolute inset-0 flex items-center justify-center pointer-events-none text-sm text-gray-500"
			>
				{$i18n.t('No data')}
			</div>
		{/if}
	</div>
</div>
