<script lang="ts">
	// Create a character from card text pasted from anywhere (another chat, a website): card JSON
	// or labelled text ("Name: ... Personality: ... First message: ..."). The fields are shown for
	// review before anything is saved.
	import { getContext } from 'svelte';
	import { toast } from 'svelte-sonner';

	import { models, user } from '$lib/stores';
	import { convertSystemPromptToCharacter, createCharacter } from '$lib/apis/models';
	import Modal from '$lib/components/common/Modal.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import Switch from '$lib/components/common/Switch.svelte';
	import Textarea from '$lib/components/common/Textarea.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';
	import ModelSelector from '$lib/components/chat/ModelSelector/Selector.svelte';

	const i18n: any = getContext('i18n');

	export let show = false;
	export let onCreated: (model: any) => void = () => {};

	let step: 'paste' | 'review' = 'paste';
	let text = '';
	let character: Record<string, any> | null = null;
	let detected: string[] = [];
	let method: 'parser' | 'ai' | 'card' = 'parser';
	let busy: 'read' | 'ai' | 'create' | null = null;
	let baseModelId: string | null = null;
	let isPublic = false;

	$: baseModelItems = (($models ?? []) as any[])
		.filter((model) => !model?.preset && model?.owned_by !== 'arena' && !(model?.direct ?? false))
		.map((model) => ({ value: model.id, label: model.name, model }));

	$: if (!show) reset();

	const reset = () => {
		step = 'paste';
		text = '';
		character = null;
		detected = [];
		busy = null;
	};

	$: fields = [
		{ key: 'description', label: $i18n.t('Description'), rows: 4 },
		{ key: 'personality', label: $i18n.t('Personality'), rows: 2 },
		{ key: 'scenario', label: $i18n.t('Scenario'), rows: 2 },
		{ key: 'first_mes', label: $i18n.t('First Message'), rows: 3 },
		{ key: 'mes_example', label: $i18n.t('Example Dialogue'), rows: 3 },
		{ key: 'system_prompt', label: $i18n.t('Character System Prompt'), rows: 2 },
		{ key: 'creator_notes', label: $i18n.t('Creator Notes'), rows: 2 }
	];

	const read = async (useAi: boolean) => {
		if (!text.trim()) return;
		busy = useAi ? 'ai' : 'read';
		try {
			const result = await convertSystemPromptToCharacter(localStorage.token, {
				system: text,
				name: '',
				base_model_id: baseModelId,
				use_ai: useAi
			});
			character = { alternate_greetings: [], tags: [], ...result.character };
			detected = result.detected;
			method = result.method;
			step = 'review';
		} catch (error: any) {
			toast.error(`${error?.detail ?? error}`);
		} finally {
			busy = null;
		}
	};

	const setField = (key: string, value: string) => (character = { ...character, [key]: value });

	const create = async () => {
		if (!character || !baseModelId) return;
		if (!`${character.name ?? ''}`.trim()) {
			toast.error($i18n.t('Give the character a name.'));
			return;
		}
		busy = 'create';
		try {
			const [model] = await createCharacter(localStorage.token, {
				base_model_id: baseModelId,
				character,
				public: isPublic
			});
			toast.success($i18n.t('Created {{NAME}}', { NAME: model.name }));
			show = false;
			onCreated(model);
		} catch (error: any) {
			toast.error(`${error?.detail ?? error}`);
		} finally {
			busy = null;
		}
	};

	const labelClass = 'mb-1 text-xs text-gray-500';
	const boxClass =
		'w-full rounded-xl bg-gray-50 dark:bg-gray-850 px-3 py-2 text-[0.8125rem] outline-hidden placeholder:text-gray-400 dark:placeholder:text-gray-600';
	const buttonClass =
		'px-3 py-1.5 text-sm rounded-full transition flex items-center gap-2 disabled:opacity-50';
</script>

