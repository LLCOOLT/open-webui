<script lang="ts">
	import { getContext, onDestroy, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';

	import { chatId, chatPersonaId, settings } from '$lib/stores';
	import {
		createLorebook,
		getChatMemory,
		getChatRecall,
		getChatRoleplaySettings,
		getLorebookById,
		refreshChatMemory,
		refreshChatRecall,
		suggestLoreEntries,
		updateChatMemory,
		updateChatRecall,
		updateChatRoleplaySettings,
		updateLorebookById,
		type ChatMemory,
		type ChatRecall,
		type ChatRoleplaySettings,
		type Lorebook
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
			if (action === 'refresh') {
				toast.success($i18n.t('Story memory updated'));
				memoryChanged = true;
			}
		} catch (error: any) {
			toast.error(`${error?.detail ?? error}`);
		} finally {
			memoryBusy = null;
		}
	};

	const onMemoryUpdated = () => {
		memoryChanged = true;
		loadMemory();
	};

	// Character memory across chats: what the character remembers about the user, one entry per
	// line, edited in place.
	let recall: ChatRecall | null = null;
	let recallDraft = '';
	let recallEnabled = true;
	let recallBusy: 'save' | 'refresh' | 'clear' | null = null;

	const recallText = (value: ChatRecall) => value.entries.map((entry) => entry.content).join('\n');

	const setRecall = (value: ChatRecall) => {
		recall = value;
		recallDraft = recallText(value);
	};

	const loadRecall = async () => {
		if (!loadedFor) return;
		try {
			setRecall(await getChatRecall(localStorage.token, loadedFor));
		} catch {
			recall = null;
		}
	};

	const recallAction = async (action: 'save' | 'refresh' | 'clear') => {
		if (!loadedFor || !recall) return;
		recallBusy = action;
		try {
			if (action === 'refresh') {
				const result = await refreshChatRecall(localStorage.token, loadedFor, false);
				setRecall(result);
				toast.success(
					result.updated
						? $i18n.t('{{NAME}} will remember this', { NAME: result.char })
						: $i18n.t('Nothing new to remember')
				);
			} else {
				// Lines that still match an entry keep it (and its history); the rest are new.
				const known = new Map(recall.entries.map((entry) => [entry.content.trim(), entry.id]));
				const lines =
					action === 'clear'
						? []
						: recallDraft
								.split('\n')
								.map((line) => line.replace(/^\s*[-•*]\s*/, '').trim())
								.filter(Boolean);
				setRecall(
					await updateChatRecall(
						localStorage.token,
						loadedFor,
						lines.map((content) => ({ id: known.get(content) ?? null, content }))
					)
				);
			}
		} catch (error: any) {
			toast.error(`${error?.detail ?? error}`);
		} finally {
			recallBusy = null;
		}
	};

	const onRecallUpdated = () => loadRecall();

	// Lorebook suggestions: lasting facts from the story memory, reviewed before they are added.
	type Suggestion = { name: string; keys: string; content: string; selected: boolean };
	let suggestions: Suggestion[] | null = null;
	let suggestionSource: 'memory' | 'chat' = 'memory';
	let suggesting = false;
	let addingLore = false;
	let memoryChanged = false; // the memory changed since suggestions were last checked
	let targetLorebook = '__new__';
	let writableBooks: Lorebook[] = [];

	const suggestLore = async () => {
		if (!loadedFor) return;
		suggesting = true;
		try {
			const result = await suggestLoreEntries(localStorage.token, loadedFor);
			suggestionSource = result.source;
			suggestions = result.suggestions.map((item) => ({
				name: item.name,
				keys: item.keys.join(', '),
				content: item.content,
				selected: true
			}));
			memoryChanged = false;
			if (!suggestions.length) toast.info($i18n.t('No new facts to add'));
			const books = await Promise.all(
				(chatSettings?.lorebook_ids ?? []).map((id) =>
					getLorebookById(localStorage.token, id).catch(() => null)
				)
			);
			writableBooks = books.filter((book): book is Lorebook => !!book?.write_access);
			if (!writableBooks.some((book) => book.id === targetLorebook)) {
				targetLorebook = writableBooks[0]?.id ?? '__new__';
			}
		} catch (error: any) {
			toast.error(`${error?.detail ?? error}`);
		} finally {
			suggesting = false;
		}
	};

	const addLore = async () => {
		const chosen = (suggestions ?? []).filter((item) => item.selected && item.content.trim());
		if (!loadedFor || !chosen.length) return;
		addingLore = true;
		try {
			const entries = (startId: number) =>
				chosen.map((item, index) => ({
					id: startId + index,
					name: item.name,
					comment: item.name,
					keys: item.keys
						.split(',')
						.map((key) => key.trim())
						.filter(Boolean),
					content: item.content.trim(),
					enabled: true
				}));
			if (targetLorebook === '__new__') {
				const book = await createLorebook(localStorage.token, {
					name: $i18n.t('Story facts'),
					description: $i18n.t('Facts from a roleplay chat, added from its story memory.'),
					data: { entries: entries(0) as any }
				});
				await save([...(chatSettings?.lorebook_ids ?? []), book.id]);
			} else {
				const book = await getLorebookById(localStorage.token, targetLorebook);
				const existing = book.data?.entries ?? [];
				const nextId = Math.max(-1, ...existing.map((entry) => Number(entry.id) || 0)) + 1;
				await updateLorebookById(localStorage.token, book.id, {
					name: book.name,
					description: book.description,
					data: { ...book.data, entries: [...existing, ...(entries(nextId) as any)] },
					meta: book.meta
				});
			}
			toast.success($i18n.t('Added {{COUNT}} lorebook entries', { COUNT: chosen.length }));
			suggestions = null;
		} catch (error: any) {
			toast.error(`${error?.detail ?? error}`);
		} finally {
			addingLore = false;
		}
	};
	onMount(() => {
		window.addEventListener('roleplay:memory-updated', onMemoryUpdated);
		window.addEventListener('roleplay:recall-updated', onRecallUpdated);
	});
	onDestroy(() => {
		window.removeEventListener('roleplay:memory-updated', onMemoryUpdated);
		window.removeEventListener('roleplay:recall-updated', onRecallUpdated);
	});
	$: defaultPersona = personas.find((persona) => persona.is_default);

	const load = async (id: string) => {
		loadedFor = id;
		try {
			chatSettings = await getChatRoleplaySettings(localStorage.token, id);
			note = chatSettings.authors_note ?? { text: '', depth: 4, role: 'system' };
			memoryEnabled = chatSettings.memory_enabled ?? true;
			recallEnabled = chatSettings.recall_enabled ?? true;
			loadMemory();
			loadRecall();
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
				memory_enabled: memoryEnabled,
				recall_enabled: recallEnabled
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

		{#if isCharacterChat && recall?.available && recall.character_enabled}
			<div>
				<div class="mb-1 flex items-center justify-between text-gray-500">
					<span>{$i18n.t('{{NAME}} remembers', { NAME: recall.char })}</span>
					<Switch
						bind:state={recallEnabled}
						ariaLabel={$i18n.t('Remember this chat')}
						on:change={async (event) => {
							recallEnabled = event.detail;
							await save();
							loadRecall();
						}}
					/>
				</div>
				<div class="text-gray-400 mb-1">
					{recallEnabled
						? $i18n.t(
								'What {{NAME}} knows about you from this and earlier chats; it is updated every few messages and kept only for you. One line per fact.',
								{ NAME: recall.char }
							)
						: $i18n.t('{{NAME}} will not remember this chat later.', { NAME: recall.char })}
				</div>
				{#if recallEnabled}
					{#if recall.entries.length || recallDraft}
						<Textarea
							className="w-full resize-vertical bg-transparent py-1 outline-hidden max-h-80 overflow-y-auto"
							ariaLabel={$i18n.t('{{NAME}} remembers', { NAME: recall.char })}
							placeholder={$i18n.t('Nothing yet')}
							bind:value={recallDraft}
							rows={4}
							minSize={64}
						/>
					{:else}
						<div class="text-gray-400 italic mb-1">{$i18n.t('Nothing yet')}</div>
					{/if}
					<div class="mt-1 flex flex-wrap items-center gap-1.5">
						{#if recallDraft !== recallText(recall)}
							<button
								type="button"
								class="px-2 py-0.5 rounded-lg bg-gray-100 dark:bg-gray-850 hover:bg-gray-200 dark:hover:bg-gray-800 flex items-center gap-1"
								disabled={recallBusy !== null}
								on:click={() => recallAction('save')}
							>
								{#if recallBusy === 'save'}<Spinner className="size-3" />{/if}
								{$i18n.t('Save')}
							</button>
						{/if}
						<button
							type="button"
							class="px-2 py-0.5 rounded-lg bg-gray-100 dark:bg-gray-850 hover:bg-gray-200 dark:hover:bg-gray-800 flex items-center gap-1"
							disabled={recallBusy !== null}
							title={$i18n.t('Add what happened in this chat to the memory now')}
							on:click={() => recallAction('refresh')}
						>
							{#if recallBusy === 'refresh'}<Spinner className="size-3" />{/if}
							{$i18n.t('Update now')}
						</button>
						{#if recall.entries.length}
							<button
								type="button"
								class="px-2 py-0.5 rounded-lg text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-850 flex items-center gap-1"
								disabled={recallBusy !== null}
								title={$i18n.t('Forget everything {{NAME}} remembers about you', {
									NAME: recall.char
								})}
								on:click={() => recallAction('clear')}
							>
								{#if recallBusy === 'clear'}<Spinner className="size-3" />{/if}
								{$i18n.t('Forget all')}
							</button>
						{/if}
					</div>
				{/if}
			</div>
		{/if}

		{#if isCharacterChat}
			<div>
				<div class="mb-1 flex items-center justify-between text-gray-500">
					<span>{$i18n.t('Lorebook suggestions')}</span>
					<button
						type="button"
						class="px-2 py-0.5 rounded-lg bg-gray-100 dark:bg-gray-850 hover:bg-gray-200 dark:hover:bg-gray-800 flex items-center gap-1"
						disabled={suggesting}
						title={$i18n.t(
							'Find people, places and facts from the story that no lorebook covers yet'
						)}
						on:click={suggestLore}
					>
						{#if suggesting}<Spinner className="size-3" />{/if}
						{$i18n.t('Suggest entries')}
					</button>
				</div>
				{#if memoryChanged && !suggestions}
					<div class="text-gray-400">
						{$i18n.t('The story memory was updated. Check it for new facts to keep in a lorebook.')}
					</div>
				{/if}
				{#if suggestions?.length}
					<div class="text-gray-400 mb-1">
						{suggestionSource === 'memory'
							? $i18n.t('From the story memory. Edit or untick before adding.')
							: $i18n.t(
									'From the recent messages (there is no story memory yet). Edit or untick before adding.'
								)}
					</div>
					<div class="flex flex-col gap-2">
						{#each suggestions as item}
							<div
								class="rounded-lg border border-gray-100 dark:border-gray-850 p-1.5 {item.selected
									? ''
									: 'opacity-50'}"
							>
								<div class="flex items-center gap-1.5">
									<input
										type="checkbox"
										aria-label={$i18n.t('Add {{NAME}}', { NAME: item.name })}
										bind:checked={item.selected}
									/>
									<input
										class="flex-1 min-w-0 bg-transparent outline-hidden font-medium"
										aria-label={$i18n.t('Name')}
										bind:value={item.name}
									/>
								</div>
								<input
									class="w-full bg-transparent outline-hidden text-gray-500"
									aria-label={$i18n.t('Keywords')}
									placeholder={$i18n.t('Keywords, comma-separated')}
									bind:value={item.keys}
								/>
								<Textarea
									className="w-full resize-vertical bg-transparent outline-hidden"
									ariaLabel={$i18n.t('Content')}
									bind:value={item.content}
									rows={2}
								/>
							</div>
						{/each}
					</div>
					<div class="mt-1.5 flex items-center justify-between gap-2">
						<select
							class="bg-transparent pr-6 outline-hidden min-w-0"
							aria-label={$i18n.t('Add to')}
							bind:value={targetLorebook}
						>
							{#each writableBooks as book (book.id)}
								<option value={book.id}>{book.name}</option>
							{/each}
							<option value="__new__">{$i18n.t('New lorebook for this chat')}</option>
						</select>
						<span class="flex items-center gap-1.5 shrink-0">
							<button
								type="button"
								class="px-2 py-0.5 rounded-lg text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-850"
								on:click={() => (suggestions = null)}
							>
								{$i18n.t('Dismiss')}
							</button>
							<button
								type="button"
								class="px-2 py-0.5 rounded-lg bg-gray-100 dark:bg-gray-850 hover:bg-gray-200 dark:hover:bg-gray-800 flex items-center gap-1"
								disabled={addingLore || !suggestions.some((item) => item.selected)}
								on:click={addLore}
							>
								{#if addingLore}<Spinner className="size-3" />{/if}
								{$i18n.t('Add {{COUNT}}', {
									COUNT: suggestions.filter((item) => item.selected).length
								})}
							</button>
						</span>
					</div>
				{/if}
			</div>
		{/if}
	</div>
{/if}
