<script lang="ts">
	// Characters home: every character you can chat with, to start a chat in one tap, and the ways
	// to make new ones (paste a card, import card files, browse card sites).
	import { getPageContext } from '$lib/utils/page';
	const { goto, tab: pageTab } = getPageContext();

	import { getContext } from 'svelte';
	import { WEBUI_NAME, config, mobile, models, showSidebar, user } from '$lib/stores';
	import { getModels } from '$lib/apis';
	import { characterImageUrl } from '$lib/utils/characters';
	import { WEBUI_API_BASE_URL } from '$lib/constants';

	import PageTitle from '$lib/components/common/PageTitle.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Sidebar from '$lib/components/icons/Sidebar.svelte';
	import Search from '$lib/components/icons/Search.svelte';
	import Pencil from '$lib/components/icons/Pencil.svelte';
	import Plus from '$lib/components/icons/Plus.svelte';
	import PasteCharacterModal from '$lib/components/workspace/Models/PasteCharacterModal.svelte';
	import CharacterImportModal from '$lib/components/workspace/Models/CharacterImportModal.svelte';

	const i18n: any = getContext('i18n');

	let query = '';
	let showPaste = false;
	let showImport = false;

	$: canCreate = $user?.role === 'admin' || !!$user?.permissions?.workspace?.models;
	$: isAdmin = $user?.role === 'admin';

	$: characters = (($models ?? []) as any[])
		.filter((model) => model?.info?.meta?.character && !(model?.info?.meta?.hidden ?? false))
		.map((model) => {
			const character = model.info.meta.character;
			const tags = [
				...(model.info.meta.tags ?? []).map((tag: any) => tag?.name ?? tag),
				...(character.tags ?? [])
			].filter((tag) => typeof tag === 'string' && tag);
			return {
				id: model.id,
				name: model.name || character.name,
				blurb: (model.info.meta.description || character.creator_notes || '').trim(),
				greetings: character.greeting_count ?? 0,
				image: characterImageUrl(
					model,
					`${WEBUI_API_BASE_URL}/models/model/profile/image?id=${encodeURIComponent(model.id)}`
				),
				tags: [...new Set(tags)].slice(0, 4),
				editable: isAdmin || model.info.user_id === $user?.id
			};
		})
		.sort((a, b) => a.name.localeCompare(b.name));

	$: filtered = query.trim()
		? characters.filter((character) =>
				[character.name, character.blurb, ...character.tags]
					.join(' ')
					.toLowerCase()
					.includes(query.trim().toLowerCase())
			)
		: characters;

	const startChat = (id: string) => goto(`/?models=${encodeURIComponent(id)}`);

	const refreshModels = async () => models.set(await getModels(localStorage.token));

	const buttonClass =
		'px-3 py-1.5 text-sm rounded-full transition flex items-center gap-1.5 whitespace-nowrap';
</script>

<PageTitle title={$i18n.t('Characters') + ' / ' + $WEBUI_NAME} />

<PasteCharacterModal
	bind:show={showPaste}
	onCreated={async (model) => {
		await refreshModels();
		startChat(model.id);
	}}
/>
{#if isAdmin}
	<CharacterImportModal bind:show={showImport} onImported={refreshModels} />
{/if}

<div
	class="flex flex-col w-full h-full max-h-full transition-width duration-200 ease-in-out {$showSidebar &&
	!pageTab
		? 'md:max-w-[calc(100%-var(--sidebar-width))]'
		: ''} max-w-full"
