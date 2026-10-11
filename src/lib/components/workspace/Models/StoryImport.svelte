<script lang="ts">
	import { getContext } from 'svelte';
	import { goto } from '$app/navigation';
	import { toast } from 'svelte-sonner';

	import { config, models, settings } from '$lib/stores';
	import { getModels } from '$lib/apis';
	import {
		analyzeStory,
		createCharactersFromStory,
		draftStoryGreeting,
		writeStoryProfile,
		type StoryCharacterDraft,
		type StoryDraft
	} from '$lib/apis/models';

	import Spinner from '$lib/components/common/Spinner.svelte';
	import Switch from '$lib/components/common/Switch.svelte';
	import Textarea from '$lib/components/common/Textarea.svelte';
	import ChevronLeft from '$lib/components/icons/ChevronLeft.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';
	import ModelSelector from '$lib/components/chat/ModelSelector/Selector.svelte';

	const i18n: any = getContext('i18n');

	let step: 'input' | 'review' = 'input';
	let title = '';
	let text = '';
	let baseModelId: string | null = null;
	let analysisModelId: string | null = null;
	let detailed = true;
	let profileFor: number | null = null;
	let fileInput: HTMLInputElement;
	let analyzing = false;
	let creating = false;
	let isPublic = false;

	let draft: StoryDraft | null = null;
	let characters: StoryCharacterDraft[] = [];
	let entries: Record<string, any>[] = [];
	let lorebookName = '';
	let events: string[] = [];
	let startIndex = -1;
	let playAs = '';
	let scenario = '';
	let greetingFor: number | null = null;

	$: baseModelItems = (($models ?? []) as any[])
		.filter((model) => !model?.preset && model?.owned_by !== 'arena' && !(model?.direct ?? false))
		.map((model) => ({ value: model.id, label: model.name, model }));
	$: selectedCount = characters.filter((character) => character.selected).length;

	const loadFiles = async (files: FileList | null) => {
		const file = files?.[0];
		if (!file) return;
		text = await file.text();
		if (!title) title = file.name.replace(/\.[^.]+$/, '');
		fileInput.value = '';
	};

	const composeScenario = () => {
		const parts: string[] = [];
		if (startIndex >= 0) {
			const earlier = events.slice(Math.max(0, startIndex - 3), startIndex);
			if (earlier.length) parts.push(`${$i18n.t('Earlier')}: ${earlier.join(' ')}`);
			parts.push(`${$i18n.t('Now')}: ${events[startIndex]}`);
		}
		parts.push(
			playAs
				? `{{user}} is ${playAs}.`
				: $i18n.t('{{USER}} is a newcomer to this story.', { USER: '{{user}}' })
		);
		scenario = parts.join('\n');
	};

	const analyze = async () => {
		if (!baseModelId) {
			toast.error($i18n.t('Please select a base model'));
			return;
		}
		analyzing = true;
		try {
			draft = await analyzeStory(localStorage.token, {
				text,
				title: title.trim(),
				base_model_id: baseModelId,
				analysis_model_id: analysisModelId || null,
				detailed
			});
			characters = draft.characters;
			entries = draft.lorebook.entries;
			lorebookName = draft.lorebook.name;
			events = draft.events;
			startIndex = events.length ? 0 : -1;
			playAs = '';
			composeScenario();
			step = 'review';
			if (draft.failed_chunks) {
				toast.warning(
					$i18n.t('{{FAILED}} of {{TOTAL}} parts could not be read; the draft may be incomplete.', {
						FAILED: draft.failed_chunks,
						TOTAL: draft.chunks
					})
				);
			}
		} catch (error: any) {
			toast.error(`${error?.detail ?? error}`);
		} finally {
			analyzing = false;
		}
	};

	const setPlayAs = (name: string) => {
		playAs = name;
		// The character the user plays is not created as a model.
		characters = characters.map((character) =>
			character.name === name ? { ...character, selected: false } : character
		);
		composeScenario();
	};

	const draftGreeting = async (index: number) => {
		if (!baseModelId) return;
		greetingFor = index;
		try {
			const { first_mes } = await draftStoryGreeting(localStorage.token, {
				base_model_id: baseModelId,
				character: characters[index],
				scenario
			});
			characters[index].first_mes = first_mes;
		} catch (error: any) {
			toast.error(`${error?.detail ?? error}`);
		} finally {
			greetingFor = null;
		}
	};

	const writeProfile = async (index: number) => {
		if (!baseModelId) return;
		profileFor = index;
		try {
			const profile = await writeStoryProfile(localStorage.token, {
				text,
				title: title.trim(),
				base_model_id: baseModelId,
				analysis_model_id: analysisModelId || null,
				character: characters[index]
			});
			characters[index] = { ...characters[index], ...profile, detailed: true };
		} catch (error: any) {
			toast.error(`${error?.detail ?? error}`);
		} finally {
			profileFor = null;
		}
	};

	const create = async () => {
		const chosen = characters.filter((character) => character.selected);
		if (!chosen.length || !baseModelId) return;
		creating = true;
		try {
			const created = await createCharactersFromStory(localStorage.token, {
				title: title.trim(),
				base_model_id: baseModelId,
				public: isPublic,
				scenario,
				characters: chosen.map(
					({ name, description, personality, first_mes, mes_example, creator_notes }) => ({
						name,
						description,
						personality,
						first_mes,
						mes_example,
						creator_notes
					})
				),
				lorebook: entries.length ? { name: lorebookName, entries } : null
			});
			toast.success($i18n.t('Created {{COUNT}} character(s)', { COUNT: created.length }));
			await models.set(
				await getModels(
					localStorage.token,
					($config?.features?.enable_direct_connections && $settings?.directConnections) || null
				)
			);
			goto('/workspace/models');
		} catch (error: any) {
			toast.error(`${error?.detail ?? error}`);
		} finally {
			creating = false;
		}
	};

	const labelClass = 'mb-1 text-xs text-gray-500';
	const textareaClass =
		'min-h-12 w-full resize-none overflow-y-hidden bg-transparent py-1 text-[0.8125rem] outline-hidden placeholder:text-gray-300 dark:placeholder:text-gray-700';
	const buttonClass =
		'px-3 py-1 text-xs rounded-lg bg-gray-100 dark:bg-gray-850 hover:bg-gray-200 dark:hover:bg-gray-800 flex items-center gap-1.5 disabled:opacity-50';
	const primaryClass =
		'px-4 py-1.5 text-sm bg-black hover:bg-gray-900 text-white dark:bg-white dark:text-black dark:hover:bg-gray-100 transition rounded-full flex items-center gap-2 disabled:opacity-50';
