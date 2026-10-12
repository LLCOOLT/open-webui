<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import { goto } from '$app/navigation';

	import { config, mobile, showSidebar, user } from '$lib/stores';
	import type i18nType from '$lib/i18n';
	import SidebarIcon from '$lib/components/icons/Sidebar.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';

	const i18n: typeof i18nType = getContext('i18n');

	let allowed = false;

	onMount(() => {
		if (
			!($config?.features as { enable_finance?: boolean } | undefined)?.enable_finance ||
			($user?.role !== 'admin' &&
				!(($user?.permissions?.features as { finance?: boolean } | undefined)?.finance ?? false))
		) {
			goto('/');
			return;
		}
		allowed = true;
	});
</script>

<div
	class="flex flex-col w-full h-screen max-h-[100dvh] transition-width duration-200 ease-in-out {$showSidebar
		? 'md:max-w-[calc(100%-var(--sidebar-width))]'
		: ''} max-w-full"
>
	<div class="flex h-full min-h-0 flex-col">
		<div class="shrink-0 px-2.5 pt-2 pb-1">
			<div class="flex items-center gap-0.5 md:gap-1">
				{#if $mobile}
					<div class="{$showSidebar ? 'md:hidden' : ''} flex flex-none items-center">
						<Tooltip content={$showSidebar ? $i18n.t('Close Sidebar') : $i18n.t('Open Sidebar')}>
							<button
								id="sidebar-toggle-button"
								class="flex size-7 items-center justify-center text-gray-400 transition"
								aria-label={$showSidebar ? $i18n.t('Close Sidebar') : $i18n.t('Open Sidebar')}
								on:click={() => {
									showSidebar.set(!$showSidebar);
								}}
								type="button"
							>
								<SidebarIcon className="size-4" />
							</button>
						</Tooltip>
					</div>
				{/if}

				<div class="flex w-full min-w-0 items-center py-1">
					<span class="min-w-fit px-1 text-sm select-none">{$i18n.t('Markets')}</span>
				</div>
			</div>
		</div>

		<div class="min-h-0 flex-1 overflow-hidden">
			{#if allowed}
				<slot />
			{/if}
		</div>
	</div>
</div>
