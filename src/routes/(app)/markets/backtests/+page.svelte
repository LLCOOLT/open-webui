<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { toast } from 'svelte-sonner';

	import { WEBUI_NAME } from '$lib/stores';
	import {
		deleteFinanceBacktestById,
		getFinanceBacktests,
		type FinanceBacktest
	} from '$lib/apis/finance';
	import type i18nType from '$lib/i18n';

	import Spinner from '$lib/components/common/Spinner.svelte';
	import ConfirmDialog from '$lib/components/common/ConfirmDialog.svelte';
	import {
		fmtNs,
		fmtNumber,
		fmtPct,
		signClass,
		statusClass
	} from '$lib/components/markets/backtestFormat';

	const i18n: typeof i18nType = getContext('i18n');

	let loaded = false;
	let backtests: FinanceBacktest[] = [];
	let deleteTarget: FinanceBacktest | null = null;
	let showDeleteConfirm = false;

	const load = async () => {
		backtests =
			(await getFinanceBacktests(localStorage.token, null, 200).catch((err) => {
				toast.error(`${err}`);
				return null;
			})) ?? [];
		loaded = true;
	};

	const deleteBacktest = async () => {
		if (!deleteTarget) return;
		const id = deleteTarget.id;
		const res = await deleteFinanceBacktestById(localStorage.token, id).catch((err) => {
			toast.error(`${err}`);
			return null;
		});
		if (res) {
			toast.success($i18n.t('Backtest deleted'));
			backtests = backtests.filter((b) => b.id !== id);
		}
		deleteTarget = null;
	};

	onMount(load);
</script>

<svelte:head>
	<title>{$i18n.t('Backtests')} / {$WEBUI_NAME}</title>
</svelte:head>

<ConfirmDialog
	bind:show={showDeleteConfirm}
	title={$i18n.t('Delete backtest?')}
	message={$i18n.t('This will permanently delete {{name}}.', { name: deleteTarget?.name ?? '' })}
	on:confirm={deleteBacktest}
/>

<div class="h-full overflow-y-auto px-2.5 pb-4">
	<div class="flex flex-col gap-3 max-w-6xl mx-auto pt-1">
		<div class="text-base font-medium">{$i18n.t('Backtests')}</div>
		{#if !loaded}
			<div class="flex justify-center py-10"><Spinner className="size-5" /></div>
		{:else if backtests.length === 0}
			<div class="py-10 text-center text-sm text-gray-500">
				{$i18n.t('No backtests yet. Run one from a strategy.')}
			</div>
		{:else}
			<div class="overflow-x-auto">
				<table class="w-full text-sm">
					<thead class="text-xs text-gray-500 text-left">
						<tr>
							<th class="py-1.5 pr-3 font-normal">{$i18n.t('Name')}</th>
							<th class="py-1.5 pr-3 font-normal">{$i18n.t('Symbols')}</th>
							<th class="py-1.5 pr-3 font-normal">{$i18n.t('Range')}</th>
							<th class="py-1.5 pr-3 font-normal">{$i18n.t('Status')}</th>
							<th class="py-1.5 pr-3 font-normal text-right">{$i18n.t('Return')}</th>
							<th class="py-1.5 pr-3 font-normal text-right">{$i18n.t('Buy & hold')}</th>
							<th class="py-1.5 pr-3 font-normal text-right">{$i18n.t('Sharpe')}</th>
							<th class="py-1.5 pr-3 font-normal">{$i18n.t('Created')}</th>
							<th class="py-1.5 font-normal"></th>
						</tr>
					</thead>
					<tbody>
						{#each backtests as b (b.id)}
							<tr
								class="border-t border-gray-100 dark:border-gray-850 hover:bg-gray-50 dark:hover:bg-gray-850/50 cursor-pointer"
								on:click={() => goto(`/markets/backtests/${b.id}`)}
							>
								<td class="py-2 pr-3 font-medium">{b.name}</td>
								<td class="py-2 pr-3 max-w-40 truncate">{(b.config?.symbols ?? []).join(', ')}</td>
								<td class="py-2 pr-3 text-xs text-gray-500 whitespace-nowrap"
									>{b.config?.start ?? '—'} → {b.config?.end ?? '—'}</td
								>
								<td class="py-2 pr-3">
									<span class="rounded-full px-2 py-0.5 text-xs {statusClass(b.status)}"
										>{b.status}</span
									>
								</td>
								<td class="py-2 pr-3 text-right {signClass(b.metrics?.total_return)}"
									>{fmtPct(b.metrics?.total_return)}</td
								>
								<td class="py-2 pr-3 text-right {signClass(b.benchmark?.total_return)}"
									>{fmtPct(b.benchmark?.total_return)}</td
								>
								<td class="py-2 pr-3 text-right">{fmtNumber(b.metrics?.sharpe)}</td>
								<td class="py-2 pr-3 text-xs text-gray-500 whitespace-nowrap"
									>{fmtNs(b.created_at)}</td
								>
								<td class="py-2 text-right">
									<button
										class="text-xs text-gray-500 hover:text-red-600"
										type="button"
										on:click|stopPropagation={() => {
											deleteTarget = b;
											showDeleteConfirm = true;
										}}>{$i18n.t('Delete')}</button
									>
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}
	</div>
</div>
