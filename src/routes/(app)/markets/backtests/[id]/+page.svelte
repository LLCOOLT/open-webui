<script lang="ts">
	import { getContext, onDestroy } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import { toast } from 'svelte-sonner';

	import { WEBUI_NAME } from '$lib/stores';
	import {
		deleteFinanceBacktestById,
		getFinanceBacktestById,
		type FinanceBacktest,
		type FinanceBacktestMetrics
	} from '$lib/apis/finance';
	import type i18nType from '$lib/i18n';

	import Spinner from '$lib/components/common/Spinner.svelte';
	import ConfirmDialog from '$lib/components/common/ConfirmDialog.svelte';
	import EquityChart from '$lib/components/markets/EquityChart.svelte';
	import {
		fmtMetric,
		fmtMoney,
		fmtNs,
		fmtNumber,
		fmtPct,
		fmtSec,
		signClass,
		statusClass,
		type MetricKey
	} from '$lib/components/markets/backtestFormat';

	const i18n: typeof i18nType = getContext('i18n');

	const TRADE_LIMIT = 200;

	$: backtestId = $page.params.id ?? '';

	let backtest: FinanceBacktest | null = null;
	let loaded = false;
	let timer: ReturnType<typeof setTimeout> | null = null;
	let showDeleteConfirm = false;

	$: metricRows = [
		{ key: 'total_return', label: $i18n.t('Total return'), kind: 'pct' },
		{ key: 'cagr', label: $i18n.t('CAGR'), kind: 'pct' },
		{ key: 'sharpe', label: $i18n.t('Sharpe'), kind: 'num' },
		{ key: 'sortino', label: $i18n.t('Sortino'), kind: 'num' },
		{ key: 'max_drawdown', label: $i18n.t('Max drawdown'), kind: 'pct' },
		{ key: 'win_rate', label: $i18n.t('Win rate'), kind: 'pct' },
		{ key: 'profit_factor', label: $i18n.t('Profit factor'), kind: 'num' },
		{ key: 'trades', label: $i18n.t('Trades'), kind: 'int' },
		{ key: 'exposure', label: $i18n.t('Exposure'), kind: 'pct' },
		{ key: 'avg_trade_return', label: $i18n.t('Avg trade return'), kind: 'pct' },
		{ key: 'final_equity', label: $i18n.t('Final equity'), kind: 'money' }
	] as { key: MetricKey; label: string; kind: string }[];

	const metric = (m: FinanceBacktestMetrics | null | undefined, key: MetricKey) =>
		m ? (m[key] ?? null) : null;

	$: result = backtest?.result ?? null;
	$: perSymbol = Object.entries(result?.per_symbol ?? {});
	$: grid = result?.grid ?? [];
	$: bestParamsJson = result?.best_params ? JSON.stringify(result.best_params) : null;
	$: trades = result?.trades ?? [];
	$: pending = backtest?.status === 'queued' || backtest?.status === 'running';

	const stopPolling = () => {
		if (timer) clearTimeout(timer);
		timer = null;
	};

	const fetchBacktest = async (id: string) => {
		const res = await getFinanceBacktestById(localStorage.token, id).catch((err) => {
			toast.error(`${err}`);
			return null;
		});
		if (id !== backtestId) return;
		if (!res) {
			if (!backtest) goto('/markets/backtests');
			return;
		}
		backtest = res;
		loaded = true;
		stopPolling();
		if (res.status === 'queued' || res.status === 'running') {
			timer = setTimeout(() => fetchBacktest(id), 2000);
		}
	};

	let loadedId = '';
	$: if (backtestId && backtestId !== loadedId) {
		loadedId = backtestId;
		stopPolling();
		backtest = null;
		loaded = false;
		fetchBacktest(backtestId);
	}

	const deleteBacktest = async () => {
		if (!backtest) return;
		const res = await deleteFinanceBacktestById(localStorage.token, backtest.id).catch((err) => {
			toast.error(`${err}`);
			return null;
		});
		if (res) {
			toast.success($i18n.t('Backtest deleted'));
			goto('/markets/backtests');
		}
	};

	onDestroy(stopPolling);
</script>

<svelte:head>
	<title>{backtest?.name ?? $i18n.t('Backtest')} / {$WEBUI_NAME}</title>
