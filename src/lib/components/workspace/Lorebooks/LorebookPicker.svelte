<script lang="ts">
	import { createEventDispatcher, getContext, onMount } from 'svelte';

	import { getLorebooks, type Lorebook } from '$lib/apis/lorebooks';
	import XMark from '$lib/components/icons/XMark.svelte';

	const i18n: any = getContext('i18n');
	const dispatch = createEventDispatcher<{ change: string[] }>();

	// Attached lorebook ids, in order.
	export let value: string[] = [];
	export let disabled = false;

	let available: Lorebook[] = [];
	let loaded = false;

	$: byId = Object.fromEntries(available.map((lorebook) => [lorebook.id, lorebook]));
	$: options = available.filter((lorebook) => !value.includes(lorebook.id));

	const update = (ids: string[]) => {
		value = ids;
		dispatch('change', ids);
	};

	onMount(async () => {
		try {
			available = (await getLorebooks(localStorage.token)).items;
		} catch {
			available = [];
		}
		loaded = true;
	});
</script>

<div class="flex flex-wrap items-center gap-1.5">
	{#each value as id (id)}
		<span
			class="flex items-center gap-1 rounded-lg bg-gray-100 dark:bg-gray-850 px-2 py-0.5 text-xs"
		>
			{byId[id]?.name ?? (loaded ? $i18n.t('Unavailable lorebook') : '…')}
			{#if !disabled}
				<button
					type="button"
					class="text-gray-500 hover:text-gray-900 dark:hover:text-gray-100"
					aria-label={$i18n.t('Detach {{NAME}}', { NAME: byId[id]?.name ?? id })}
					on:click={() => update(value.filter((other) => other !== id))}
				>
					<XMark className="size-3" />
				</button>
			{/if}
		</span>
	{/each}

	{#if !disabled && options.length}
		<select
			class="rounded-lg bg-transparent pr-6 text-xs text-gray-500 outline-hidden cursor-pointer"
			aria-label={$i18n.t('Attach a lorebook')}
			value=""
			on:change={(event) => {
				const id = event.currentTarget.value;
				event.currentTarget.value = '';
				if (id) update([...value, id]);
			}}
		>
			<option value="">{$i18n.t('+ Attach lorebook')}</option>
			{#each options as lorebook (lorebook.id)}
				<option value={lorebook.id}>{lorebook.name}</option>
			{/each}
		</select>
	{:else if loaded && !value.length}
		<span class="text-xs text-gray-400">{$i18n.t('No lorebooks available')}</span>
	{/if}
</div>
