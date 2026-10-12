<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { toast } from 'svelte-sonner';

	import { WEBUI_NAME, user } from '$lib/stores';
	import {
		createFinanceStrategy,
		deleteFinanceStrategyById,
		getFinanceStrategies,
		getFinanceStrategyTemplates,
		type FinanceStrategy,
		type FinanceStrategyForm,
		type FinanceStrategyTemplate
	} from '$lib/apis/finance';
	import type i18nType from '$lib/i18n';

	import Spinner from '$lib/components/common/Spinner.svelte';
	import Modal from '$lib/components/common/Modal.svelte';
	import ConfirmDialog from '$lib/components/common/ConfirmDialog.svelte';
	import { fmtNs } from '$lib/components/markets/backtestFormat';

	const i18n: typeof i18nType = getContext('i18n');

	let loaded = false;
	let strategies: FinanceStrategy[] = [];
	let templates: FinanceStrategyTemplate[] = [];
	let showPicker = false;
	let creating = false;

	let deleteTarget: FinanceStrategy | null = null;
	let showDeleteConfirm = false;

	const BLANK_DEFINITION = {
		direction: 'long',
		indicators: [
			{ id: 'fast', type: 'ema', period: '$fast' },
			{ id: 'slow', type: 'ema', period: '$slow' }
		],
		entry: { all: [{ left: 'fast', op: 'crosses_above', right: 'slow' }] },
		exit: { any: [{ left: 'fast', op: 'crosses_below', right: 'slow' }] },
		stop_loss_pct: null,
		take_profit_pct: null,
		position_size_pct: 100
	};

	const BLANK_PYTHON = `def signals(df, params):
    """Return (entries, exits) as boolean pandas Series aligned to df.index.

    df columns: open, high, low, close, volume (indexed by timestamp).
    """
    fast = df["close"].ewm(span=int(params.get("fast", 10)), adjust=False).mean()
    slow = df["close"].ewm(span=int(params.get("slow", 30)), adjust=False).mean()
    entries = (fast > slow) & (fast.shift(1) <= slow.shift(1))
    exits = (fast < slow) & (fast.shift(1) >= slow.shift(1))
    return entries, exits
`;

	const load = async () => {
		const [s, t] = await Promise.all([
			getFinanceStrategies(localStorage.token).catch((err) => {
				toast.error(`${err}`);
				return null;
			}),
			getFinanceStrategyTemplates(localStorage.token).catch(() => null)
		]);
		strategies = s ?? [];
		templates = t ?? [];
		loaded = true;
	};

	const create = async (form: FinanceStrategyForm) => {
		if (creating) return;
		creating = true;
		const res = await createFinanceStrategy(localStorage.token, form).catch((err) => {
			toast.error(`${err}`);
			return null;
		});
		creating = false;
		if (res) {
			showPicker = false;
			goto(`/markets/strategies/${res.id}`);
		}
	};

	const fromTemplate = (t: FinanceStrategyTemplate) =>
		create({
			name: t.name,
			description: t.description,
			kind: 'template',
			definition: t.definition,
			params: { ...(t.params ?? {}) },
			timeframe: t.timeframe ?? '1Day',
			universe: [...(t.universe ?? [])]
		});

	const blankTemplate = () =>
		create({
			name: $i18n.t('New strategy'),
			description: '',
			kind: 'template',
			definition: BLANK_DEFINITION,
			params: { fast: 10, slow: 30 },
			timeframe: '1Day',
			universe: ['SPY']
		});

	const blankPython = () =>
		create({
			name: $i18n.t('New Python strategy'),
			description: '',
			kind: 'python',
			source: BLANK_PYTHON,
			params: { fast: 10, slow: 30 },
			timeframe: '1Day',
			universe: ['SPY']
		});

	const deleteStrategy = async () => {
		if (!deleteTarget) return;
		const res = await deleteFinanceStrategyById(localStorage.token, deleteTarget.id).catch(
			(err) => {
				toast.error(`${err}`);
				return null;
			}
		);
		if (res) {
			toast.success($i18n.t('Strategy deleted'));
			strategies = strategies.filter((s) => s.id !== deleteTarget?.id);
		}
		deleteTarget = null;
	};

	onMount(load);
</script>

<svelte:head>
	<title>{$i18n.t('Strategies')} / {$WEBUI_NAME}</title>
</svelte:head>

<ConfirmDialog
	bind:show={showDeleteConfirm}
	title={$i18n.t('Delete strategy?')}
	message={$i18n.t('This will permanently delete {{name}}.', { name: deleteTarget?.name ?? '' })}
	on:confirm={deleteStrategy}
/>

