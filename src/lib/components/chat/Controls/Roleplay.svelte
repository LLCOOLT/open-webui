<script lang="ts">
	import { getContext, onDestroy, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';

	import { chatId, chatPersonaId, settings } from '$lib/stores';
	import {
		getChatMemory,
		getChatRoleplaySettings,
		refreshChatMemory,
		updateChatMemory,
		updateChatRoleplaySettings,
		type ChatMemory,
		type ChatRoleplaySettings
	} from '$lib/apis/lorebooks';
	import LorebookPicker from '$lib/components/workspace/Lorebooks/LorebookPicker.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import Switch from '$lib/components/common/Switch.svelte';
	import Textarea from '$lib/components/common/Textarea.svelte';

	const i18n: any = getContext('i18n');

	export let models: any[] = [];

	const NON_SAVED_PREFIXES = ['temporary:', 'local:', 'channel:'];

	let chatSettings: ChatRoleplaySettings | null = null;
	let note = { text: '', depth: 4, role: 'system' as 'system' | 'user' | 'assistant' };
	let loadedFor: string | null = null;
	let saveTimer: ReturnType<typeof setTimeout>;

	$: savedChat = !!$chatId && !NON_SAVED_PREFIXES.some((prefix) => $chatId.startsWith(prefix));
	$: if (savedChat && $chatId !== loadedFor) load($chatId);
	$: personas = $settings?.personas ?? [];
	$: isCharacterChat = (models ?? []).some((model) => model?.info?.meta?.character);

	let memory: ChatMemory | null = null;
	let memoryDraft = '';
	let memoryEnabled = true;
	let memoryBusy: 'save' | 'refresh' | 'clear' | null = null;

	const setMemory = (value: ChatMemory) => {
		memory = value;
		memoryDraft = value.summary ?? '';
	};

	const loadMemory = async () => {
		if (!loadedFor) return;
		try {
			setMemory(await getChatMemory(localStorage.token, loadedFor));
		} catch {
			memory = null;
		}
	};

	const memoryAction = async (action: 'save' | 'refresh' | 'clear') => {
		if (!loadedFor) return;
		memoryBusy = action;
		try {
			setMemory(
				action === 'refresh'
					? await refreshChatMemory(localStorage.token, loadedFor)
					: await updateChatMemory(
							localStorage.token,
							loadedFor,
							action === 'clear' ? '' : memoryDraft
						)
			);
			if (action === 'refresh') toast.success($i18n.t('Story memory updated'));
		} catch (error: any) {
			toast.error(`${error?.detail ?? error}`);
		} finally {
			memoryBusy = null;
		}
	};

	const onMemoryUpdated = () => loadMemory();
	onMount(() => window.addEventListener('roleplay:memory-updated', onMemoryUpdated));
	onDestroy(() => window.removeEventListener('roleplay:memory-updated', onMemoryUpdated));
	$: defaultPersona = personas.find((persona) => persona.is_default);

	const load = async (id: string) => {
		loadedFor = id;
		try {
			chatSettings = await getChatRoleplaySettings(localStorage.token, id);
			note = chatSettings.authors_note ?? { text: '', depth: 4, role: 'system' };
			memoryEnabled = chatSettings.memory_enabled ?? true;
			loadMemory();
		} catch {
			chatSettings = null;
		}
	};

	const save = async (lorebookIds: string[] = chatSettings?.lorebook_ids ?? []) => {
		if (!loadedFor) return;
		try {
			chatSettings = await updateChatRoleplaySettings(localStorage.token, loadedFor, {
				lorebook_ids: lorebookIds,
				authors_note: note.text.trim()
					? { ...note, depth: Math.max(0, Number(note.depth) || 0) }
					: null,
				persona_id: $chatPersonaId,
				memory_enabled: memoryEnabled
			});
		} catch (error: any) {
			toast.error(`${error?.detail ?? error}`);
		}
	};

	const saveSoon = () => {
		clearTimeout(saveTimer);
		saveTimer = setTimeout(() => save(), 600);
	};
</script>

{#if personas.length}
	<div class="py-1 text-xs flex items-center justify-between gap-2">
		<label for="chat-persona" class="text-gray-500">{$i18n.t('You are')}</label>
		<select
			id="chat-persona"
			class="bg-transparent pr-6 outline-hidden text-right"
			value={$chatPersonaId ?? '__default__'}
			on:change={(event) => {
				const value = event.currentTarget.value;
				chatPersonaId.set(value === '__default__' ? null : value);
				if (savedChat && chatSettings) save();
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
	</div>
{/if}

{#if !savedChat}
	<div class="py-1 text-xs text-gray-400">
		{$i18n.t('Send a message first; lorebooks and the author’s note are saved with the chat.')}
	</div>
{:else if chatSettings}
	<div class="space-y-2.5 py-1 text-xs">
		<div>
			<div class="mb-1 text-gray-500">{$i18n.t('Lorebooks')}</div>
			<LorebookPicker value={chatSettings.lorebook_ids} on:change={(event) => save(event.detail)} />
		</div>

		<div>
			<div class="mb-1 flex items-center justify-between text-gray-500">
				<span>{$i18n.t("Author's Note")}</span>
				<span class="flex items-center gap-2">
					<label class="flex items-center gap-1">
						{$i18n.t('Depth')}
						<input
							type="number"
							min="0"
							max="1000"
							class="w-10 bg-transparent outline-hidden text-right"
							bind:value={note.depth}
							on:input={saveSoon}
						/>
					</label>
					<select
						class="bg-transparent pr-6 outline-hidden"
						aria-label={$i18n.t('Role')}
						bind:value={note.role}
						on:change={() => save()}
					>
						<option value="system">{$i18n.t('System')}</option>
						<option value="user">{$i18n.t('User')}</option>
						<option value="assistant">{$i18n.t('Assistant')}</option>
					</select>
				</span>
			</div>
			<textarea
				class="w-full resize-vertical bg-transparent py-1 outline-hidden placeholder:text-gray-300 dark:placeholder:text-gray-700"
				rows="2"
				aria-label={$i18n.t("Author's Note")}
				placeholder={$i18n.t('Steers the story; inserted this many messages from the end')}
				bind:value={note.text}
				on:input={saveSoon}
			></textarea>
		</div>

		{#if isCharacterChat && memory?.available}
			<div>
				<div class="mb-1 flex items-center justify-between text-gray-500">
					<span>{$i18n.t('Long-chat memory')}</span>
					<Switch
						bind:state={memoryEnabled}
						ariaLabel={$i18n.t('Long-chat memory')}
						on:change={async (event) => {
							memoryEnabled = event.detail;
							await save();
							loadMemory();
						}}
					/>
				</div>
				{#if memoryEnabled && memory}
					<div class="text-gray-400 mb-1">
						{memory.summary
							? $i18n.t(
									'Covers the first {{COUNT}} of {{TOTAL}} messages; newer ones are sent in full.',
									{
										COUNT: memory.summarized_messages,
										TOTAL: memory.total_messages
									}
								)
							: $i18n.t(
									'Older messages are summarized into a story memory once the chat passes about {{TOKENS}} tokens.',
									{ TOKENS: memory.token_threshold.toLocaleString() }
								)}
					</div>
					{#if memory.summary}
						<Textarea
							className="w-full resize-vertical bg-transparent py-1 outline-hidden max-h-80 overflow-y-auto"
							ariaLabel={$i18n.t('Story memory')}
							bind:value={memoryDraft}
							rows={6}
							minSize={96}
						/>
					{/if}
					<div class="mt-1 flex flex-wrap items-center gap-1.5">
						{#if memory.summary && memoryDraft !== memory.summary}
							<button
								type="button"
								class="px-2 py-0.5 rounded-lg bg-gray-100 dark:bg-gray-850 hover:bg-gray-200 dark:hover:bg-gray-800 flex items-center gap-1"
								disabled={memoryBusy !== null}
								on:click={() => memoryAction('save')}
							>
								{#if memoryBusy === 'save'}<Spinner className="size-3" />{/if}
								{$i18n.t('Save')}
							</button>
						{/if}
						<button
							type="button"
							class="px-2 py-0.5 rounded-lg bg-gray-100 dark:bg-gray-850 hover:bg-gray-200 dark:hover:bg-gray-800 flex items-center gap-1"
							disabled={memoryBusy !== null}
							title={$i18n.t('Summarize the older messages into the memory now')}
							on:click={() => memoryAction('refresh')}
						>
							{#if memoryBusy === 'refresh'}<Spinner className="size-3" />{/if}
							{$i18n.t('Update now')}
						</button>
						{#if memory.summary}
							<button
								type="button"
								class="px-2 py-0.5 rounded-lg text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-850 flex items-center gap-1"
								disabled={memoryBusy !== null}
								title={$i18n.t('Forget the memory and send the full chat again')}
								on:click={() => memoryAction('clear')}
							>
								{#if memoryBusy === 'clear'}<Spinner className="size-3" />{/if}
								{$i18n.t('Clear')}
							</button>
						{/if}
					</div>
				{/if}
			</div>
		{/if}
	</div>
{/if}
