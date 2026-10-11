<script lang="ts">
	import { getContext, onDestroy } from 'svelte';
	import { toast } from 'svelte-sonner';

	import { models, user } from '$lib/stores';
	import { importCharacterCards } from '$lib/apis/models';
	import {
		previewCharacterFile,
		revokePreviewImage,
		type CharacterPreview
	} from '$lib/utils/characters';

	import Modal from '$lib/components/common/Modal.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import Switch from '$lib/components/common/Switch.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';
	import ModelSelector from '$lib/components/chat/ModelSelector/Selector.svelte';

	const i18n: any = getContext('i18n');

	export let show = false;
	export let onImported: (models: any[]) => void = () => {};

	let fileInput: HTMLInputElement;
	let previews: CharacterPreview[] = [];
	let baseModelId: string | null = null;
	let isPublic = false;
	let importing = false;

	$: baseModelItems = (($models ?? []) as any[])
		.filter((model) => !model?.preset && model?.owned_by !== 'arena' && !(model?.direct ?? false))
		.map((model) => ({ value: model.id, label: model.name, model }));

	$: validPreviews = previews.filter((preview) => !preview.error);

	$: if (!show) reset();

	const reset = () => {
		previews.forEach(revokePreviewImage);
		previews = [];
		baseModelId = null;
		isPublic = false;
		importing = false;
		if (fileInput) fileInput.value = '';
	};

	const addFiles = async (files: FileList | null) => {
		if (!files?.length) return;
		const added = await Promise.all(Array.from(files).map(previewCharacterFile));
		previews = [...previews, ...added];
		fileInput.value = '';
	};

	const removePreview = (index: number) => {
		revokePreviewImage(previews[index]);
		previews = previews.filter((_, i) => i !== index);
	};

	const submitHandler = async () => {
		if (!baseModelId) {
			toast.error($i18n.t('Please select a base model'));
			return;
		}
		if (!validPreviews.length) return;

		importing = true;
		try {
			const created = await importCharacterCards(
				localStorage.token,
				validPreviews.map((preview) => preview.file),
				baseModelId,
				isPublic
			);
			toast.success($i18n.t('Imported {{COUNT}} character(s)', { COUNT: created.length }));
			onImported(created);
			show = false;
		} catch (error: any) {
			toast.error(`${error?.detail ?? error}`);
		} finally {
			importing = false;
		}
	};

	onDestroy(reset);
</script>

{#if $user?.role === 'admin'}
	<Modal size="md" bind:show>
		<div class="px-4 pt-3 pb-4 dark:text-gray-200">
			<div class="flex justify-between items-center pb-2">
				<div class="text-sm font-medium">{$i18n.t('Import Character Cards')}</div>
				<button
					class="self-center rounded-lg p-1 text-gray-500 transition hover:bg-gray-50 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-200"
					aria-label={$i18n.t('Close')}
					on:click={() => (show = false)}
				>
					<XMark className="size-4" />
				</button>
			</div>

			<input
				bind:this={fileInput}
				type="file"
				accept=".json,.png,.charx"
				multiple
				hidden
				on:change={(event) => addFiles(event.currentTarget.files)}
			/>

			<button
				type="button"
				class="w-full rounded-2xl border border-dashed border-gray-300 dark:border-gray-700 px-4 py-5 text-sm text-gray-500 hover:bg-gray-50 dark:hover:bg-gray-850 transition"
				on:click={() => fileInput.click()}
				on:dragover|preventDefault
				on:drop|preventDefault={(event) => addFiles(event.dataTransfer?.files ?? null)}
			>
				{$i18n.t('Choose or drop character cards (.png, .json, .charx)')}
			</button>

			{#if previews.length}
				<div class="mt-3 flex flex-col gap-2 max-h-80 overflow-y-auto">
					{#each previews as preview, index}
						<div class="flex gap-3 items-start rounded-xl px-3 py-2 bg-gray-50 dark:bg-gray-850">
							{#if preview.image}
								<img
									src={preview.image}
									alt={preview.name}
									class="size-12 rounded-lg object-cover shrink-0"
								/>
							{:else}
								<div class="size-12 rounded-lg bg-gray-200 dark:bg-gray-800 shrink-0"></div>
							{/if}

							<div class="flex-1 min-w-0 text-xs">
								<div class="flex items-center gap-2">
									<span class="font-medium text-sm truncate">{preview.name}</span>
									{#if preview.spec}
										<span
											class="shrink-0 rounded-md px-1.5 py-0.5 bg-gray-200 dark:bg-gray-800 text-gray-600 dark:text-gray-400"
										>
											{preview.spec}
										</span>
									{/if}
								</div>

								{#if preview.error}
									<div class="text-red-500 mt-0.5">{preview.error}</div>
								{:else}
									<div class="text-gray-500 line-clamp-2 mt-0.5">
										{preview.creatorNotes || preview.description}
									</div>
									<div class="text-gray-400 mt-0.5">
										{$i18n.t('{{COUNT}} greeting(s)', { COUNT: preview.greetingCount })}
										{#if preview.lorebookEntryCount}
											· {$i18n.t('Embedded lorebook ({{COUNT}} entries)', {
												COUNT: preview.lorebookEntryCount
											})}
										{/if}
									</div>
								{/if}
							</div>

							<button
								class="shrink-0 rounded-lg p-1 text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800"
								aria-label={$i18n.t('Remove')}
								on:click={() => removePreview(index)}
							>
								<XMark className="size-3.5" />
							</button>
						</div>
					{/each}
				</div>
			{/if}

			<div class="mt-4">
				<div class="mb-1 text-xs text-gray-500">{$i18n.t('Base Model (From)')}</div>
				<ModelSelector
					id="character-import-base-model"
					placeholder={$i18n.t('Select a base model (e.g. llama3, gpt-4o)')}
					searchPlaceholder={$i18n.t('Search a model')}
					items={baseModelItems}
					triggerClassName="text-xs"
					selectionOnly
					includeHidden
					bind:value={baseModelId}
				/>
			</div>

			<div class="mt-3 flex items-center justify-between text-xs">
				<div>
					<div class="font-medium">{$i18n.t('Visible to all users')}</div>
					<div class="text-gray-500">
						{$i18n.t('Off keeps the imported characters private; you can share them later.')}
					</div>
				</div>
				<Switch bind:state={isPublic} ariaLabel={$i18n.t('Visible to all users')} />
			</div>

			<div class="flex justify-end pt-4">
				<button
					class="px-3.5 py-1.5 text-sm bg-black hover:bg-gray-900 text-white dark:bg-white dark:text-black dark:hover:bg-gray-100 transition rounded-full flex items-center gap-2 disabled:opacity-50"
					disabled={importing || !validPreviews.length || !baseModelId}
					on:click={submitHandler}
				>
					{#if importing}
						<Spinner className="size-3.5" />
					{/if}
					{$i18n.t('Import {{COUNT}}', { COUNT: validPreviews.length })}
				</button>
			</div>
		</div>
	</Modal>
{/if}
