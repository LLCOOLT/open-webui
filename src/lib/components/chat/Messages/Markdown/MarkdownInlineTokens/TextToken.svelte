<script lang="ts">
	import { settings } from '$lib/stores';

	export let token;
	export let done = true;

	// Quoted dialogue gets a class; it is only styled inside .roleplay-format (character chats with
	// roleplay formatting on), so other chats look the same.
	const DIALOGUE_RE = /("[^"\n]+"|“[^”\n]+”)/;

	$: raw = token?.raw ?? '';
	$: parts = done ? raw.split(DIALOGUE_RE) : [raw];
</script>

{#if done || !($settings?.chatFadeStreamingText ?? true)}
	{#each parts as part, index}{#if index % 2 === 1}<span class="rp-dialogue">{part}</span
			>{:else}{part}{/if}{/each}
{:else}
	{#each raw.split(' ') as text}
		<span class="fade-in-token">
			{text}{' '}
		</span>
	{/each}
{/if}
