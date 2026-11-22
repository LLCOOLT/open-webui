<script lang="ts">
	// Who you are playing as in this chat, switchable in one tap from the input bar. The same choice
	// as Chat Controls > Roleplay ("You are"); it is sent with every message (see chatPersonaId).
	import { getContext } from 'svelte';
	import { chatPersonaId, settings } from '$lib/stores';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import ChevronDown from '$lib/components/icons/ChevronDown.svelte';

	const i18n: any = getContext('i18n');

	$: personas = $settings?.personas ?? [];
	$: defaultPersona = personas.find((persona) => persona.is_default);
	$: current =
		$chatPersonaId === ''
			? null
			: (personas.find((persona) => persona.id === $chatPersonaId) ?? defaultPersona ?? null);
</script>

{#if personas.length}
	<Tooltip content={$i18n.t('Who you are in this chat')} placement="top">
		<label
			class="relative px-2 py-[0.3125rem] flex items-center gap-1.5 text-xs font-medium rounded-full transition-colors duration-300 bg-transparent text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800 cursor-pointer max-w-36"
		>
			{#if current?.avatar_url}
				<img src={current.avatar_url} alt="" class="size-4 rounded-full object-cover shrink-0" />
			{:else}
				<span
					class="size-4 rounded-full bg-gray-200 dark:bg-gray-700 text-[0.625rem] flex items-center justify-center shrink-0"
					>{(current?.name ?? '?').slice(0, 1).toUpperCase()}</span
				>
			{/if}
			<span class="truncate">{current?.name ?? $i18n.t('No persona')}</span>
			<ChevronDown className="size-2.5 shrink-0" strokeWidth="2.5" />
			<select
				class="absolute inset-0 opacity-0 cursor-pointer"
				aria-label={$i18n.t('Who you are in this chat')}
				value={$chatPersonaId ?? '__default__'}
				on:change={(event) => {
					const value = event.currentTarget.value;
					chatPersonaId.set(value === '__default__' ? null : value);
				}}
			>
				<option value="__default__">
					{defaultPersona
						? $i18n.t('{{NAME}} (default)', { NAME: defaultPersona.name })
						: $i18n.t('Default')}
				</option>
				{#each personas as persona (persona.id)}
					<option value={persona.id}>{persona.name}</option>
				{/each}
				<option value="">{$i18n.t('No persona')}</option>
			</select>
		</label>
	</Tooltip>
{/if}
