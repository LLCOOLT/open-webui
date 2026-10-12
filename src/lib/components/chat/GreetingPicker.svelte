<script lang="ts">
	import { getContext } from 'svelte';

	import Spinner from '$lib/components/common/Spinner.svelte';
	import Sparkles from '$lib/components/icons/Sparkles.svelte';

	const i18n: any = getContext('i18n');

	// A new chat with a character starts empty; these are the ways to open it instead.
	export let name = '';
	export let greetings: string[] = [];
	export let busy = false;
	export let onPick: (index: number) => void = () => {};
	export let onOpening: () => void = () => {};

	let expanded = false;
	$: shown = expanded ? greetings : greetings.slice(0, 3);

	const preview = (text: string) => text.replace(/\s+/g, ' ').trim();
</script>

<div class="flex flex-col gap-1.5 text-sm">
	<div class="text-xs text-gray-500 dark:text-gray-400 px-1">
		{$i18n.t('Type to start, or open with:')}
	</div>

	<button
		type="button"
		class="flex items-center gap-2 px-3 py-2 rounded-xl text-left border border-gray-100 dark:border-gray-850 hover:bg-gray-50 dark:hover:bg-gray-850 transition disabled:cursor-wait"
		disabled={busy}
		on:click={onOpening}
	>
		{#if busy}
			<Spinner className="size-4 shrink-0" />
		{:else}
			<Sparkles className="size-4 shrink-0" strokeWidth="1.75" />
		{/if}
		<span class="min-w-0">
			<span class="block font-medium">
				{$i18n.t('Let {{NAME}} open', { NAME: name })}
			</span>
			<span class="block text-xs text-gray-500 dark:text-gray-400">
				{$i18n.t('A new first message that picks up from what {{NAME}} remembers about you', {
					NAME: name
				})}
			</span>
		</span>
	</button>

	{#each shown as text, index}
		<button
			type="button"
			class="px-3 py-2 rounded-xl text-left border border-gray-100 dark:border-gray-850 hover:bg-gray-50 dark:hover:bg-gray-850 transition disabled:opacity-50"
			disabled={busy}
			title={$i18n.t('Use this greeting')}
			on:click={() => onPick(index)}
		>
			<span class="block text-xs text-gray-500 dark:text-gray-400">
				{index === 0 ? $i18n.t("Card's greeting") : $i18n.t('Greeting {{N}}', { N: index + 1 })}
			</span>
			<span class="line-clamp-2 text-gray-700 dark:text-gray-300">{preview(text)}</span>
		</button>
	{/each}

	{#if greetings.length > 3}
		<button
			type="button"
			class="self-start px-1 text-xs text-gray-500 hover:underline"
			on:click={() => (expanded = !expanded)}
		>
			{expanded
				? $i18n.t('Show fewer')
				: $i18n.t('Show all {{COUNT}} greetings', { COUNT: greetings.length })}
		</button>
	{/if}
</div>
