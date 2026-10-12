<script lang="ts">
	import { createEventDispatcher, getContext, onDestroy } from 'svelte';

	import { searchFinanceAssets, type FinanceAsset } from '$lib/apis/finance';
	import type i18nType from '$lib/i18n';

	import Search from '$lib/components/icons/Search.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';

	const i18n: typeof i18nType = getContext('i18n');
	const dispatch = createEventDispatcher<{ select: FinanceAsset }>();

	let query = '';
	let results: FinanceAsset[] = [];
	let open = false;
	let loading = false;
	let timer: ReturnType<typeof setTimeout>;
	let requestId = 0;

	const onInput = () => {
		clearTimeout(timer);
		const q = query.trim();
		if (!q) {
			results = [];
			open = false;
			loading = false;
			return;
		}
		loading = true;
		timer = setTimeout(async () => {
			const id = ++requestId;
			const res = await searchFinanceAssets(localStorage.token, q, 10).catch(() => []);
			if (id !== requestId) return;
			results = res ?? [];
			loading = false;
			open = true;
		}, 300);
	};

	const select = (asset: FinanceAsset) => {
		dispatch('select', asset);
		query = '';
		results = [];
		open = false;
	};

	onDestroy(() => clearTimeout(timer));
</script>

<div class="relative w-full">
	<div class="flex items-center rounded-xl px-2.5 h-8 bg-gray-50 dark:bg-gray-850 text-sm">
		<div class="mr-2 text-gray-500">
			<Search className="size-3.5" />
		</div>
		<input
			class="w-full bg-transparent outline-hidden"
			bind:value={query}
			on:input={onInput}
			on:focus={() => (open = results.length > 0)}
			on:blur={() => setTimeout(() => (open = false), 150)}
			on:keydown={(e) => {
				if (e.key === 'Enter') {
					e.preventDefault();
					if (results[0]) {
						select(results[0]);
					} else if (query.trim()) {
						select({
							symbol: query.trim().toUpperCase(),
							name: '',
							exchange: '',
							asset_class: 'us_equity',
							tradable: true
						});
					}
				} else if (e.key === 'Escape') {
					open = false;
				}
			}}
			placeholder={$i18n.t('Search symbols')}
			aria-label={$i18n.t('Search symbols')}
			maxlength="64"
		/>
		{#if loading}
			<Spinner className="size-3.5" />
		{/if}
	</div>

	{#if open}
		<div
			class="absolute z-30 mt-1 w-full max-h-72 overflow-y-auto rounded-xl border border-gray-100 dark:border-gray-800 bg-white dark:bg-gray-900 shadow-lg p-1"
		>
			{#each results as asset (asset.symbol)}
				<button
					class="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-sm hover:bg-gray-50 dark:hover:bg-gray-850"
					type="button"
					on:mousedown|preventDefault={() => select(asset)}
				>
					<span class="font-medium w-16 shrink-0">{asset.symbol}</span>
					<span class="truncate text-gray-500 flex-1">{asset.name}</span>
					<span class="text-xs text-gray-400 shrink-0">{asset.exchange}</span>
				</button>
			{:else}
				<div class="px-2 py-1.5 text-sm text-gray-500">{$i18n.t('No results found')}</div>
			{/each}
		</div>
	{/if}
</div>
