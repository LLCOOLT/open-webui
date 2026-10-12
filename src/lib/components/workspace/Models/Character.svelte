<script lang="ts">
	import { getContext } from 'svelte';
	import { toast } from 'svelte-sonner';

	import {
		characterExpressionUrl,
		convertSystemPromptToCharacter,
		deleteCharacterExpression,
		generateCharacterExpression,
		uploadCharacterExpressions
	} from '$lib/apis/models';
	import { config, user } from '$lib/stores';
	import Spinner from '$lib/components/common/Spinner.svelte';

	import Switch from '$lib/components/common/Switch.svelte';
	import Textarea from '$lib/components/common/Textarea.svelte';
	import Plus from '$lib/components/icons/Plus.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';
	import LorebookPicker from '$lib/components/workspace/Lorebooks/LorebookPicker.svelte';

	const i18n: any = getContext('i18n');

	// meta.character of the model being edited; null when the model is not a character.
	export let character: any = null;
	export let modelName = '';
	// meta.lorebook_ids of the model being edited.
	export let lorebookIds: string[] | null | undefined = [];
	// The model's existing system prompt, offered for conversion when it was not a character yet.
	export let sourcePrompt = '';
	export let baseModelId: string | null = null;
	export let convertible = false;
	// Set when editing a saved model: expression images are stored per model.
	export let modelId: string | null = null;

	let expressionInput: HTMLInputElement;
	let expressionsBusy = false;
	// Bumped after uploads so replaced images are not served from the browser cache.
	let expressionVersion = 0;
	$: expressionNames = Object.keys(character?.expressions ?? {}).sort();

	// The server keeps the image files; this copy only lists the names for display.
	const setExpressionNames = (names: string[]) => {
		const current = character.expressions ?? {};
		character.expressions = Object.fromEntries(names.map((name) => [name, current[name] ?? 'new']));
		expressionVersion += 1;
	};

	const uploadExpressions = async (files: FileList | null) => {
		if (!modelId || !files?.length) return;
		expressionsBusy = true;
		try {
			const result = await uploadCharacterExpressions(
				localStorage.token,
				modelId,
				Array.from(files)
			);
			setExpressionNames(result.expressions);
			if (result.skipped.length) {
				toast.warning(
					$i18n.t('Skipped (not an image or no usable name): {{FILES}}', {
						FILES: result.skipped.slice(0, 5).join(', ')
					})
				);
			}
		} catch (error: any) {
			toast.error(`${error?.detail ?? error}`);
		} finally {
			expressionsBusy = false;
			expressionInput.value = '';
		}
	};

	const removeExpression = async (name: string) => {
		if (!modelId) return;
		try {
			setExpressionNames(
				(await deleteCharacterExpression(localStorage.token, modelId, name)).expressions
			);
		} catch (error: any) {
			toast.error(`${error?.detail ?? error}`);
		}
	};

	let converting: 'parser' | 'ai' | null = null;

	$: fieldLabels = {
		description: $i18n.t('Description'),
		personality: $i18n.t('Personality'),
		scenario: $i18n.t('Scenario'),
		first_mes: $i18n.t('First Message'),
		mes_example: $i18n.t('Example Dialogue'),
		system_prompt: $i18n.t('Character System Prompt'),
		post_history_instructions: $i18n.t('Post-History Instructions'),
		creator_notes: $i18n.t('Creator Notes')
	} as Record<string, string>;

	const convert = async (useAi: boolean) => {
		converting = useAi ? 'ai' : 'parser';
		try {
			const result = await convertSystemPromptToCharacter(localStorage.token, {
				system: sourcePrompt,
				name: modelName,
				base_model_id: baseModelId,
				use_ai: useAi
			});
			const filled = Object.fromEntries(
				Object.entries(result.character).filter(([, value]) => value !== '' && value !== null)
			);
			character = { ...withDefaults(character ?? newCharacter()), ...filled };
			toast.success(
				result.detected.length
					? $i18n.t('Filled from the system prompt: {{FIELDS}}. Review before saving.', {
							FIELDS: result.detected.map((key) => fieldLabels[key] ?? key).join(', ')
						})
					: $i18n.t('No labelled sections found; the prompt went into the description.')
			);
		} catch (error: any) {
			toast.error(`${error?.detail ?? error}`);
		} finally {
			converting = null;
		}
	};

	const newCharacter = () => ({
		spec_version: 'owui',
		name: modelName,
		nickname: null,
		description: '',
		personality: '',
		scenario: '',
		first_mes: '',
		alternate_greetings: [],
		mes_example: '',
		system_prompt: '',
		post_history_instructions: '',
		prompt_overrides: { system_prompt: 'replace', post_history: 'replace' },
		depth_prompt: { text: '', depth: 4, role: 'system' },
		example_mode: 'system',
		reply_cleanup: defaultCleanup(),
		creator: '',
		creator_notes: ''
	});

	// Reply cleanup (see backend utils/roleplay/cleanup.py).
	function defaultCleanup() {
		return { stop_at_user: true, trim_incomplete: false, rules: [] };
	}

	const addRule = () => {
		character.reply_cleanup.rules = [
			...character.reply_cleanup.rules,
			{ find: '', replace: '', regex: false, enabled: true }
		];
	};

	const removeRule = (index: number) => {
		character.reply_cleanup.rules = character.reply_cleanup.rules.filter(
			(_: unknown, i: number) => i !== index
		);
	};

	// Generating expression images with the configured image generator.
	const DEFAULT_EXPRESSIONS = [
		'neutral',
		'joy',
		'sadness',
		'anger',
		'surprise',
		'fear',
		'embarrassment'
	];
	$: canGenerateImages =
		!!$config?.features?.enable_image_generation &&
		($user?.role === 'admin' || !!$user?.permissions?.features?.image_generation);
	let showGenerate = false;
	let generateNames = '';
	let generateStyle = '';
	let generating: { name: string; index: number; total: number } | null = null;

	const openGenerate = () => {
		const missing = DEFAULT_EXPRESSIONS.filter((name) => !expressionNames.includes(name));
		generateNames = (missing.length ? missing : DEFAULT_EXPRESSIONS).join(', ');
		showGenerate = true;
	};

	const generateExpressions = async () => {
		const names = [
			...new Set(
				generateNames
					.split(',')
					.map((name) => name.trim().toLowerCase())
					.filter(Boolean)
			)
		];
		if (!modelId || !names.length) return;
		let made = 0;
		for (const [index, name] of names.entries()) {
			generating = { name, index: index + 1, total: names.length };
			try {
				const result = await generateCharacterExpression(
					localStorage.token,
					modelId,
					name,
					generateStyle
				);
				setExpressionNames(result.expressions);
				made += 1;
			} catch (error: any) {
				toast.error(`${name}: ${error?.detail ?? error}`);
				if (made === 0) break; // likely not set up; do not repeat the same error for each name
			}
		}
		generating = null;
		if (made) {
			toast.success($i18n.t('Generated {{COUNT}} expression images', { COUNT: made }));
			showGenerate = false;
		}
	};

	let enabled = character !== null && character !== undefined;
	$: setEnabled(enabled);

	const setEnabled = (value: boolean) => {
		if (value && !character) {
			character = newCharacter();
		} else if (!value && character) {
			character = null;
		}
	};

	// Older or imported records may lack the Open WebUI settings; fill them in by reassignment
	// (mutating the bound prop in a reactive block miscompiles next to the bindings below).
	const withDefaults = (value: any) => ({
		...value,
		prompt_overrides: value.prompt_overrides ?? {
			system_prompt: 'replace',
			post_history: 'replace'
		},
		depth_prompt: value.depth_prompt ?? { text: '', depth: 4, role: 'system' },
		alternate_greetings: value.alternate_greetings ?? [],
		example_mode: value.example_mode ?? 'system',
		reply_cleanup: { ...defaultCleanup(), ...(value.reply_cleanup ?? {}) }
	});

	$: if (
		character &&
		(!character.prompt_overrides ||
			!character.depth_prompt ||
			!character.alternate_greetings ||
			!character.example_mode ||
			!character.reply_cleanup?.rules)
	) {
		character = withDefaults(character);
	}

	$: textFields = [
		{
			key: 'description',
			label: $i18n.t('Description'),
			placeholder: $i18n.t('Who the character is: appearance, background, traits')
		},
		{
			key: 'personality',
			label: $i18n.t('Personality'),
			placeholder: $i18n.t("A short summary of the character's personality")
		},
		{
			key: 'scenario',
			label: $i18n.t('Scenario'),
			placeholder: $i18n.t('The situation the chat starts in')
		}
	];

	$: promptFields = [
		{
			key: 'system_prompt',
			override: 'system_prompt',
			label: $i18n.t('Character System Prompt'),
			placeholder: $i18n.t('Empty uses the default prompt. {{MACRO}} inserts it.', {
				MACRO: '{{original}}'
			})
		},
		{
			key: 'post_history_instructions',
			override: 'post_history',
			label: $i18n.t('Post-History Instructions'),
			placeholder: $i18n.t('Sent after the latest user message')
		}
	];

	$: greetingIndexes = (character?.alternate_greetings ?? []).map((_: string, i: number) => i);

	const labelClass = 'mb-1 text-xs text-gray-500';
	const inputClass =
		'w-full rounded-lg bg-transparent text-[0.8125rem] outline-hidden placeholder:text-gray-300 dark:placeholder:text-gray-700';
	const textareaClass =
		'min-h-12 w-full resize-none overflow-y-hidden bg-transparent py-1 text-[0.8125rem] text-gray-700 outline-hidden placeholder:text-gray-300 dark:text-gray-300 dark:placeholder:text-gray-700';
	const selectClass =
		'rounded-lg bg-transparent pr-6 text-xs text-gray-600 dark:text-gray-400 outline-hidden cursor-pointer';
