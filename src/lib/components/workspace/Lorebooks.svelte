<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { toast } from 'svelte-sonner';
	import fileSaver from 'file-saver';
	const { saveAs } = fileSaver;

	import { WEBUI_NAME, user, workspaceActions, workspaceCounts } from '$lib/stores';
	import {
		deleteLorebookById,
		exportLorebook,
		getLorebooks,
		importLorebook,
		type Lorebook
	} from '$lib/apis/lorebooks';

	import ConfirmDialog from '$lib/components/common/ConfirmDialog.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Search from '$lib/components/icons/Search.svelte';
	import GarbageBin from '$lib/components/icons/GarbageBin.svelte';
	import Download from '$lib/components/icons/Download.svelte';
	import Pencil from '$lib/components/icons/Pencil.svelte';

	const i18n: any = getContext('i18n');

	let lorebooks: Lorebook[] | null = null;
	let query = '';
	let searchTimer: ReturnType<typeof setTimeout>;
	let importInput: HTMLInputElement;
	let deleteTarget: Lorebook | null = null;
	let showDeleteConfirm = false;

	$: workspaceActions.set([
		{ id: 'lorebooks-new', label: $i18n.t('Create'), href: '/workspace/lorebooks/create' },
		{
			id: 'lorebooks-import',
			label: $i18n.t('Import Lorebook'),
			onClick: () => importInput?.click(),
			// Admin only, like character card import.
			visible: $user?.role === 'admin'
		}
	]);

	const load = async () => {
		try {
			const res = await getLorebooks(localStorage.token, query, 'write');
			lorebooks = res.items;
			if (!query) workspaceCounts.update((counts) => ({ ...counts, lorebooks: res.total }));
		} catch (error: any) {
			toast.error(`${error?.detail ?? error}`);
			lorebooks = [];
		}
	};

	const importHandler = async (files: FileList | null) => {
		const file = files?.[0];
		if (!file) return;
		try {
			const created = await importLorebook(localStorage.token, file);
			toast.success($i18n.t('Imported "{{NAME}}"', { NAME: created.name }));
			await load();
		} catch (error: any) {
			toast.error(`${error?.detail ?? error}`);
		} finally {
			importInput.value = '';
		}
	};

	const exportHandler = async (lorebook: Lorebook) => {
		try {
			const { blob, filename } = await exportLorebook(localStorage.token, lorebook.id);
			saveAs(blob, filename);
		} catch (error: any) {
			toast.error(`${error?.detail ?? error}`);
		}
	};

	const deleteHandler = async (lorebook: Lorebook) => {
		try {
			await deleteLorebookById(localStorage.token, lorebook.id);
			toast.success($i18n.t('Deleted {{name}}', { name: lorebook.name }));
			await load();
		} catch (error: any) {
			toast.error(`${error?.detail ?? error}`);
		}
	};

	onMount(load);
</script>

<svelte:head>
	<title>{$i18n.t('Lorebooks')} / {$WEBUI_NAME}</title>
</svelte:head>

<ConfirmDialog
	bind:show={showDeleteConfirm}
	title={$i18n.t('Delete lorebook?')}
	message={$i18n.t(
		'Characters and chats that use it will stop receiving its entries. This cannot be undone.'
	)}
	onConfirm={() => deleteTarget && deleteHandler(deleteTarget)}
/>

<input
	bind:this={importInput}
	type="file"
	accept=".json,.png,.charx"
	hidden
	on:change={(event) => importHandler(event.currentTarget.files)}
/>

<div class="space-y-1">
	<div class="flex h-8 items-center w-full">
		<div class="self-center ml-1 mr-3"><Search className="size-3.5" /></div>
		<input
			class="w-full text-sm py-1 outline-hidden bg-transparent"
			bind:value={query}
			aria-label={$i18n.t('Search Lorebooks')}
			placeholder={$i18n.t('Search Lorebooks')}
			maxlength="500"
			on:input={() => {
				clearTimeout(searchTimer);
				searchTimer = setTimeout(load, 300);
			}}
		/>
	</div>

	{#if lorebooks === null}
		<div class="flex justify-center py-10"><Spinner className="size-5" /></div>
	{:else if lorebooks.length === 0}
		<div class="py-12 text-center text-sm text-gray-500">
			<div class="font-medium text-gray-700 dark:text-gray-300">{$i18n.t('No lorebooks yet')}</div>
			<div class="mt-1">
				{$i18n.t(
					'Lorebooks hold world details that are added to the prompt when their keywords come up in a chat.'
				)}
			</div>
		</div>
	{:else}
		<div class="flex flex-col gap-0.5">
			{#each lorebooks as lorebook (lorebook.id)}
				<div
					class="group flex items-center gap-3 rounded-xl px-2 py-2 hover:bg-gray-50 dark:hover:bg-gray-850 transition"
				>
					<button
						class="flex-1 min-w-0 text-left"
						on:click={() => goto(`/workspace/lorebooks/edit?id=${encodeURIComponent(lorebook.id)}`)}
					>
						<div class="flex items-center gap-2">
							<span class="text-sm font-medium truncate">{lorebook.name}</span>
							<span class="shrink-0 text-xs text-gray-400">
								{$i18n.t('{{COUNT}} entries', { COUNT: lorebook.data?.entries?.length ?? 0 })}
							</span>
						</div>
						{#if lorebook.description}
							<div class="text-xs text-gray-500 truncate">{lorebook.description}</div>
						{/if}
						{#if lorebook.user && lorebook.user_id !== $user?.id}
							<div class="text-xs text-gray-400">
								{$i18n.t('By {{name}}', { name: lorebook.user.name })}
							</div>
						{/if}
					</button>

					<div class="flex shrink-0 items-center gap-1 text-gray-500">
						<Tooltip content={$i18n.t('Edit')}>
							<button
								class="p-1.5 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800"
								aria-label={$i18n.t('Edit')}
								on:click={() =>
									goto(`/workspace/lorebooks/edit?id=${encodeURIComponent(lorebook.id)}`)}
							>
								<Pencil className="size-3.5" />
							</button>
						</Tooltip>
						<Tooltip content={$i18n.t('Export')}>
							<button
								class="p-1.5 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800"
								aria-label={$i18n.t('Export')}
								on:click={() => exportHandler(lorebook)}
							>
								<Download className="size-3.5" />
							</button>
						</Tooltip>
						<Tooltip content={$i18n.t('Delete')}>
							<button
								class="p-1.5 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800"
								aria-label={$i18n.t('Delete')}
								on:click={() => {
									deleteTarget = lorebook;
									showDeleteConfirm = true;
								}}
							>
								<GarbageBin className="size-3.5" />
							</button>
						</Tooltip>
					</div>
				</div>
			{/each}
		</div>
	{/if}
</div>