</script>

<div class="max-w-3xl mx-auto w-full pb-12">
	<button
		type="button"
		class="flex items-center gap-1 text-xs text-gray-500 hover:text-gray-900 dark:hover:text-gray-100 w-fit mb-3"
		on:click={() => (step === 'review' ? (step = 'input') : goto('/workspace/models'))}
	>
		<ChevronLeft className="size-3" />
		{step === 'review' ? $i18n.t('Back to the story') : $i18n.t('Back')}
	</button>

	<div class="text-xl font-medium">{$i18n.t('Create Characters from a Story')}</div>
	<div class="text-sm text-gray-500 mt-1">
		{$i18n.t(
			'A model reads the story and drafts its characters, a lorebook of its world, and a timeline to start from. You review everything before anything is created.'
		)}
	</div>

	{#if step === 'input'}
		<div class="mt-5 space-y-4">
			<div>
				<div class={labelClass}>{$i18n.t('Title')}</div>
				<input
					class="w-full bg-transparent text-sm outline-hidden placeholder:text-gray-300 dark:placeholder:text-gray-700"
					placeholder={$i18n.t('Optional')}
					aria-label={$i18n.t('Story title')}
					bind:value={title}
				/>
			</div>

			<div>
				<div class="flex items-center justify-between">
					<div class={labelClass}>{$i18n.t('Story')}</div>
					<button
						type="button"
						class="text-xs text-gray-500 hover:underline"
						on:click={() => fileInput.click()}
					>
						{$i18n.t('Load a .txt or .md file')}
					</button>
					<input
						bind:this={fileInput}
						type="file"
						accept=".txt,.md,.markdown,text/plain"
						hidden
						on:change={(event) => loadFiles(event.currentTarget.files)}
					/>
				</div>
				<textarea
					class="w-full h-72 rounded-xl border border-gray-100 dark:border-gray-850 bg-transparent p-3 text-sm outline-hidden resize-y placeholder:text-gray-300 dark:placeholder:text-gray-700"
					placeholder={$i18n.t('Paste the story here')}
					aria-label={$i18n.t('Story text')}
					bind:value={text}
				></textarea>
				<div class="text-xs text-gray-400 text-right">
					{$i18n.t('{{COUNT}} characters', { COUNT: text.length.toLocaleString() })}
				</div>
			</div>

			<div>
				<div class={labelClass}>{$i18n.t('Base Model (From)')}</div>
				<ModelSelector
					id="story-base-model"
					placeholder={$i18n.t('Select a base model (e.g. llama3, gpt-4o)')}
					searchPlaceholder={$i18n.t('Search a model')}
					items={baseModelItems}
					triggerClassName="text-xs"
					selectionOnly
					includeHidden
					bind:value={baseModelId}
				/>
				<div class="text-[0.6875rem] text-gray-400 mt-1">
					{$i18n.t('The characters will run on this model.')}
				</div>
			</div>

			<div>
				<div class={labelClass}>{$i18n.t('Analysis Model')}</div>
				<ModelSelector
					id="story-analysis-model"
					placeholder={$i18n.t('Task model (default)')}
					searchPlaceholder={$i18n.t('Search a model')}
					items={baseModelItems}
					triggerClassName="text-xs"
					selectionOnly
					includeHidden
					bind:value={analysisModelId}
				/>
				<div class="text-[0.6875rem] text-gray-400 mt-1">
					{$i18n.t(
						'Optional. Small task models give thin characters; a larger model reads the story more closely.'
					)}
				</div>
			</div>

			<label class="flex items-start gap-2 text-xs">
				<Switch bind:state={detailed} ariaLabel={$i18n.t('Write detailed profiles')} />
				<span>
					{$i18n.t('Write detailed profiles for the main characters')}
					<span class="block text-[0.6875rem] text-gray-400">
						{$i18n.t(
							'Rereads every scene each main character is in for appearance, history, relationships, voice and example dialogue. Takes longer.'
						)}
					</span>
				</span>
			</label>

			<div class="flex items-center justify-end gap-3">
				{#if analyzing}
					<span class="text-xs text-gray-500">
						{$i18n.t('Reading the story… long stories take a few minutes.')}
					</span>
				{/if}
				<button
					class={primaryClass}
					disabled={analyzing || !text.trim() || !baseModelId}
					on:click={analyze}
				>
					{#if analyzing}<Spinner className="size-3.5" />{/if}
					{$i18n.t('Analyze Story')}
				</button>
			</div>
		</div>
	{:else if draft}
		<div class="mt-4 text-xs text-gray-500">
			{$i18n.t('{{CHARACTERS}} characters, {{ENTRIES}} world entries, {{EVENTS}} events', {
				CHARACTERS: characters.length,
				ENTRIES: entries.length,
				EVENTS: events.length
			})}
		</div>

		<section class="mt-5">
			<div class="text-sm font-medium">{$i18n.t('Where the roleplay starts')}</div>
			<div class="mt-2 flex flex-wrap items-center gap-2 text-xs text-gray-500">
				<label for="story-play-as">{$i18n.t('You play')}</label>
				<select
					id="story-play-as"
					class="bg-transparent pr-6 outline-hidden text-gray-700 dark:text-gray-300"
					value={playAs}
					on:change={(event) => setPlayAs(event.currentTarget.value)}
				>
					<option value="">{$i18n.t('Yourself, as a newcomer')}</option>
					{#each characters as character}
						<option value={character.name}>{character.name}</option>
					{/each}
				</select>
			</div>

			{#if events.length}
				<div
					class="mt-2 max-h-56 overflow-y-auto rounded-xl border border-gray-100 dark:border-gray-850 p-2"
				>
					{#each events as event, index}
						<label class="flex items-start gap-2 py-1 text-xs cursor-pointer">
							<input
								type="radio"
								name="story-start"
								class="mt-0.5"
								checked={startIndex === index}
								on:change={() => {
									startIndex = index;
									composeScenario();
								}}
							/>
							<span
								class={startIndex === index ? 'font-medium' : 'text-gray-600 dark:text-gray-400'}
							>
								{index + 1}. {event}
							</span>
						</label>
					{/each}
				</div>
			{/if}

			<div class="mt-2">
				<div class={labelClass}>{$i18n.t('Scenario (shared by every character)')}</div>
				<Textarea
					className={textareaClass}
					ariaLabel={$i18n.t('Scenario')}
					rows={3}
					minSize={64}
					bind:value={scenario}
				/>
			</div>
		</section>

		<section class="mt-6">
			<div class="text-sm font-medium">
				{$i18n.t('Characters')}
				<span class="text-gray-400 font-normal">{selectedCount}/{characters.length}</span>
			</div>
			<div class="mt-2 flex flex-col gap-2">
				{#each characters as character, index}
					<div
						class="rounded-xl border border-gray-100 dark:border-gray-850 p-3 {character.selected
							? ''
							: 'opacity-60'}"
					>
						<div class="flex items-center gap-2">
							<input
								type="checkbox"
								aria-label={$i18n.t('Create {{NAME}}', { NAME: character.name })}
								bind:checked={character.selected}
								disabled={character.name === playAs}
							/>
							<input
								class="flex-1 bg-transparent text-sm font-medium outline-hidden"
								aria-label={$i18n.t('Character Name')}
								bind:value={character.name}
							/>
							{#if character.detailed}
								<span
									class="text-[0.625rem] px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-850 text-gray-500"
								>
									{$i18n.t('Detailed')}
								</span>
							{/if}
							<span class="text-xs text-gray-400">
								{character.name === playAs
									? $i18n.t('Played by you')
									: $i18n.t('In {{COUNT}} part(s)', { COUNT: character.mentions })}
							</span>
							{#if character.selected}
								<button
									type="button"
									class={buttonClass}
									disabled={profileFor !== null}
									title={$i18n.t(
										'Reread every scene this character is in and write a fuller sheet'
									)}
									on:click={() => writeProfile(index)}
								>
									{#if profileFor === index}<Spinner className="size-3" />{/if}
									{character.detailed
										? $i18n.t('Rewrite profile')
										: $i18n.t('Write detailed profile')}
								</button>
							{/if}
						</div>

						{#if character.selected}
							<div class="mt-2 space-y-2">
								<div>
									<div class={labelClass}>{$i18n.t('Description')}</div>
									<Textarea
										className={textareaClass}
										rows={2}
										minSize={48}
										bind:value={character.description}
									/>
								</div>
								<div>
									<div class={labelClass}>{$i18n.t('Personality')}</div>
									<Textarea
										className={textareaClass}
										rows={2}
										minSize={48}
										bind:value={character.personality}
									/>
								</div>
								<div>
									<div class={labelClass}>{$i18n.t('Example Dialogue')}</div>
									<Textarea
										className={textareaClass}
										rows={2}
										minSize={48}
										bind:value={character.mes_example}
									/>
								</div>
								<div>
									<div class="flex items-center justify-between">
										<div class={labelClass}>{$i18n.t('First Message')}</div>
										<button
											type="button"
											class={buttonClass}
											disabled={greetingFor !== null}
											on:click={() => draftGreeting(index)}
										>
											{#if greetingFor === index}<Spinner className="size-3" />{/if}
											{$i18n.t('Draft from scenario')}
										</button>
									</div>
									<Textarea
										className={textareaClass}
										placeholder={$i18n.t('Optional; the chat opens with this message')}
										rows={2}
										minSize={48}
										bind:value={character.first_mes}
									/>
								</div>
							</div>
						{/if}
					</div>
				{/each}
			</div>
		</section>

		<section class="mt-6">
			<div class="flex items-center justify-between">
				<div class="text-sm font-medium">
					{$i18n.t('World lorebook')}
					<span class="text-gray-400 font-normal">{entries.length}</span>
				</div>
				<input
					class="bg-transparent text-xs text-right outline-hidden text-gray-500"
					aria-label={$i18n.t('Lorebook Name')}
					bind:value={lorebookName}
				/>
			</div>
			<div class="mt-2 flex flex-col gap-1">
				{#each entries as entry, index (index)}
					<details class="rounded-xl border border-gray-100 dark:border-gray-850 px-3 py-2 text-xs">
						<summary class="flex items-center gap-2 cursor-pointer">
							<span class="font-medium">{entry.name}</span>
							<span class="text-gray-400">{entry.comment}</span>
							<span class="flex-1 truncate text-gray-400">{(entry.keys ?? []).join(', ')}</span>
							<button
								type="button"
								class="p-0.5 rounded text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800"
								aria-label={$i18n.t('Remove')}
								on:click|preventDefault={() => (entries = entries.filter((_, i) => i !== index))}
							>
								<XMark className="size-3" />
							</button>
						</summary>
						<Textarea className="{textareaClass} mt-1" rows={2} bind:value={entry.content} />
					</details>
				{/each}
			</div>
		</section>

		<div class="mt-6 flex items-center justify-between">
			<div class="flex items-center gap-2 text-xs">
				<Switch bind:state={isPublic} ariaLabel={$i18n.t('Visible to all users')} />
				<span>{$i18n.t('Visible to all users')}</span>
			</div>
			<button class={primaryClass} disabled={creating || !selectedCount} on:click={create}>
				{#if creating}<Spinner className="size-3.5" />{/if}
				{$i18n.t('Create {{COUNT}} character(s)', { COUNT: selectedCount })}
			</button>
		</div>
	{/if}
</div>