</script>

<section class="mt-2.5">
	<div class="flex items-center justify-between">
		<div>
			<div class="text-xs text-gray-400 dark:text-gray-600">{$i18n.t('Character')}</div>
			<div class="text-[0.6875rem] text-gray-400 dark:text-gray-600">
				{$i18n.t("Use character card fields to build this model's prompt.")}
			</div>
		</div>
		<Switch bind:state={enabled} ariaLabel={$i18n.t('This model is a character')} />
	</div>

	{#if enabled && character}
		{#if convertible && sourcePrompt.trim()}
			<div
				class="mt-2.5 flex flex-wrap items-center justify-between gap-2 rounded-xl bg-gray-50 dark:bg-gray-850 px-3 py-2 text-xs"
			>
				<div class="text-gray-600 dark:text-gray-400">
					{$i18n.t('Fill the fields below from this model’s current system prompt?')}
				</div>
				<div class="flex gap-1.5">
					<button
						type="button"
						class="px-2.5 py-1 rounded-lg bg-white dark:bg-gray-800 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center gap-1 disabled:opacity-50"
						disabled={converting !== null}
						on:click={() => convert(false)}
					>
						{#if converting === 'parser'}<Spinner className="size-3" />{/if}
						{$i18n.t('Convert')}
					</button>
					<button
						type="button"
						class="px-2.5 py-1 rounded-lg bg-white dark:bg-gray-800 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center gap-1 disabled:opacity-50"
						disabled={converting !== null || !baseModelId}
						title={baseModelId ? '' : $i18n.t('Choose a base model first.')}
						on:click={() => convert(true)}
					>
						{#if converting === 'ai'}<Spinner className="size-3" />{/if}
						{$i18n.t('Convert with AI')}
					</button>
				</div>
			</div>
		{/if}

		<div class="mt-2.5 space-y-3">
			<div class="flex gap-3">
				<div class="flex-1">
					<div class={labelClass}>{$i18n.t('Character Name')}</div>
					<input
						class={inputClass}
						placeholder={modelName}
						aria-label={$i18n.t('Character Name')}
						bind:value={character.name}
					/>
				</div>
				<div class="flex-1">
					<div class={labelClass}>{$i18n.t('Nickname')}</div>
					<input
						class={inputClass}
						placeholder={$i18n.t('Optional, used for {{MACRO}}', { MACRO: '{{char}}' })}
						aria-label={$i18n.t('Nickname')}
						value={character.nickname ?? ''}
						on:input={(event) => (character.nickname = event.currentTarget.value || null)}
					/>
				</div>
			</div>

			{#each textFields as field}
				<div>
					<div class={labelClass}>{field.label}</div>
					<Textarea
						className={textareaClass}
						placeholder={field.placeholder}
						ariaLabel={field.label}
						rows={2}
						minSize={48}
						bind:value={character[field.key]}
					/>
				</div>
			{/each}

			<div>
				<div class={labelClass}>{$i18n.t('First Message')}</div>
				<Textarea
					className={textareaClass}
					placeholder={$i18n.t("The character's opening message in a new chat")}
					ariaLabel={$i18n.t('First Message')}
					rows={2}
					minSize={48}
					bind:value={character.first_mes}
				/>
			</div>

			<div>
				<div class="flex items-center justify-between">
					<div class={labelClass}>{$i18n.t('Alternate Greetings')}</div>
					<button
						type="button"
						class="p-1 rounded-lg text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-850"
						aria-label={$i18n.t('Add Greeting')}
						on:click={() =>
							(character.alternate_greetings = [...character.alternate_greetings, ''])}
					>
						<Plus className="size-3" />
					</button>
				</div>
				{#each greetingIndexes as index}
					<div class="flex gap-2 items-start">
						<Textarea
							className={textareaClass}
							placeholder={$i18n.t('Greeting {{NUMBER}}', { NUMBER: index + 2 })}
							ariaLabel={$i18n.t('Greeting {{NUMBER}}', { NUMBER: index + 2 })}
							rows={1}
							bind:value={character.alternate_greetings[index]}
						/>
						<button
							type="button"
							class="mt-1 p-1 rounded-lg text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-850"
							aria-label={$i18n.t('Remove')}
							on:click={() =>
								(character.alternate_greetings = character.alternate_greetings.filter(
									(_: string, i: number) => i !== index
								))}
						>
							<XMark className="size-3" />
						</button>
					</div>
				{/each}
			</div>

			<div>
				<div class="flex items-center justify-between">
					<div class={labelClass}>{$i18n.t('Example Dialogue')}</div>
					<select
						class={selectClass}
						aria-label={$i18n.t('Example dialogue placement')}
						bind:value={character.example_mode}
					>
						<option value="system">{$i18n.t('In system prompt')}</option>
						<option value="messages">{$i18n.t('As example messages')}</option>
					</select>
				</div>
				<Textarea
					className={textareaClass}
					placeholder={'<START>\n{{user}}: …\n{{char}}: …'}
					ariaLabel={$i18n.t('Example Dialogue')}
					rows={3}
					minSize={64}
					bind:value={character.mes_example}
				/>
			</div>

			{#each promptFields as field}
				<div>
					<div class="flex items-center justify-between">
						<div class={labelClass}>{field.label}</div>
						<select
							class={selectClass}
							aria-label={$i18n.t('How {{FIELD}} combines with the default', {
								FIELD: field.label
							})}
							bind:value={character.prompt_overrides[field.override]}
						>
							<option value="replace">{$i18n.t('Replaces default')}</option>
							<option value="supplement">{$i18n.t('Adds to default')}</option>
						</select>
					</div>
					<Textarea
						className={textareaClass}
						placeholder={field.placeholder}
						ariaLabel={field.label}
						rows={2}
						minSize={48}
						bind:value={character[field.key]}
					/>
				</div>
			{/each}

			<div>
				<div class="flex items-center justify-between gap-2">
					<div class={labelClass}>{$i18n.t('Character Note')}</div>
					<div class="flex items-center gap-2 text-xs text-gray-500">
						<label class="flex items-center gap-1">
							{$i18n.t('Depth')}
							<input
								type="number"
								min="0"
								max="1000"
								class="w-12 bg-transparent outline-hidden text-right"
								bind:value={character.depth_prompt.depth}
							/>
						</label>
						<select
							class={selectClass}
							aria-label={$i18n.t('Role')}
							bind:value={character.depth_prompt.role}
						>
							<option value="system">{$i18n.t('System')}</option>
							<option value="user">{$i18n.t('User')}</option>
							<option value="assistant">{$i18n.t('Assistant')}</option>
						</select>
					</div>
				</div>
				<Textarea
					className={textareaClass}
					placeholder={$i18n.t('Inserted this many messages from the end of the chat')}
					ariaLabel={$i18n.t('Character Note')}
					rows={1}
					bind:value={character.depth_prompt.text}
				/>
			</div>

			<div>
				<div class={labelClass}>{$i18n.t('Reply Cleanup')}</div>
				<div class="flex flex-col gap-1.5 text-xs">
					<label class="flex items-center justify-between gap-2">
						<span>
							{$i18n.t("Stop replies that start writing the user's lines")}
							<span class="block text-[0.6875rem] text-gray-400">
								{$i18n.t(
									'Cuts the reply at a line like "{{USER}}: …" and drops a leading "{{CHAR}}:".',
									{
										USER: '{{user}}',
										CHAR: '{{char}}',
										interpolation: { escapeValue: false }
									}
								)}
							</span>
						</span>
						<Switch bind:state={character.reply_cleanup.stop_at_user} />
					</label>
					<label class="flex items-center justify-between gap-2">
						<span>
							{$i18n.t('Trim unfinished sentences')}
							<span class="block text-[0.6875rem] text-gray-400">
								{$i18n.t(
									'Removes a last sentence that was cut off, for example by the token limit.'
								)}
							</span>
						</span>
						<Switch bind:state={character.reply_cleanup.trim_incomplete} />
					</label>

					{#each character.reply_cleanup.rules as rule, index}
						<div class="flex items-center gap-1.5">
							<input
								type="checkbox"
								aria-label={$i18n.t('Rule enabled')}
								bind:checked={rule.enabled}
							/>
							<input
								class="{inputClass} flex-1 min-w-0 font-mono"
								placeholder={rule.regex ? '/pattern/gi' : $i18n.t('Find')}
								aria-label={$i18n.t('Find')}
								bind:value={rule.find}
							/>
							<input
								class="{inputClass} flex-1 min-w-0 font-mono"
								placeholder={rule.regex ? $i18n.t('Replace ($1 for groups)') : $i18n.t('Replace')}
								aria-label={$i18n.t('Replace')}
								bind:value={rule.replace}
							/>
							<label class="flex items-center gap-1 text-gray-500 shrink-0">
								<input type="checkbox" bind:checked={rule.regex} />
								{$i18n.t('Regex')}
							</label>
							<button
								type="button"
								class="p-0.5 rounded-full text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-850"
								aria-label={$i18n.t('Remove rule')}
								on:click={() => removeRule(index)}
							>
								<XMark className="size-3" />
							</button>
						</div>
					{/each}
					<div class="flex items-center justify-between gap-2">
						<span class="text-[0.6875rem] text-gray-400">
							{$i18n.t(
								'Find-and-replace rules run on every finished reply, in order. {{CHAR}} and {{USER}} work in both fields.',
								{ CHAR: '{{char}}', USER: '{{user}}', interpolation: { escapeValue: false } }
							)}
						</span>
						<button
							type="button"
							class="px-2 py-0.5 text-xs rounded-lg text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-850 flex items-center gap-1 shrink-0"
							on:click={addRule}
						>
							<Plus className="size-3" />
							{$i18n.t('Add rule')}
						</button>
					</div>
				</div>
			</div>

			<div class="flex gap-3">
				<div class="flex-1">
					<div class={labelClass}>{$i18n.t('Creator')}</div>
					<input
						class={inputClass}
						aria-label={$i18n.t('Creator')}
						bind:value={character.creator}
					/>
				</div>
			</div>

			<div>
				<div class={labelClass}>{$i18n.t('Lorebooks')}</div>
				<LorebookPicker
					value={lorebookIds ?? []}
					on:change={(event) => (lorebookIds = event.detail)}
				/>
				{#if character.character_book?.entries?.length}
					<div class="mt-1 text-[0.6875rem] text-gray-400">
						{$i18n.t(
							'This character also carries its original embedded lorebook ({{COUNT}} entries), used as is.',
							{ COUNT: character.character_book.entries.length }
						)}
					</div>
				{/if}
			</div>

			<div>
				<div class="flex items-center justify-between">
					<div class={labelClass}>
						{$i18n.t('Expressions')}
						{#if expressionNames.length}<span class="text-gray-400">{expressionNames.length}</span
							>{/if}
					</div>
					{#if modelId}
						<button
							type="button"
							class="px-2 py-0.5 text-xs rounded-lg text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-850 flex items-center gap-1 disabled:opacity-50"
							disabled={expressionsBusy}
							on:click={() => expressionInput.click()}
						>
							{#if expressionsBusy}<Spinner className="size-3" />{:else}<Plus
									className="size-3"
								/>{/if}
							{$i18n.t('Add images')}
						</button>
						{#if canGenerateImages}
							<button
								type="button"
								class="px-2 py-0.5 text-xs rounded-lg text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-850 flex items-center gap-1 disabled:opacity-50"
								disabled={!!generating}
								on:click={() => (showGenerate ? (showGenerate = false) : openGenerate())}
							>
								{$i18n.t('Generate')}
							</button>
						{/if}
					{/if}
				</div>
				{#if showGenerate && modelId}
					<div
						class="mt-1 p-2 rounded-lg border border-gray-100 dark:border-gray-850 flex flex-col gap-1.5 text-xs"
					>
						<label>
							<span class="text-gray-500">{$i18n.t('Expressions to draw (comma-separated)')}</span>
							<input class={inputClass} bind:value={generateNames} disabled={!!generating} />
						</label>
						<label>
							<span class="text-gray-500">{$i18n.t('Art style (optional)')}</span>
							<input
								class={inputClass}
								placeholder={$i18n.t('e.g. anime, soft watercolor')}
								bind:value={generateStyle}
								disabled={!!generating}
							/>
						</label>
						<div class="flex items-center justify-between gap-2">
							<span class="text-[0.6875rem] text-gray-400">
								{$i18n.t(
									'With image editing set up, each image redraws the avatar with the expression, so the look stays the same; otherwise it is drawn from the description. Same names replace existing images.'
								)}
							</span>
							<button
								type="button"
								class="px-3 py-1 rounded-lg bg-black text-white dark:bg-white dark:text-black flex items-center gap-1.5 shrink-0 disabled:opacity-60"
								disabled={!!generating || !generateNames.trim()}
								on:click={generateExpressions}
							>
								{#if generating}
									<Spinner className="size-3" />
									{$i18n.t('Drawing {{NAME}} ({{INDEX}}/{{TOTAL}})', {
										NAME: generating.name,
										INDEX: generating.index,
										TOTAL: generating.total
									})}
								{:else}
									{$i18n.t('Generate')}
								{/if}
							</button>
						</div>
					</div>
				{/if}
				<input
					bind:this={expressionInput}
					type="file"
					accept="image/*,.zip,application/zip"
					multiple
					hidden
					on:change={(event) => uploadExpressions(event.currentTarget.files)}
				/>
				{#if modelId && expressionNames.length}
					<div class="mt-1 grid grid-cols-4 sm:grid-cols-6 gap-2">
						{#each expressionNames as name (name)}
							<div class="relative group min-w-0">
								<img
									src={`${characterExpressionUrl(modelId, name)}&v=${expressionVersion}`}
									alt={name}
									loading="lazy"
									class="w-full aspect-[3/4] object-cover object-top rounded-lg bg-gray-50 dark:bg-gray-850"
								/>
								<div class="text-[0.6875rem] text-gray-500 truncate text-center" title={name}>
									{name}
								</div>
								<button
									type="button"
									class="absolute top-1 right-1 p-0.5 rounded-full bg-white/80 dark:bg-black/60 text-gray-600 dark:text-gray-300 opacity-0 group-hover:opacity-100 focus:opacity-100"
									aria-label={$i18n.t('Remove {{NAME}}', { NAME: name })}
									on:click={() => removeExpression(name)}
								>
									<XMark className="size-3" />
								</button>
							</div>
						{/each}
					</div>
				{/if}
				<div class="mt-1 text-[0.6875rem] text-gray-400">
					{modelId
						? $i18n.t(
								'Name each image after its emotion (joy.png, angry.webp…) or upload a sprite pack .zip. After each reply, the task model picks the image that fits.'
							)
						: $i18n.t('Save the character first, then add expression images.')}
				</div>
			</div>

			<div>
				<div class={labelClass}>{$i18n.t('Creator Notes')}</div>
				<Textarea
					className={textareaClass}
					placeholder={$i18n.t('Shown to users; never sent to the model')}
					ariaLabel={$i18n.t('Creator Notes')}
					rows={2}
					minSize={48}
					bind:value={character.creator_notes}
				/>
			</div>
		</div>
	{/if}
</section>