</svelte:head>

<ConfirmDialog
	bind:show={showDeleteConfirm}
	title={$i18n.t('Delete backtest?')}
	message={$i18n.t('This will permanently delete {{name}}.', { name: backtest?.name ?? '' })}
	on:confirm={deleteBacktest}
/>

<div class="h-full overflow-y-auto px-2.5 pb-6">
	{#if !loaded || !backtest}
		<div class="flex justify-center py-10"><Spinner className="size-5" /></div>
	{:else}
		<div class="flex flex-col gap-4 max-w-6xl mx-auto pt-1">
			<div class="flex flex-wrap items-center gap-2">
				<a href="/markets/backtests" class="text-sm text-gray-500 hover:underline"
					>{$i18n.t('Backtests')}</a
				>
				<span class="text-gray-400">/</span>
				<span class="text-sm font-medium truncate">{backtest.name}</span>
				<span class="rounded-full px-2 py-0.5 text-xs {statusClass(backtest.status)}"
					>{backtest.status}</span
				>
				{#if pending}<Spinner className="size-3.5" />{/if}
				<div class="flex-1"></div>
				{#if backtest.strategy_id}
					<a
						href="/markets/strategies/{backtest.strategy_id}"
						class="px-3 h-8 flex items-center text-sm rounded-full bg-gray-50 hover:bg-gray-100 dark:bg-gray-850 dark:hover:bg-gray-800 transition"
						>{$i18n.t('Open strategy')}{backtest.strategy_version
							? ` (v${backtest.strategy_version})`
							: ''}</a
					>
				{/if}
				<button
					class="px-3 h-8 text-sm rounded-full text-red-600 bg-gray-50 hover:bg-gray-100 dark:bg-gray-850 dark:hover:bg-gray-800 transition"
					type="button"
					on:click={() => (showDeleteConfirm = true)}>{$i18n.t('Delete')}</button
				>
			</div>

			<div
				class="rounded-xl px-3 py-2 text-xs bg-yellow-500/10 text-yellow-800 dark:text-yellow-200"
			>
				{$i18n.t(
					'Backtests are hypothetical. Past performance does not predict future results; beware of overfitting.'
				)}
			</div>

			<div class="flex flex-wrap gap-x-5 gap-y-1 text-xs text-gray-500">
				<span
					>{$i18n.t('Symbols')}:
					<span class="text-gray-900 dark:text-gray-100"
						>{(backtest.config?.symbols ?? []).join(', ')}</span
					></span
				>
				<span
					>{$i18n.t('Timeframe')}:
					<span class="text-gray-900 dark:text-gray-100">{backtest.config?.timeframe}</span></span
				>
				<span
					>{$i18n.t('Range')}:
					<span class="text-gray-900 dark:text-gray-100"
						>{backtest.config?.start ?? '—'} → {backtest.config?.end ?? '—'}</span
					></span
				>
				<span
					>{$i18n.t('Initial cash')}:
					<span class="text-gray-900 dark:text-gray-100"
						>{fmtMoney(backtest.config?.initial_cash)}</span
					></span
				>
				<span
					>{$i18n.t('Commission')}:
					<span class="text-gray-900 dark:text-gray-100"
						>{backtest.config?.commission_bps ?? 0} bps</span
					></span
				>
				<span
					>{$i18n.t('Slippage')}:
					<span class="text-gray-900 dark:text-gray-100"
						>{backtest.config?.slippage_bps ?? 0} bps</span
					></span
				>
				<span
					>{$i18n.t('Params')}:
					<span class="text-gray-900 dark:text-gray-100 font-mono"
						>{JSON.stringify(backtest.config?.params ?? {})}</span
					></span
				>
				<span
					>{$i18n.t('Created')}:
					<span class="text-gray-900 dark:text-gray-100">{fmtNs(backtest.created_at)}</span></span
				>
			</div>

			{#if backtest.status === 'failed'}
				<div
					class="rounded-xl px-3 py-2 text-sm bg-red-500/10 text-red-700 dark:text-red-300 whitespace-pre-wrap font-mono"
				>
					{backtest.error ?? $i18n.t('Backtest failed')}
				</div>
			{:else if pending}
				<div class="py-10 text-center text-sm text-gray-500">
					{backtest.status === 'queued'
						? $i18n.t('Backtest queued…')
						: $i18n.t('Backtest running…')}
				</div>
			{/if}

			{#if backtest.metrics || backtest.benchmark}
				<div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-2">
					{#each metricRows as row (row.key)}
						{@const s = metric(backtest.metrics, row.key)}
						{@const b = metric(backtest.benchmark, row.key)}
						<div class="rounded-xl bg-gray-50 dark:bg-gray-850 px-3 py-2">
							<div class="text-xs text-gray-500">{row.label}</div>
							<div class="flex items-baseline justify-between gap-2">
								<span
									class="text-base font-medium {row.kind === 'pct' ? signClass(s) : ''}"
									title={$i18n.t('Strategy')}>{fmtMetric(row.kind, s)}</span
								>
								<span class="text-xs text-gray-500" title={$i18n.t('Buy & hold')}
									>{$i18n.t('B&H')} {fmtMetric(row.kind, b)}</span
								>
							</div>
						</div>
					{/each}
				</div>
			{/if}

			{#if result && (result.equity?.length ?? 0) > 0}
				<div class="rounded-2xl border border-gray-100 dark:border-gray-850 p-3">
					<div class="text-sm font-medium mb-2">{$i18n.t('Equity curve')}</div>
					<EquityChart equity={result.equity} benchmark={result.benchmark_equity ?? []} />
				</div>
			{/if}

			{#if perSymbol.length > 1}
				<div class="rounded-2xl border border-gray-100 dark:border-gray-850 p-3 overflow-x-auto">
					<div class="text-sm font-medium mb-2">{$i18n.t('Per symbol')}</div>
					<table class="w-full text-sm">
						<thead class="text-xs text-gray-500 text-left">
							<tr>
								<th class="py-1 pr-3 font-normal">{$i18n.t('Symbol')}</th>
								<th class="py-1 pr-3 font-normal text-right">{$i18n.t('Return')}</th>
								<th class="py-1 pr-3 font-normal text-right">{$i18n.t('Buy & hold')}</th>
								<th class="py-1 pr-3 font-normal text-right">{$i18n.t('Sharpe')}</th>
								<th class="py-1 pr-3 font-normal text-right">{$i18n.t('Max drawdown')}</th>
								<th class="py-1 pr-3 font-normal text-right">{$i18n.t('Trades')}</th>
							</tr>
						</thead>
						<tbody>
							{#each perSymbol as [sym, r] (sym)}
								<tr class="border-t border-gray-100 dark:border-gray-850">
									<td class="py-1.5 pr-3 font-medium">{sym}</td>
									<td class="py-1.5 pr-3 text-right {signClass(r?.metrics?.total_return)}"
										>{fmtPct(r?.metrics?.total_return)}</td
									>
									<td class="py-1.5 pr-3 text-right {signClass(r?.benchmark?.total_return)}"
										>{fmtPct(r?.benchmark?.total_return)}</td
									>
									<td class="py-1.5 pr-3 text-right">{fmtNumber(r?.metrics?.sharpe)}</td>
									<td class="py-1.5 pr-3 text-right">{fmtPct(r?.metrics?.max_drawdown)}</td>
									<td class="py-1.5 pr-3 text-right">{fmtNumber(r?.metrics?.trades, 0)}</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{/if}

			{#if grid.length > 0}
				<div class="rounded-2xl border border-gray-100 dark:border-gray-850 p-3 overflow-x-auto">
					<div class="text-sm font-medium mb-1">{$i18n.t('Parameter grid')}</div>
					<div class="text-xs text-gray-500 mb-2">
						{$i18n.t(
							'Picking the best in-sample combination is a classic source of overfitting; confirm on a different period.'
						)}
					</div>
					<table class="w-full text-sm">
						<thead class="text-xs text-gray-500 text-left">
							<tr>
								<th class="py-1 pr-3 font-normal">{$i18n.t('Params')}</th>
								<th class="py-1 pr-3 font-normal text-right">{$i18n.t('Return')}</th>
								<th class="py-1 pr-3 font-normal text-right">{$i18n.t('Sharpe')}</th>
								<th class="py-1 pr-3 font-normal text-right">{$i18n.t('Max drawdown')}</th>
								<th class="py-1 pr-3 font-normal text-right">{$i18n.t('Trades')}</th>
							</tr>
						</thead>
						<tbody>
							{#each grid as g, idx (idx)}
								{@const best =
									bestParamsJson !== null && JSON.stringify(g.params) === bestParamsJson}
								<tr
									class="border-t border-gray-100 dark:border-gray-850 {best
										? 'bg-green-500/10 font-medium'
										: ''}"
								>
									<td class="py-1.5 pr-3 font-mono text-xs"
										>{JSON.stringify(g.params)}{best ? ` ★ ${$i18n.t('best')}` : ''}</td
									>
									<td class="py-1.5 pr-3 text-right {signClass(g.metrics?.total_return)}"
										>{fmtPct(g.metrics?.total_return)}</td
									>
									<td class="py-1.5 pr-3 text-right">{fmtNumber(g.metrics?.sharpe)}</td>
									<td class="py-1.5 pr-3 text-right">{fmtPct(g.metrics?.max_drawdown)}</td>
									<td class="py-1.5 pr-3 text-right">{fmtNumber(g.metrics?.trades, 0)}</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{/if}

			{#if trades.length > 0}
				<div class="rounded-2xl border border-gray-100 dark:border-gray-850 p-3 overflow-x-auto">
					<div class="text-sm font-medium mb-2">
						{$i18n.t('Trades')} <span class="text-xs text-gray-500">({trades.length})</span>
					</div>
					<table class="w-full text-sm">
						<thead class="text-xs text-gray-500 text-left">
							<tr>
								<th class="py-1 pr-3 font-normal">{$i18n.t('Symbol')}</th>
								<th class="py-1 pr-3 font-normal">{$i18n.t('Entry')}</th>
								<th class="py-1 pr-3 font-normal text-right">{$i18n.t('Entry price')}</th>
								<th class="py-1 pr-3 font-normal">{$i18n.t('Exit')}</th>
								<th class="py-1 pr-3 font-normal text-right">{$i18n.t('Exit price')}</th>
								<th class="py-1 pr-3 font-normal text-right">{$i18n.t('Qty')}</th>
								<th class="py-1 pr-3 font-normal text-right">{$i18n.t('Return')}</th>
								<th class="py-1 pr-3 font-normal text-right">{$i18n.t('P&L')}</th>
								<th class="py-1 pr-3 font-normal">{$i18n.t('Exit reason')}</th>
							</tr>
						</thead>
						<tbody>
							{#each trades.slice(0, TRADE_LIMIT) as t, idx (idx)}
								<tr class="border-t border-gray-100 dark:border-gray-850">
									<td class="py-1.5 pr-3 font-medium">{t.symbol}</td>
									<td class="py-1.5 pr-3 text-xs whitespace-nowrap">{fmtSec(t.entry_t)}</td>
									<td class="py-1.5 pr-3 text-right">{fmtNumber(t.entry_price, 4)}</td>
									<td class="py-1.5 pr-3 text-xs whitespace-nowrap">{fmtSec(t.exit_t)}</td>
									<td class="py-1.5 pr-3 text-right">{fmtNumber(t.exit_price, 4)}</td>
									<td class="py-1.5 pr-3 text-right">{fmtNumber(t.qty, 4)}</td>
									<td class="py-1.5 pr-3 text-right {signClass(t.return_pct)}"
										>{fmtPct(t.return_pct)}</td
									>
									<td class="py-1.5 pr-3 text-right {signClass(t.pnl)}">{fmtMoney(t.pnl)}</td>
									<td class="py-1.5 pr-3 text-xs text-gray-500">{t.exit_reason ?? '—'}</td>
								</tr>
							{/each}
						</tbody>
					</table>
					{#if trades.length > TRADE_LIMIT}
						<div class="mt-2 text-xs text-gray-500">
							{$i18n.t('Showing the first {{limit}} of {{total}} trades.', {
								limit: TRADE_LIMIT,
								total: trades.length
							})}
						</div>
					{/if}
				</div>
			{/if}
		</div>
	{/if}
</div>