<Modal bind:show={showPicker} size="md">
	<div class="px-5 pt-4 pb-5 text-gray-900 dark:text-gray-100">
		<div class="mb-3 flex items-center justify-between">
			<div class="text-lg font-medium">{$i18n.t('New strategy')}</div>
			<button
				class="text-sm text-gray-500 hover:text-gray-900 dark:hover:text-white"
				type="button"
				on:click={() => (showPicker = false)}>{$i18n.t('Close')}</button
			>
		</div>
		<div class="flex flex-col gap-2">
			{#each templates as t (t.id)}
				<button
					class="rounded-xl border border-gray-100 dark:border-gray-850 px-3 py-2 text-left hover:bg-gray-50 dark:hover:bg-gray-850 transition disabled:opacity-50"
					type="button"
					disabled={creating}
					on:click={() => fromTemplate(t)}
				>
					<div class="text-sm font-medium">{t.name}</div>
					{#if t.description}
						<div class="text-xs text-gray-500 line-clamp-2">{t.description}</div>
					{/if}
				</button>
			{/each}
			<button
				class="rounded-xl border border-dashed border-gray-200 dark:border-gray-800 px-3 py-2 text-left hover:bg-gray-50 dark:hover:bg-gray-850 transition disabled:opacity-50"
				type="button"
				disabled={creating}
				on:click={blankTemplate}
			>
				<div class="text-sm font-medium">{$i18n.t('Blank template')}</div>
				<div class="text-xs text-gray-500">
					{$i18n.t('Start from a JSON rule definition (indicators, entry and exit conditions).')}
				</div>
			</button>
			{#if $user?.role === 'admin'}
				<button
					class="rounded-xl border border-dashed border-gray-200 dark:border-gray-800 px-3 py-2 text-left hover:bg-gray-50 dark:hover:bg-gray-850 transition disabled:opacity-50"
					type="button"
					disabled={creating}
					on:click={blankPython}
				>
					<div class="text-sm font-medium">{$i18n.t('Blank Python')}</div>
					<div class="text-xs text-gray-500">
						{$i18n.t('Write a signals(df, params) function in Python (admin only).')}
					</div>
				</button>
			{/if}
		</div>
	</div>
</Modal>

<div class="h-full overflow-y-auto px-2.5 pb-4">
	<div class="flex flex-col gap-3 max-w-6xl mx-auto pt-1">
		<div class="flex items-center justify-between">
			<div class="text-base font-medium">{$i18n.t('Strategies')}</div>
			<button
				class="px-3.5 py-1.5 text-sm font-medium bg-black hover:bg-gray-900 text-white dark:bg-white dark:text-black dark:hover:bg-gray-100 transition rounded-full"
				type="button"
				on:click={() => (showPicker = true)}
			>
				{$i18n.t('New strategy')}
			</button>
		</div>

		{#if !loaded}
			<div class="flex justify-center py-10"><Spinner className="size-5" /></div>
		{:else if strategies.length === 0}
			<div class="py-10 text-center text-sm text-gray-500">
				{$i18n.t('No strategies yet. Create one from a starter template.')}
			</div>
		{:else}
			<div class="overflow-x-auto">
				<table class="w-full text-sm">
					<thead class="text-xs text-gray-500 text-left">
						<tr>
							<th class="py-1.5 pr-3 font-normal">{$i18n.t('Name')}</th>
							<th class="py-1.5 pr-3 font-normal">{$i18n.t('Kind')}</th>
							<th class="py-1.5 pr-3 font-normal">{$i18n.t('Timeframe')}</th>
							<th class="py-1.5 pr-3 font-normal">{$i18n.t('Universe')}</th>
							<th class="py-1.5 pr-3 font-normal">{$i18n.t('Version')}</th>
							<th class="py-1.5 pr-3 font-normal">{$i18n.t('Updated')}</th>
							<th class="py-1.5 font-normal"></th>
						</tr>
					</thead>
					<tbody>
						{#each strategies as s (s.id)}
							<tr
								class="border-t border-gray-100 dark:border-gray-850 hover:bg-gray-50 dark:hover:bg-gray-850/50 cursor-pointer"
								on:click={() => goto(`/markets/strategies/${s.id}`)}
							>
								<td class="py-2 pr-3">
									<div class="font-medium">{s.name}</div>
									{#if s.description}
										<div class="text-xs text-gray-500 line-clamp-1">{s.description}</div>
									{/if}
								</td>
								<td class="py-2 pr-3">
									<span
										class="rounded-full px-2 py-0.5 text-xs {s.kind === 'python'
											? 'bg-yellow-500/15 text-yellow-700 dark:text-yellow-300'
											: 'bg-blue-500/15 text-blue-700 dark:text-blue-300'}"
									>
										{s.kind === 'python' ? $i18n.t('Python') : $i18n.t('Template')}
									</span>
								</td>
								<td class="py-2 pr-3">{s.timeframe}</td>
								<td class="py-2 pr-3 max-w-48 truncate">{(s.universe ?? []).join(', ')}</td>
								<td class="py-2 pr-3">v{s.version}</td>
								<td class="py-2 pr-3 text-xs text-gray-500">{fmtNs(s.updated_at)}</td>
								<td class="py-2 text-right">
									<button
										class="text-xs text-gray-500 hover:text-red-600"
										type="button"
										on:click|stopPropagation={() => {
											deleteTarget = s;
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
