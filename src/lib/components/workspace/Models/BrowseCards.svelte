<script lang="ts">
	import { onMount } from 'svelte';
	import { getPageContext } from '$lib/utils/page';
	import { config, user } from '$lib/stores';
	import CardBrowser from '$lib/components/workspace/Models/CardBrowser.svelte';

	const { goto } = getPageContext();

	onMount(() => {
		// Admin only, like character card import.
		if ($user?.role !== 'admin' || !$config?.features?.enable_character_cards) {
			goto('/workspace/models', { replaceState: true });
		}
	});
</script>

{#if $user?.role === 'admin'}
	<CardBrowser />
{/if}