>
	<nav class="px-1.5 md:px-2.5 pt-1.5 drag-region select-none">
		<div class="flex items-center gap-1">
			{#if $mobile && !pageTab}
				<Tooltip content={$showSidebar ? $i18n.t('Close Sidebar') : $i18n.t('Open Sidebar')}>
					<button
						class="flex rounded-lg p-1.5 hover:bg-gray-100 dark:hover:bg-gray-850 transition"
						aria-label={$showSidebar ? $i18n.t('Close Sidebar') : $i18n.t('Open Sidebar')}
						on:click={() => showSidebar.set(!$showSidebar)}
					>
						<Sidebar className="size-4" />
					</button>
				</Tooltip>
			{/if}
			<h1 class="px-1 text-lg font-medium">{$i18n.t('Characters')}</h1>
		</div>
	</nav>

	<div class="flex-1 overflow-y-auto px-3 md:px-5 pb-6">
		<div class="mx-auto max-w-6xl">
			<div class="mt-2 mb-4 flex flex-wrap items-center gap-2">
				<div
					class="flex flex-1 min-w-48 items-center gap-2 rounded-full bg-gray-50 dark:bg-gray-850 px-3 py-1.5"
				>
					<Search className="size-3.5 text-gray-400" />
					<input
						class="w-full bg-transparent text-sm outline-hidden"
						placeholder={$i18n.t('Search characters')}
						aria-label={$i18n.t('Search characters')}
						bind:value={query}
					/>
				</div>
				{#if canCreate}
					<button
						class="{buttonClass} bg-black hover:bg-gray-900 text-white dark:bg-white dark:text-black dark:hover:bg-gray-100"
						on:click={() => (showPaste = true)}
					>
						<Plus className="size-3.5" strokeWidth="2.5" />
						{$i18n.t('New from text')}
					</button>
				{/if}
				{#if isAdmin}
					<button
						class="{buttonClass} bg-gray-50 hover:bg-gray-100 dark:bg-gray-850 dark:hover:bg-gray-800"
						on:click={() => (showImport = true)}
					>
						{$i18n.t('Import cards')}
					</button>
					<button
						class="{buttonClass} bg-gray-50 hover:bg-gray-100 dark:bg-gray-850 dark:hover:bg-gray-800"
						on:click={() => goto('/workspace/models/browse')}
					>
						{$i18n.t('Browse')}
					</button>
				{/if}
			</div>

			{#if filtered.length}
				<div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-3">
					{#each filtered as character (character.id)}
						<div
							class="group relative flex flex-col overflow-hidden rounded-2xl bg-gray-50 dark:bg-gray-850 hover:shadow-md transition"
						>
							<button
								class="flex flex-col text-left"
								aria-label={$i18n.t('Chat with {{NAME}}', { NAME: character.name })}
								on:click={() => startChat(character.id)}
							>
								<img
									src={character.image}
									alt=""
									loading="lazy"
									class="aspect-[3/4] w-full object-cover bg-gray-100 dark:bg-gray-800"
								/>
								<div class="p-2.5">
									<div class="font-medium text-sm truncate">{character.name}</div>
									{#if character.blurb}
										<div class="mt-0.5 text-xs text-gray-500 line-clamp-2">{character.blurb}</div>
									{/if}
									<div class="mt-1.5 flex flex-wrap gap-1">
										{#each character.tags as tag}
											<span
												class="rounded-md bg-gray-200/70 dark:bg-gray-800 px-1.5 py-0.5 text-[0.6875rem] text-gray-600 dark:text-gray-400"
												>{tag}</span
											>
										{/each}
									</div>
								</div>
							</button>
							{#if character.editable}
								<Tooltip content={$i18n.t('Edit')} className="absolute top-2 right-2">
									<button
										class="rounded-full bg-white/80 dark:bg-gray-900/80 p-1.5 transition {$mobile
											? ''
											: 'opacity-0 group-hover:opacity-100 focus:opacity-100'}"
										aria-label={$i18n.t('Edit {{NAME}}', { NAME: character.name })}
										on:click={() =>
											goto(`/workspace/models/edit?id=${encodeURIComponent(character.id)}`)}
									>
										<Pencil className="size-3.5" />
									</button>
								</Tooltip>
							{/if}
						</div>
					{/each}
				</div>
			{:else if characters.length}
				<div class="py-16 text-center text-sm text-gray-500">
					{$i18n.t('No characters match your search.')}
				</div>
			{:else}
				<div class="py-16 text-center">
					<div class="text-base font-medium">{$i18n.t('No characters yet')}</div>
					<div class="mt-1 text-sm text-gray-500 max-w-md mx-auto">
						{#if canCreate}
							{$i18n.t(
								'Paste a character card (JSON or text with Name, Personality, Scenario, First message...) to create one.'
							)}
						{:else}
							{$i18n.t('Ask an admin to add characters you can chat with.')}
						{/if}
					</div>
					{#if canCreate}
						<button
							class="{buttonClass} mx-auto mt-4 bg-black hover:bg-gray-900 text-white dark:bg-white dark:text-black dark:hover:bg-gray-100"
							on:click={() => (showPaste = true)}
						>
							<Plus className="size-3.5" strokeWidth="2.5" />
							{$i18n.t('New from text')}
						</button>
					{/if}
				</div>
			{/if}
		</div>
	</div>
</div>
