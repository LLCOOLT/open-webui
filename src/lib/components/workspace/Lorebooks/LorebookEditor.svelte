<script lang="ts">
	import { getContext } from 'svelte';
	import { toast } from 'svelte-sonner';

	import { user } from '$lib/stores';
	import {
		testLorebook,
		type Lorebook,
		type LorebookEntry,
		type LorebookForm
	} from '$lib/apis/lorebooks';

	import AccessButton from '$lib/components/common/AccessButton.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import Switch from '$lib/components/common/Switch.svelte';
	import Textarea from '$lib/components/common/Textarea.svelte';
	import ChevronDown from '$lib/components/icons/ChevronDown.svelte';
	import ChevronLeft from '$lib/components/icons/ChevronLeft.svelte';
	import ChevronUp from '$lib/components/icons/ChevronUp.svelte';
	import Plus from '$lib/components/icons/Plus.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';
	import AccessControlModal from '../common/AccessControlModal.svelte';

	const i18n: any = getContext('i18n');

	export let lorebook: Lorebook | null = null;
	export let onSubmit: (form: LorebookForm) => Promise<void>;
	export let onBack: () => void = () => {};

	const newEntry = (index: number): LorebookEntry => ({
		name: '',
		comment: '',
		enabled: true,
		keys: [],
		secondary_keys: [],
		selective: false,
		use_regex: false,
		case_sensitive: false,
		constant: false,
		insertion_order: 100 + index,
		priority: 10,
		position: 'before_char',
		content: ''
	});

	let name = lorebook?.name ?? '';
	let description = lorebook?.description ?? '';
	let scanDepth: number | null = lorebook?.data?.scan_depth ?? null;
	let tokenBudget: number | null = lorebook?.data?.token_budget ?? null;
	let recursive = lorebook?.data?.recursive_scanning ?? false;
	let entries: LorebookEntry[] = structuredClone(lorebook?.data?.entries ?? []);
	let accessGrants: any[] = lorebook?.access_grants ?? [];
	let expanded = new Set<number>();
	let showAccess = false;
	let saving = false;

	let testText = '';
	let testing = false;
	let testResult: Awaited<ReturnType<typeof testLorebook>> | null = null;

	const readOnly = lorebook !== null && lorebook.write_access === false;

	const data = () => ({
		scan_depth: scanDepth || null,
		token_budget: tokenBudget || null,
		recursive_scanning: recursive,
		extensions: lorebook?.data?.extensions ?? {},
		entries
	});

	const toggle = (index: number) => {
		expanded.has(index) ? expanded.delete(index) : expanded.add(index);
		expanded = expanded;
	};

	const addEntry = () => {
		entries = [...entries, newEntry(entries.length)];
		expanded = new Set([...expanded, entries.length - 1]);
	};

	const removeEntry = (index: number) => {
		entries = entries.filter((_, i: number) => i !== index);
		expanded = new Set();
	};

	const parseKeys = (value: string) =>
		value
			.split(',')
			.map((key) => key.trim())
			.filter(Boolean);

	const summary = (entry: LorebookEntry) =>
		entry.name ||
		entry.comment ||
		(entry.constant ? $i18n.t('Always active') : entry.keys.join(', ')) ||
		$i18n.t('Untitled entry');

	const runTest = async () => {
		testing = true;
		try {
			testResult = await testLorebook(localStorage.token, data(), testText);
		} catch (error: any) {
			toast.error(`${error?.detail ?? error}`);
		} finally {
			testing = false;
		}
	};

	const submitHandler = async () => {
		if (!name.trim()) {
			toast.error($i18n.t('Name is required.'));
			return;
		}
		saving = true;
		try {
			await onSubmit({
				name: name.trim(),
				description: description.trim() || null,
				data: data(),
				meta: lorebook?.meta ?? null,
				access_grants: accessGrants
			});
		} finally {
			saving = false;
		}
	};

	// Defined here, not inline in the markup: bindings inside an each over an inline array
	// literal miscompile in Svelte 5 legacy mode.
	$: entryFlags = [
		{ key: 'constant', label: $i18n.t('Always active') },
		{ key: 'use_regex', label: $i18n.t('Regex keys') },
		{ key: 'case_sensitive', label: $i18n.t('Case sensitive') },
		{ key: 'selective', label: $i18n.t('Require a secondary key') }
	] as { key: 'constant' | 'use_regex' | 'case_sensitive' | 'selective'; label: string }[];

	const labelClass = 'mb-1 text-xs text-gray-500';
	const inputClass =
		'w-full bg-transparent text-sm outline-hidden placeholder:text-gray-300 dark:placeholder:text-gray-700';
	const numberClass = 'w-16 bg-transparent text-sm outline-hidden text-right';
	const textareaClass =
		'min-h-12 w-full resize-none overflow-y-hidden bg-transparent py-1 text-[0.8125rem] outline-hidden placeholder:text-gray-300 dark:placeholder:text-gray-700';