<Modal size="md" bind:show>
	<div class="px-4 pt-3 pb-4 dark:text-gray-200">
		<div class="flex justify-between items-center pb-2">
			<div class="text-sm font-medium">{$i18n.t('New character from text')}</div>
			<button
				class="self-center rounded-lg p-1 text-gray-500 transition hover:bg-gray-50 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-200"
				aria-label={$i18n.t('Close')}
				on:click={() => (show = false)}
			>
				<XMark className="size-4" />
			</button>
		</div>

		<div class="mb-3">
			<div class={labelClass}>{$i18n.t('Base Model (From)')}</div>
			<ModelSelector
				id="paste-character-base-model"
				placeholder={$i18n.t('Select a base model (e.g. llama3, gpt-4o)')}
				searchPlaceholder={$i18n.t('Search a model')}
				items={baseModelItems}
				triggerClassName="text-xs"
				selectionOnly
				includeHidden
				bind:value={baseModelId}
			/>
		</div>

		{#if step === 'paste'}
			<div class={labelClass}>
				{$i18n.t(
					'Paste a character card: JSON, or text with sections like Name, Description, Personality, Scenario, First message, Example dialogue.'
				)}
			</div>
			<textarea
				class="{boxClass} min-h-64 resize-y font-mono text-xs"
				aria-label={$i18n.t('Character card text')}
				placeholder={'Name: Mara\nPersonality: Dry humour, fiercely loyal\nScenario: {{user}} joins her crew\nFirst message: *Mara looks you over.* "You\'ll do."'}
				bind:value={text}
			></textarea>

			<div class="flex justify-end gap-2 pt-3">
				<button
					class="{buttonClass} bg-black hover:bg-gray-900 text-white dark:bg-white dark:text-black dark:hover:bg-gray-100"
					disabled={!text.trim() || busy !== null}
					on:click={() => read(false)}
				>
					{#if busy === 'read'}<Spinner className="size-3.5" />{/if}
					{$i18n.t('Read card')}
				</button>
			</div>
		{:else if character}
			{#if method === 'card'}
				<div class="mb-2 text-xs text-gray-500">{$i18n.t('Read as a character card.')}</div>
			{:else if method === 'parser' && detected.length < 2}
				<div
					class="mb-2 flex flex-wrap items-center justify-between gap-2 rounded-xl bg-amber-50 dark:bg-amber-400/10 px-3 py-2 text-xs text-amber-800 dark:text-amber-200"
				>
					<span>
						{$i18n.t(
							'Few labelled sections were found, so most of the text went into the description.'
						)}
					</span>
					<button
						class="px-2.5 py-1 rounded-lg bg-white/70 dark:bg-gray-800 hover:bg-white dark:hover:bg-gray-700 flex items-center gap-1 disabled:opacity-50"
						disabled={busy !== null || !baseModelId}
						title={baseModelId ? '' : $i18n.t('Choose a base model first.')}
						on:click={() => read(true)}
					>
						{#if busy === 'ai'}<Spinner className="size-3" />{/if}
						{$i18n.t('Sort it with AI')}
					</button>
				</div>
			{/if}

			<div class="flex flex-col gap-2.5 max-h-[55vh] overflow-y-auto pr-1">
				<div>
					<div class={labelClass}>{$i18n.t('Character Name')}</div>
					<input
						class={boxClass}
						aria-label={$i18n.t('Character Name')}
						value={character.name ?? ''}
						on:input={(e) => setField('name', e.currentTarget.value)}
					/>
				</div>
				{#each fields as field (field.key)}
					{#if (character[field.key] ?? '').trim() || ['description', 'first_mes'].includes(field.key)}
						<div>
							<div class={labelClass}>{field.label}</div>
							<Textarea
								className="{boxClass} resize-none"
								ariaLabel={field.label}
								value={character[field.key] ?? ''}
								rows={field.rows}
								maxSize={240}
								onInput={(e) => setField(field.key, (e.currentTarget as HTMLTextAreaElement).value)}
							/>
						</div>
					{/if}
				{/each}
				<div class="text-xs text-gray-500">
					{$i18n.t('{{COUNT}} greeting(s)', {
						COUNT: (character.first_mes ? 1 : 0) + (character.alternate_greetings?.length ?? 0)
					})}
					{#if character.tags?.length}
						· {character.tags.join(', ')}
					{/if}
					{#if character.character_book?.entries?.length}
						· {$i18n.t('Embedded lorebook ({{COUNT}} entries)', {
							COUNT: character.character_book.entries.length
						})}
					{/if}
				</div>
			</div>

			{#if $user?.role === 'admin'}
				<div class="mt-3 flex items-center justify-between text-xs">
					<div class="font-medium">{$i18n.t('Visible to all users')}</div>
					<Switch bind:state={isPublic} ariaLabel={$i18n.t('Visible to all users')} />
				</div>
			{/if}

			<div class="flex justify-between gap-2 pt-4">
				<button
					class="{buttonClass} bg-gray-50 hover:bg-gray-100 dark:bg-gray-850 dark:hover:bg-gray-800"
					disabled={busy !== null}
					on:click={() => (step = 'paste')}
				>
					{$i18n.t('Back')}
				</button>
				<button
					class="{buttonClass} bg-black hover:bg-gray-900 text-white dark:bg-white dark:text-black dark:hover:bg-gray-100"
					disabled={busy !== null || !baseModelId}
					title={baseModelId ? '' : $i18n.t('Choose a base model first.')}
					on:click={create}
				>
					{#if busy === 'create'}<Spinner className="size-3.5" />{/if}
					{$i18n.t('Create character')}
				</button>
			</div>
		{/if}
	</div>
</Modal>
