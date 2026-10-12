<script lang="ts">
	import CardText from './CardText.svelte';
	import { cardPlainText } from '$lib/utils/cardText';
	import { getContext, onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { toast } from 'svelte-sonner';

	import { config, models, settings, user } from '$lib/stores';
	import { getModels } from '$lib/apis';
	import {
		getCardSources,
		importCardFromSource,
		previewCardFromSource,
		searchCardSource,
		type CardPreview,
		type CardSource,
		type CardSummary
	} from '$lib/apis/models';
	import Modal from '$lib/components/common/Modal.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';

	import Spinner from '$lib/components/common/Spinner.svelte';
	import Switch from '$lib/components/common/Switch.svelte';
	import ChevronLeft from '$lib/components/icons/ChevronLeft.svelte';
	import ModelSelector from '$lib/components/chat/ModelSelector/Selector.svelte';

	const i18n: any = getContext('i18n');

	const SORTS = [
		{ value: 'popular', label: 'Popular' },
		{ value: 'trending', label: 'Trending' },
		{ value: 'newest', label: 'Newest' }
	];

	let sources: CardSource[] = [];
	let source = '';
	let query = '';
	let sort = 'popular';
	let nsfw = false;
	let page = 1;
	let items: CardSummary[] = [];
	let hasMore = false;
	let loading = false;
	let error = '';
	let controller: AbortController | null = null;

	let baseModelId: string | null = null;
	let isPublic = false;
	let link = '';
	let importingKey: string | null = null;
	let imported: Record<string, string> = {};
	// The card being read in the preview dialog.
	let previewItem: CardSummary | null = null;
	let preview: CardPreview | null = null;
	let previewError = '';
	let showPreview = false;
	let greetingIndex = 0;

	const openPreview = async (item: CardSummary) => {
		previewItem = item;
		preview = null;
		previewError = '';
		greetingIndex = 0;
		showPreview = true;
		try {
			const result = await previewCardFromSource(localStorage.token, item.source, item.id);
			if (previewItem === item) preview = result;
		} catch (err: any) {
			if (previewItem === item) previewError = `${err?.detail ?? err}`;
		}
	};

	// Shown as the card would read in a chat with you.
	const expand = (text: string) =>
		(text ?? '')
			.replace(/\{\{char\}\}/gi, preview?.name || previewItem?.name || '')
			.replace(/\{\{user\}\}/gi, $user?.name || 'You');

	$: greetings = preview
		? [preview.first_mes, ...preview.alternate_greetings].filter((g) => g?.trim())
		: [];
	$: previewSections = preview
		? [
				// Prompt-like fields stay as written; the rest render like the card's author laid them out.
				{ label: $i18n.t('Description'), text: preview.description, rendered: true },
				{ label: $i18n.t('Personality'), text: preview.personality, rendered: true },
				{ label: $i18n.t('Scenario'), text: preview.scenario, rendered: true },
				{ label: $i18n.t('Example Dialogue'), text: preview.mes_example, rendered: false },
				{
					label: $i18n.t('Character System Prompt'),
					text: preview.system_prompt,
					rendered: false
				}
			].filter((section) => section.text?.trim())
		: [];

	$: baseModelItems = (($models ?? []) as any[])
		.filter((model) => !model?.preset && model?.owned_by !== 'arena' && !(model?.direct ?? false))
		.map((model) => ({ value: model.id, label: model.name, model }));

	const keyOf = (item: CardSummary) => `${item.source}:${item.id}`;

	// Tag filters per site (tag vocabularies differ between sites).
	type TagFilters = { include: string[]; exclude: string[] };
	let tagFilters: Record<string, TagFilters> = {};
	let tagInput = '';
	$: currentSource = sources.find((item) => item.id === source);
	$: supportsTags = currentSource?.supports_tags ?? false;
	$: resultTagsClickable = currentSource?.result_tags ?? false;
	$: activeTags = tagFilters[source] ?? { include: [], exclude: [] };

	const setTags = (filters: TagFilters) => {
		tagFilters = { ...tagFilters, [source]: filters };
		search(1);
	};

	const addTag = (raw: string, exclude = false) => {
		let tag = raw.trim().replace(/,/g, ' ');
		if (tag.startsWith('-') || tag.startsWith('−')) {
			exclude = true;
			tag = tag.slice(1).trim();
		}
		if (!tag) return;
		const lower = tag.toLowerCase();
		const include = activeTags.include.filter((t) => t.toLowerCase() !== lower);
		const excluded = activeTags.exclude.filter((t) => t.toLowerCase() !== lower);
		setTags(
			exclude
				? { include, exclude: [...excluded, tag] }
				: { include: [...include, tag], exclude: excluded }
		);
	};

	// Filtered-on tags first, so a tile shows why it matched.
	const tagsFirst = (tags: string[], wanted: string[]) => {
		const lower = new Set(wanted.map((t) => t.toLowerCase()));
		return [...tags].sort(
			(a, b) => Number(lower.has(b.toLowerCase())) - Number(lower.has(a.toLowerCase()))
		);
	};

	const removeTag = (tag: string) =>
		setTags({
			include: activeTags.include.filter((t) => t !== tag),
			exclude: activeTags.exclude.filter((t) => t !== tag)
		});

	const loadPreferences = () => {
		try {
			const saved = JSON.parse(localStorage.getItem('cardBrowser') ?? '{}');
			if (typeof saved.source === 'string') source = saved.source;
			if (typeof saved.sort === 'string') sort = saved.sort;
			if (typeof saved.nsfw === 'boolean') nsfw = saved.nsfw;
			if (typeof saved.baseModelId === 'string') baseModelId = saved.baseModelId;
			if (saved.tagFilters && typeof saved.tagFilters === 'object') tagFilters = saved.tagFilters;
		} catch {
			// Preferences are a convenience only.
		}
	};

	const savePreferences = () => {
		try {
			localStorage.setItem(
				'cardBrowser',
				JSON.stringify({ source, sort, nsfw, baseModelId, tagFilters })
			);
		} catch {
			// Ignore storage errors.
		}
	};

	const search = async (nextPage = 1) => {
		if (!source) return;
		controller?.abort();
		const current = new AbortController();
		controller = current;
		loading = true;
		error = '';
		savePreferences();
		try {
			const result = await searchCardSource(
				localStorage.token,
				source,
				{
					q: query.trim(),
					page: nextPage,
					sort,
					nsfw,
					tags: (tagFilters[source] ?? { include: [] }).include,
					exclude_tags: (tagFilters[source] ?? { exclude: [] }).exclude
				},
				current.signal
			);
			if (current.signal.aborted) return;
			items = result.items;
			hasMore = result.has_more;
			page = result.page;
		} catch (err: any) {
			if (current.signal.aborted) return;
			items = [];
			hasMore = false;
			error = `${err?.detail ?? err}`;
		} finally {
			if (controller === current) loading = false;
		}
	};

	const selectSource = (id: string) => {
		if (id === source) return;
		source = id;
		search(1);
	};

	const refreshModels = async () => {
		await models.set(
			await getModels(
				localStorage.token,
				($config?.features?.enable_direct_connections && $settings?.directConnections) || null
			)
		);
	};

	const runImport = async (key: string, form: { source?: string; id?: string; url?: string }) => {
		if (!baseModelId) {
			toast.error($i18n.t('Please select a base model'));
			return;
		}
		importingKey = key;
		savePreferences();
		try {
			const created = await importCardFromSource(localStorage.token, {
				...form,
				base_model_id: baseModelId,
				public: isPublic
			});
			const model = created?.[0];
			if (model) {
				imported = { ...imported, [key]: model.id };
				toast.success($i18n.t('Imported {{NAME}}', { NAME: model.name }));
			}
			await refreshModels();
		} catch (err: any) {
			toast.error(`${err?.detail ?? err}`);
		} finally {
			importingKey = null;
		}
	};

	const importItem = (item: CardSummary) =>
		runImport(keyOf(item), { source: item.source, id: item.id });

	const importLink = async () => {
		if (!link.trim()) return;
		await runImport('link', { url: link.trim() });
		if (imported.link) link = '';
	};

	// Character Tavern thumbnails go through a resizing CDN that sometimes rate-limits;
	// fall back to the full card image, then to the initial.
	const imageFailed = (event: Event, item: CardSummary) => {
		const image = event.currentTarget as HTMLImageElement;
		const full = item.image_url.replace(/\/cdn-cgi\/image\/[^/]+/, '');
		if (full !== item.image_url && image.src !== full) {
			image.src = full;
		} else {
			image.style.display = 'none';
		}
	};

	onMount(async () => {
		loadPreferences();
		try {
			// Sites without search (Poe) are imported by link only.
			sources = (await getCardSources(localStorage.token)).filter(
				(item) => item.searchable !== false
			);
		} catch (err: any) {
			error = `${err?.detail ?? err}`;
			return;
		}
		if (!sources.some((item) => item.id === source)) source = sources[0]?.id ?? '';
		search(1);
	});

	const inputClass =
		'w-full bg-transparent text-sm outline-hidden placeholder:text-gray-300 dark:placeholder:text-gray-700';
	const buttonClass =
		'shrink-0 whitespace-nowrap px-3 py-1 text-xs rounded-lg bg-gray-100 dark:bg-gray-850 hover:bg-gray-200 dark:hover:bg-gray-800 flex items-center gap-1.5 disabled:opacity-50';
</script>

<div class="max-w-6xl mx-auto w-full pb-12">
	<button
		type="button"
		class="flex items-center gap-1 text-xs text-gray-500 hover:text-gray-900 dark:hover:text-gray-100 w-fit mb-3"
		on:click={() => goto('/workspace/models')}
	>
		<ChevronLeft className="size-3" />
		{$i18n.t('Back')}
	</button>

	<div class="text-xl font-medium">{$i18n.t('Browse Character Cards')}</div>
	<div class="text-sm text-gray-500 mt-1">
		{$i18n.t(
			'Search public character card sites and import a card as a character model, with its greetings and lorebook. Cards are made by the sites’ users; review them before sharing.'
		)}
	</div>

	<div class="mt-5 grid gap-3 md:grid-cols-2">
		<div>
			<div class="mb-1 text-xs text-gray-500">{$i18n.t('Base Model (From)')}</div>
			<ModelSelector
				id="card-browser-base-model"
				placeholder={$i18n.t('Select a base model (e.g. llama3, gpt-4o)')}
				searchPlaceholder={$i18n.t('Search a model')}
				items={baseModelItems}
				triggerClassName="text-xs"
				selectionOnly
				includeHidden
				bind:value={baseModelId}
			/>
			<div class="mt-2 flex items-center gap-2 text-xs">
				<Switch bind:state={isPublic} ariaLabel={$i18n.t('Visible to all users')} />
				<span>{$i18n.t('Visible to all users')}</span>
			</div>
		</div>

		<form class="flex flex-col" on:submit|preventDefault={importLink}>
			<label for="card-link" class="mb-1 text-xs text-gray-500"
				>{$i18n.t('Import from a link')}</label
			>
			<div class="flex items-center gap-2">
				<input
					id="card-link"
					class="{inputClass} rounded-lg border border-gray-100 dark:border-gray-850 px-3 py-1.5"
					placeholder="https://chub.ai/characters/…"
					bind:value={link}
				/>
				<button class={buttonClass} type="submit" disabled={!link.trim() || importingKey !== null}>
					{#if importingKey === 'link'}<Spinner className="size-3" />{/if}
					{$i18n.t('Import')}
				</button>
			</div>
			<div class="text-[0.6875rem] text-gray-400 mt-1">
				{$i18n.t(
					'A character page on Chub, RisuRealm or Character Tavern, or a Poe bot (poe.com/BotName).'
				)}
			</div>
		</form>
	</div>

	<div class="mt-6 flex flex-wrap items-center gap-1 border-b border-gray-100 dark:border-gray-850">
		{#each sources as item (item.id)}
			<button
				type="button"
				class="px-3 py-1.5 text-sm -mb-px border-b-2 {source === item.id
					? 'border-gray-900 dark:border-gray-100 font-medium'
					: 'border-transparent text-gray-500 hover:text-gray-900 dark:hover:text-gray-100'}"
				on:click={() => selectSource(item.id)}
			>
				{item.name}
			</button>
		{/each}
	</div>

	<form class="mt-3 flex flex-wrap items-center gap-3" on:submit|preventDefault={() => search(1)}>
		<input
			class="{inputClass} flex-1 min-w-48 rounded-lg border border-gray-100 dark:border-gray-850 px-3 py-1.5"
			placeholder={source === 'poe'
				? $i18n.t('Paste Poe bot links or names (several at once is fine)')
				: $i18n.t('Search characters')}
			aria-label={$i18n.t('Search characters')}
			bind:value={query}
		/>
		{#if source === 'poe'}
			<a
				class="text-xs text-gray-500 hover:underline"
				href={`https://poe.com/search?q=${encodeURIComponent(query.trim())}`}
				target="_blank"
				rel="noopener noreferrer"
			>
				{$i18n.t('Search on poe.com')} ↗
			</a>
		{:else}
			<select
				class="bg-transparent text-xs pr-6 outline-hidden"
				aria-label={$i18n.t('Sort')}
				bind:value={sort}
				on:change={() => search(1)}
			>
				{#each SORTS as option (option.value)}
					<option value={option.value}>{$i18n.t(option.label)}</option>
				{/each}
			</select>
			<label class="flex items-center gap-2 text-xs">
				<Switch
					bind:state={nsfw}
					ariaLabel={$i18n.t('Include adult cards')}
					on:change={(event) => {
						nsfw = event.detail;
						search(1);
					}}
				/>
				{$i18n.t('Include adult cards')}
			</label>
		{/if}
		<button class={buttonClass} type="submit">
			{#if loading}<Spinner className="size-3" />{/if}
			{$i18n.t('Search')}
		</button>
	</form>

	{#if supportsTags}
		<div class="mt-2 flex flex-wrap items-center gap-1.5 text-xs">
			{#each activeTags.include as tag (tag)}
				<span
					class="flex items-center gap-1 pl-2 pr-1 py-0.5 rounded-full bg-gray-100 dark:bg-gray-850"
				>
					{tag}
					<button
						type="button"
						class="rounded-full hover:bg-gray-200 dark:hover:bg-gray-800"
						aria-label={$i18n.t('Remove tag {{TAG}}', { TAG: tag })}
						on:click={() => removeTag(tag)}
					>
						<XMark className="size-3" />
					</button>
				</span>
			{/each}
			{#each activeTags.exclude as tag (tag)}
				<span
					class="flex items-center gap-1 pl-2 pr-1 py-0.5 rounded-full bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-300 line-through decoration-1"
					title={$i18n.t('Excluded')}
				>
					{tag}
					<button
						type="button"
						class="rounded-full hover:bg-red-100 dark:hover:bg-red-900"
						aria-label={$i18n.t('Remove tag {{TAG}}', { TAG: tag })}
						on:click={() => removeTag(tag)}
					>
						<XMark className="size-3" />
					</button>
				</span>
			{/each}
			<input
				class="bg-transparent outline-hidden placeholder:text-gray-400 min-w-56 flex-1"
				placeholder={$i18n.t('Filter by tag (Enter to add; start with - to exclude)')}
				aria-label={$i18n.t('Filter by tag')}
				bind:value={tagInput}
				on:keydown={(event) => {
					if (event.key === 'Enter' || event.key === ',') {
						event.preventDefault();
						addTag(tagInput);
						tagInput = '';
					} else if (event.key === 'Backspace' && !tagInput) {
						const last = activeTags.exclude.at(-1) ?? activeTags.include.at(-1);
						if (last) removeTag(last);
					}
				}}
			/>
		</div>
	{/if}

	{#if error}
		<div
			class="mt-4 rounded-xl border border-red-200 dark:border-red-900 px-3 py-2 text-xs text-red-600"
		>
			{error}
		</div>
	{/if}

	{#if loading && !items.length}
		<div class="mt-10 flex justify-center"><Spinner className="size-5" /></div>
	{:else if !items.length && !error && source === 'poe'}
		<div class="mt-8 mx-auto max-w-xl text-center text-sm text-gray-500 space-y-2">
			<p>
				{$i18n.t(
					'Poe only offers search to signed-in users in its own app, so find bots on poe.com, then paste their links or names above to read and import them here.'
				)}
			</p>
			<p class="text-xs">
				{$i18n.t(
					'Bots with a public prompt import in full; for the rest, the import uses their description and greeting.'
				)}
			</p>
		</div>
	{:else if !items.length && !error}
		<div class="mt-10 text-center text-sm text-gray-500">{$i18n.t('No cards found')}</div>
	{/if}

	<div
		class="mt-4 grid gap-3 grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 {loading ? 'opacity-60' : ''}"
	>
		{#each items as item (keyOf(item))}
			{@const key = keyOf(item)}
			<div class="rounded-xl border border-gray-100 dark:border-gray-850 p-3 flex gap-3 min-w-0">
				<button
					type="button"
					class="shrink-0 w-20 h-28 rounded-lg overflow-hidden bg-gray-100 dark:bg-gray-850 flex items-center justify-center text-lg text-gray-400 hover:opacity-90"
					aria-label={$i18n.t('Read {{NAME}}', { NAME: item.name })}
					on:click={() => openPreview(item)}
				>
					{#if item.image_url}
						<img
							src={item.image_url}
							alt=""
							loading="lazy"
							referrerpolicy="no-referrer"
							class="w-full h-full object-cover"
							on:error={(event) => imageFailed(event, item)}
						/>
					{:else}
						{item.name.charAt(0).toUpperCase()}
					{/if}
				</button>

				<div class="flex-1 min-w-0 flex flex-col">
					<div class="flex items-start gap-1.5">
						<div class="flex-1 min-w-0">
							<button
								type="button"
								class="block max-w-full text-left text-sm font-medium truncate hover:underline"
								title={item.name}
								on:click={() => openPreview(item)}
							>
								{item.name}
							</button>
							{#if item.creator}
								<div class="text-xs text-gray-400 truncate">
									{$i18n.t('by {{CREATOR}}', { CREATOR: item.creator })}
								</div>
							{/if}
						</div>
						{#if item.nsfw}
							<span
								class="shrink-0 text-[0.625rem] px-1.5 py-0.5 rounded bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300"
							>
								18+
							</span>
						{/if}
					</div>

					{#if item.tagline}
						<button
							type="button"
							class="mt-1 text-left text-xs text-gray-600 dark:text-gray-400 whitespace-pre-line line-clamp-3 hover:text-gray-900 dark:hover:text-gray-200"
							on:click={() => openPreview(item)}
						>
							{cardPlainText(item.tagline)}
						</button>
					{/if}

					<div class="mt-1.5 flex flex-wrap gap-1 text-[0.625rem] text-gray-500">
						{#if item.has_lorebook}
							<span class="px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-850">
								{$i18n.t('Lorebook')}
							</span>
						{/if}
						{#if item.tokens}
							<span class="px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-850">
								{$i18n.t('{{COUNT}} tokens', { COUNT: item.tokens.toLocaleString() })}
							</span>
						{/if}
						{#if item.downloads}
							<span class="px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-850">
								{item.source === 'chub' ? '★' : '↓'}
								{item.downloads}
							</span>
						{/if}
						{#each tagsFirst(item.tags, activeTags.include).slice(0, 5) as tag}
							{#if resultTagsClickable}
								<button
									type="button"
									class="px-1.5 py-0.5 rounded bg-gray-50 dark:bg-gray-900 hover:bg-gray-200 dark:hover:bg-gray-800"
									title={$i18n.t('Filter by this tag')}
									on:click={() => addTag(tag)}>{tag}</button
								>
							{:else}
								<span class="px-1.5 py-0.5 rounded bg-gray-50 dark:bg-gray-900">{tag}</span>
							{/if}
						{/each}
					</div>

					<div class="mt-auto pt-2 flex items-center gap-2">
						{#if imported[key]}
							<a class={buttonClass} href={`/?models=${encodeURIComponent(imported[key])}`}>
								{$i18n.t('Chat')}
							</a>
							<span class="text-xs text-green-600">{$i18n.t('Imported')}</span>
						{:else}
							<button
								type="button"
								class={buttonClass}
								disabled={importingKey !== null}
								on:click={() => importItem(item)}
							>
								{#if importingKey === key}<Spinner className="size-3" />{/if}
								{$i18n.t('Import')}
							</button>
						{/if}
						<button
							type="button"
							class="text-xs text-gray-500 hover:underline"
							on:click={() => openPreview(item)}
						>
							{$i18n.t('Details')}
						</button>
						<a
							class="text-xs text-gray-500 hover:underline ml-auto"
							href={item.page_url}
							target="_blank"
							rel="noopener noreferrer"
						>
							{$i18n.t('View on site')}
						</a>
					</div>
				</div>
			</div>
		{/each}
	</div>

	{#if items.length && (page > 1 || hasMore)}
		<div class="mt-5 flex items-center justify-center gap-3 text-xs">
			<button
				type="button"
				class={buttonClass}
				disabled={loading || page <= 1}
				on:click={() => search(page - 1)}
			>
				{$i18n.t('Previous')}
			</button>
			<span class="text-gray-500">{$i18n.t('Page {{PAGE}}', { PAGE: page })}</span>
			<button
				type="button"
				class={buttonClass}
				disabled={loading || !hasMore}
				on:click={() => search(page + 1)}
			>
				{$i18n.t('Next')}
			</button>
		</div>
	{/if}
</div>

<Modal size="lg" bind:show={showPreview}>
	{#if previewItem}
		{@const key = keyOf(previewItem)}
		<div class="px-5 pt-4 pb-5 dark:text-gray-200 max-h-[85vh] flex flex-col">
			<div class="flex items-start gap-4">
				{#if previewItem.image_url}
					<img
						src={previewItem.image_url}
						alt=""
						referrerpolicy="no-referrer"
						class="shrink-0 w-24 h-32 rounded-xl object-cover bg-gray-100 dark:bg-gray-850"
						on:error={(event) => previewItem && imageFailed(event, previewItem)}
					/>
				{/if}
				<div class="flex-1 min-w-0">
					<div class="flex items-start justify-between gap-2">
						<div class="min-w-0">
							<div class="text-lg font-medium">{preview?.name || previewItem.name}</div>
							{#if preview?.creator || previewItem.creator}
								<div class="text-xs text-gray-400">
									{$i18n.t('by {{CREATOR}}', { CREATOR: preview?.creator || previewItem.creator })}
								</div>
							{/if}
						</div>
						<button
							type="button"
							class="p-1 rounded-lg text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-850"
							aria-label={$i18n.t('Close')}
							on:click={() => (showPreview = false)}
						>
							<XMark className="size-4" />
						</button>
					</div>
					<div class="mt-2 flex flex-wrap gap-1 text-[0.625rem] text-gray-500">
						{#if previewItem.nsfw}
							<span
								class="px-1.5 py-0.5 rounded bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300"
								>18+</span
							>
						{/if}
						{#if preview?.lorebook_entries}
							<span class="px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-850">
								{$i18n.t('Lorebook: {{COUNT}} entries', { COUNT: preview.lorebook_entries })}
							</span>
						{/if}
						{#if preview?.expressions?.length}
							<span class="px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-850">
								{$i18n.t('{{COUNT}} expressions', { COUNT: preview.expressions.length })}
							</span>
						{/if}
						{#if greetings.length > 1}
							<span class="px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-850">
								{$i18n.t('{{COUNT}} greetings', { COUNT: greetings.length })}
							</span>
						{/if}
						{#each (preview?.tags?.length ? preview.tags : previewItem.tags).slice(0, 12) as tag}
							{#if supportsTags && previewItem.source === source}
								<button
									type="button"
									class="px-1.5 py-0.5 rounded bg-gray-50 dark:bg-gray-900 hover:bg-gray-200 dark:hover:bg-gray-800"
									title={$i18n.t('Filter by this tag')}
									on:click={() => {
										showPreview = false;
										addTag(tag);
									}}>{tag}</button
								>
							{:else}
								<span class="px-1.5 py-0.5 rounded bg-gray-50 dark:bg-gray-900">{tag}</span>
							{/if}
						{/each}
					</div>
					<div class="mt-3 flex items-center gap-3">
						{#if imported[key]}
							<a class={buttonClass} href={`/?models=${encodeURIComponent(imported[key])}`}>
								{$i18n.t('Chat')}
							</a>
							<span class="text-xs text-green-600">{$i18n.t('Imported')}</span>
						{:else}
							<button
								type="button"
								class={buttonClass}
								disabled={importingKey !== null}
								on:click={() => previewItem && importItem(previewItem)}
							>
								{#if importingKey === key}<Spinner className="size-3" />{/if}
								{$i18n.t('Import')}
							</button>
						{/if}
						<a
							class="text-xs text-gray-500 hover:underline"
							href={previewItem.page_url}
							target="_blank"
							rel="noopener noreferrer"
						>
							{$i18n.t('View on site')}
						</a>
					</div>
				</div>
			</div>

			<div class="mt-4 flex-1 min-h-0 overflow-y-auto pr-1 space-y-4 text-sm">
				{#if previewError}
					<div
						class="rounded-xl border border-red-200 dark:border-red-900 px-3 py-2 text-xs text-red-600"
					>
						{previewError}
					</div>
				{:else if !preview}
					<div class="flex items-center gap-2 text-xs text-gray-500">
						<Spinner className="size-4" />
						{$i18n.t('Loading the card…')}
					</div>
				{:else}
					{#if preview.creator_notes?.trim()}
						<section>
							<div class="text-xs font-medium text-gray-500 mb-1">{$i18n.t('Creator Notes')}</div>
							<CardText text={preview.creator_notes} />
						</section>
					{/if}
					{#if greetings.length}
						<section>
							<div class="flex items-center justify-between text-xs font-medium text-gray-500 mb-1">
								<span>{$i18n.t('First Message')}</span>
								{#if greetings.length > 1}
									<span class="flex items-center gap-2">
										<button
											type="button"
											class="px-1.5 rounded hover:bg-gray-100 dark:hover:bg-gray-850 disabled:opacity-40"
											aria-label={$i18n.t('Previous greeting')}
											disabled={greetingIndex === 0}
											on:click={() => (greetingIndex -= 1)}>‹</button
										>
										{greetingIndex + 1}/{greetings.length}
										<button
											type="button"
											class="px-1.5 rounded hover:bg-gray-100 dark:hover:bg-gray-850 disabled:opacity-40"
											aria-label={$i18n.t('Next greeting')}
											disabled={greetingIndex >= greetings.length - 1}
											on:click={() => (greetingIndex += 1)}>›</button
										>
									</span>
								{/if}
							</div>
							<div class="rounded-xl bg-gray-50 dark:bg-gray-850 px-3 py-2">
								<CardText text={expand(greetings[Math.min(greetingIndex, greetings.length - 1)])} />
							</div>
						</section>
					{/if}
					{#each previewSections as section (section.label)}
						<section>
							<div class="text-xs font-medium text-gray-500 mb-1">{section.label}</div>
							{#if section.rendered}
								<CardText text={expand(section.text)} />
							{:else}
								<div class="whitespace-pre-wrap break-words text-gray-700 dark:text-gray-300">
									{expand(section.text)}
								</div>
							{/if}
						</section>
					{/each}
					{#if !greetings.length && !previewSections.length}
						<div class="text-xs text-gray-500">{$i18n.t('This card has no character text.')}</div>
					{/if}
				{/if}
			</div>
		</div>
	{/if}
</Modal>