</script>

<AccessControlModal
	bind:show={showAccess}
	bind:accessGrants
	accessRoles={['read', 'write']}
	share={$user?.permissions?.sharing?.lorebooks || $user?.role === 'admin'}
	sharePublic={$user?.permissions?.sharing?.public_lorebooks || $user?.role === 'admin'}
	shareUsers={($user?.permissions?.access_grants?.allow_users ?? true) || $user?.role === 'admin'}
	allowGroups={($user?.permissions?.access_grants?.allow_groups ?? true) || $user?.role === 'admin'}
/>

<form class="flex flex-col max-w-3xl mx-auto w-full pb-10" on:submit|preventDefault={submitHandler}>
	<button
		type="button"
		class="flex items-center gap-1 text-xs text-gray-500 hover:text-gray-900 dark:hover:text-gray-100 w-fit mb-3"
		on:click={onBack}
	>
		<ChevronLeft className="size-3" />
		{$i18n.t('Back')}
	</button>

	<div class="flex items-start gap-3">
		<div class="flex-1">
			<input
				class="w-full text-2xl font-medium bg-transparent outline-hidden"
				placeholder={$i18n.t('Lorebook Name')}
				aria-label={$i18n.t('Lorebook Name')}
				bind:value={name}
				disabled={readOnly}
				required
			/>
			<input
				class="{inputClass} mt-1 text-gray-500"
				placeholder={$i18n.t('Description (optional)')}
				aria-label={$i18n.t('Description')}
				bind:value={description}
				disabled={readOnly}
			/>
		</div>
		{#if !readOnly}
			<AccessButton on:click={() => (showAccess = true)} />
		{/if}
	</div>

	<div class="mt-4 flex flex-wrap gap-x-6 gap-y-2 text-xs text-gray-500">
		<label class="flex items-center gap-2">
			{$i18n.t('Scan depth')}
			<input
				type="number"
				min="1"
				max="1000"
				class={numberClass}
				placeholder="4"
				bind:value={scanDepth}
				disabled={readOnly}
			/>
		</label>
		<label class="flex items-center gap-2">
			{$i18n.t('Token budget')}
			<input
				type="number"
				min="0"
				class="{numberClass} w-20"
				placeholder={$i18n.t('None')}
				bind:value={tokenBudget}
				disabled={readOnly}
			/>
		</label>
		<div class="flex items-center gap-2">
			<span>{$i18n.t('Recursive scanning')}</span>
			<Switch bind:state={recursive} ariaLabel={$i18n.t('Recursive scanning')} />
		</div>
	</div>

	<div class="mt-6 flex items-center justify-between">
		<div class="text-sm font-medium">
			{$i18n.t('Entries')} <span class="text-gray-400 font-normal">{entries.length}</span>
		</div>
		{#if !readOnly}
			<button
				type="button"
				class="flex items-center gap-1 text-xs px-2 py-1 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-850"
				on:click={addEntry}
			>
				<Plus className="size-3" />
				{$i18n.t('Add Entry')}
			</button>
		{/if}
	</div>

	<div class="mt-2 flex flex-col gap-1.5">
		{#each entries as entry, index}
			<div class="rounded-xl border border-gray-100 dark:border-gray-850">
				<div class="flex items-center gap-2 px-3 py-2">
					<Switch bind:state={entry.enabled} ariaLabel={$i18n.t('Entry enabled')} />
					<button
						type="button"
						class="flex-1 min-w-0 text-left text-sm truncate {entry.enabled
							? ''
							: 'text-gray-400 line-through'}"
						on:click={() => toggle(index)}
					>
						{summary(entry)}
					</button>
					<span class="text-xs text-gray-400 shrink-0">
						{entry.position === 'after_char' ? $i18n.t('After') : $i18n.t('Before')} · {entry.priority}
					</span>
					<button
						type="button"
						class="p-1 rounded-lg text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-850"
						aria-label={expanded.has(index) ? $i18n.t('Collapse') : $i18n.t('Expand')}
						on:click={() => toggle(index)}
					>
						{#if expanded.has(index)}<ChevronUp className="size-3" />{:else}<ChevronDown
								className="size-3"
							/>{/if}
					</button>
					{#if !readOnly}
						<button
							type="button"
							class="p-1 rounded-lg text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-850"
							aria-label={$i18n.t('Remove')}
							on:click={() => removeEntry(index)}
						>
							<XMark className="size-3" />
						</button>
					{/if}
				</div>

				{#if expanded.has(index)}
					<div class="px-3 pb-3 space-y-2.5 border-t border-gray-100 dark:border-gray-850 pt-2.5">
						<div class="flex gap-3">
							<div class="flex-1">
								<div class={labelClass}>{$i18n.t('Keys')}</div>
								<input
									class={inputClass}
									placeholder={$i18n.t('Comma-separated, e.g. castle, keep')}
									aria-label={$i18n.t('Keys')}
									value={entry.keys.join(', ')}
									on:change={(event) => (entry.keys = parseKeys(event.currentTarget.value))}
								/>
							</div>
							<div class="flex-1">
								<div class={labelClass}>{$i18n.t('Name')}</div>
								<input
									class={inputClass}
									placeholder={$i18n.t('Optional')}
									aria-label={$i18n.t('Entry name')}
									bind:value={entry.name}
								/>
							</div>
						</div>

						<div>
							<div class={labelClass}>{$i18n.t('Content')}</div>
							<Textarea
								className={textareaClass}
								placeholder={$i18n.t('Added to the prompt when a key appears in the chat')}
								ariaLabel={$i18n.t('Entry content')}
								rows={3}
								minSize={64}
								bind:value={entry.content}
							/>
						</div>

						<div class="flex flex-wrap gap-x-5 gap-y-2 text-xs text-gray-500 items-center">
							{#each entryFlags as flag}
								<label class="flex items-center gap-1.5">
									<input type="checkbox" bind:checked={entry[flag.key]} />
									{flag.label}
								</label>
							{/each}
						</div>

						{#if entry.selective}
							<div>
								<div class={labelClass}>{$i18n.t('Secondary keys')}</div>
								<input
									class={inputClass}
									placeholder={$i18n.t('At least one must also appear')}
									aria-label={$i18n.t('Secondary keys')}
									value={entry.secondary_keys.join(', ')}
									on:change={(event) =>
										(entry.secondary_keys = parseKeys(event.currentTarget.value))}
								/>
							</div>
						{/if}

						<div class="flex flex-wrap gap-x-6 gap-y-2 text-xs text-gray-500 items-center">
							<label class="flex items-center gap-2">
								{$i18n.t('Position')}
								<select class="bg-transparent pr-6 outline-hidden" bind:value={entry.position}>
									<option value="before_char">{$i18n.t('Before description')}</option>
									<option value="after_char">{$i18n.t('After description')}</option>
								</select>
							</label>
							<label class="flex items-center gap-2">
								{$i18n.t('Priority')}
								<input type="number" class={numberClass} bind:value={entry.priority} />
							</label>
							<label class="flex items-center gap-2">
								{$i18n.t('Order')}
								<input type="number" class={numberClass} bind:value={entry.insertion_order} />
							</label>
						</div>
					</div>
				{/if}
			</div>
		{/each}
	</div>

	<div class="mt-6 rounded-xl bg-gray-50 dark:bg-gray-850 p-3">
		<div class="text-sm font-medium">{$i18n.t('Test')}</div>
		<div class="text-xs text-gray-500 mb-2">
			{$i18n.t('Paste some chat text to see which entries would activate.')}
		</div>
		<Textarea
			className={textareaClass}
			placeholder={$i18n.t('e.g. We finally reach the castle gates.')}
			ariaLabel={$i18n.t('Test text')}
			rows={2}
			bind:value={testText}
		/>
		<div class="flex justify-end">
			<button
				type="button"
				class="text-xs px-3 py-1 rounded-full bg-white dark:bg-gray-800 hover:bg-gray-100 dark:hover:bg-gray-700 flex items-center gap-1.5 disabled:opacity-50"
				disabled={testing || !testText.trim()}
				on:click={runTest}
			>
				{#if testing}<Spinner className="size-3" />{/if}
				{$i18n.t('Test')}
			</button>
		</div>
		{#if testResult}
			{@const activated = [
				...testResult.before_char.map((text) => ({ where: $i18n.t('Before description'), text })),
				...testResult.after_char.map((text) => ({ where: $i18n.t('After description'), text })),
				...testResult.depth.map((item) => ({
					where: $i18n.t('Depth {{DEPTH}} ({{ROLE}})', { DEPTH: item.depth, ROLE: item.role }),
					text: item.text
				}))
			]}
			<div class="mt-2 text-xs" data-testid="lorebook-test-result">
				{#if activated.length === 0}
					<div class="text-gray-500">{$i18n.t('No entries would activate.')}</div>
				{:else}
					{#each activated as item}
						<div class="py-1">
							<span class="text-gray-400">{item.where}:</span>
							<span class="whitespace-pre-line">{item.text}</span>
						</div>
					{/each}
				{/if}
			</div>
		{/if}
	</div>

	{#if !readOnly}
		<div class="mt-6 flex justify-end">
			<button
				type="submit"
				class="px-4 py-1.5 text-sm bg-black hover:bg-gray-900 text-white dark:bg-white dark:text-black dark:hover:bg-gray-100 transition rounded-full flex items-center gap-2 disabled:opacity-50"
				disabled={saving}
			>
				{#if saving}<Spinner className="size-3.5" />{/if}
				{lorebook ? $i18n.t('Save') : $i18n.t('Create Lorebook')}
			</button>
		</div>
	{/if}
</form>
