<script lang="ts">
	import { createEventDispatcher, getContext } from 'svelte';
	import { toast } from 'svelte-sonner';

	import {
		createFinanceConnection,
		deleteFinanceConnectionById,
		testFinanceConnectionById,
		updateFinanceConnectionById,
		type FinanceConnection,
		type FinanceProvider
	} from '$lib/apis/finance';
	import type i18nType from '$lib/i18n';

	import Modal from '$lib/components/common/Modal.svelte';
	import ConfirmDialog from '$lib/components/common/ConfirmDialog.svelte';
	import Switch from '$lib/components/common/Switch.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';

	const i18n: typeof i18nType = getContext('i18n');
	const dispatch = createEventDispatcher();

	export let show = false;
	export let providers: FinanceProvider[] = [];
	export let connections: FinanceConnection[] = [];
	/** Open directly into the "add" form. */
	export let startInForm = false;

	let mode: 'list' | 'form' = 'list';
	let editing: FinanceConnection | null = null;

	let name = '';
	let providerId = '';
	let isPaper = true;
	let isDefault = false;
	let credentials: Record<string, string> = {};
	let saving = false;
	let testingId: string | null = null;

	let showDeleteConfirm = false;
	let deleteTarget: FinanceConnection | null = null;

	$: provider = providers.find((p) => p.id === providerId) ?? null;

	let wasShown = false;
	$: if (show && !wasShown) {
		wasShown = true;
		if (startInForm || connections.length === 0) {
			openForm(null);
		} else {
			mode = 'list';
		}
	} else if (!show && wasShown) {
		wasShown = false;
	}

	const openForm = (connection: FinanceConnection | null) => {
		editing = connection;
		name = connection?.name ?? '';
		providerId = connection?.provider ?? providers[0]?.id ?? '';
		isPaper = connection?.is_paper ?? true;
		isDefault = connection?.is_default ?? connections.length === 0;
		credentials = {};
		mode = 'form';
	};

	const providerName = (id: string) => providers.find((p) => p.id === id)?.name ?? id;

	const submitHandler = async () => {
		if (!provider) {
			toast.error($i18n.t('Please select a provider'));
			return;
		}

		const filled = Object.fromEntries(
			Object.entries(credentials).filter(([, v]) => (v ?? '').trim() !== '')
		);
		const hasNewCredentials = Object.keys(filled).length > 0;

		if (!editing || hasNewCredentials) {
			const missing = provider.credential_fields.find(
				(f) => f.required && !(filled[f.name] ?? '').trim()
			);
			if (missing) {
				toast.error($i18n.t('{{field}} is required', { field: missing.label }));
				return;
			}
		}

		const form = {
			name: name.trim() || provider.name,
			provider: provider.id,
			kind: editing?.kind ?? provider.kinds[0] ?? 'broker',
			is_paper: provider.supports_paper ? isPaper : false,
			is_default: isDefault,
			credentials: hasNewCredentials ? filled : null,
			meta: editing?.meta ?? undefined
		};

		saving = true;
		const res = await (
			editing
				? updateFinanceConnectionById(localStorage.token, editing.id, form)
				: createFinanceConnection(localStorage.token, form)
		).catch((err) => {
			toast.error(`${err}`);
			return null;
		});
		saving = false;

		if (res) {
			toast.success(editing ? $i18n.t('Connection updated') : $i18n.t('Connection added'));
			credentials = {};
			dispatch('change', res);
			mode = 'list';
			editing = null;
		}
	};

	const testHandler = async (connection: FinanceConnection) => {
		testingId = connection.id;
		const res = await testFinanceConnectionById(localStorage.token, connection.id).catch((err) => {
			toast.error(`${err}`);
			return null;
		});
		testingId = null;

		if (res?.ok) {
			const equity = res.account?.equity;
			toast.success(
				equity !== undefined && equity !== null
					? $i18n.t('Connection verified. Equity: {{equity}} {{currency}}', {
							equity: Number(equity).toLocaleString(undefined, { maximumFractionDigits: 2 }),
							currency: res.account?.currency ?? ''
						})
					: $i18n.t('Connection verified')
			);
		} else if (res) {
			toast.error(res.error || $i18n.t('Connection failed'));
		}
	};

	const deleteHandler = async (connection: FinanceConnection) => {
		const res = await deleteFinanceConnectionById(localStorage.token, connection.id).catch(
			(err) => {
				toast.error(`${err}`);
				return null;
			}
		);
		if (res) {
			toast.success($i18n.t(`Deleted {{name}}`, { name: connection.name }));
			dispatch('change', null);
		}
	};
</script>

<ConfirmDialog
	bind:show={showDeleteConfirm}
	title={$i18n.t('Delete connection?')}
	on:confirm={() => {
		if (deleteTarget) deleteHandler(deleteTarget);
	}}
>
	<div class="text-sm text-gray-500 truncate">
		{$i18n.t('This will delete')} <span class="font-normal">{deleteTarget?.name}</span>.
	</div>
</ConfirmDialog>

<Modal size="md" bind:show>
	<div class="px-5 pt-4 pb-5 dark:text-gray-200">
		<div class="flex items-center justify-between mb-3">
			<div class="text-base font-medium">
				{#if mode === 'form'}
					{editing ? $i18n.t('Edit Connection') : $i18n.t('Add Connection')}
				{:else}
					{$i18n.t('Market Connections')}
				{/if}
			</div>
			<button
				class="p-1 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-850 transition"
				type="button"
				aria-label={$i18n.t('Close')}
				on:click={() => (show = false)}
			>
				<XMark className="size-4" />
			</button>
		</div>

		{#if mode === 'list'}
			<div class="flex flex-col gap-1">
				{#each connections as connection (connection.id)}
					<div
						class="flex items-center gap-2 rounded-xl px-3 py-2 border border-gray-100 dark:border-gray-850"
					>
						<div class="flex min-w-0 flex-1 flex-col">
							<div class="flex items-center gap-1.5 text-sm truncate">
								<span class="truncate">{connection.name}</span>
								<span
									class="shrink-0 rounded-md px-1.5 text-[0.625rem] font-semibold {connection.is_paper
										? 'bg-blue-500/15 text-blue-600 dark:text-blue-400'
										: 'bg-red-500/15 text-red-600 dark:text-red-400'}"
								>
									{connection.is_paper ? $i18n.t('PAPER') : $i18n.t('LIVE')}
								</span>
								{#if connection.is_default}
									<span class="shrink-0 text-[0.6875rem] text-gray-500">
										{$i18n.t('Default')}
									</span>
								{/if}
							</div>
							<div class="text-xs text-gray-500 truncate">
								{providerName(connection.provider)}
								{#if connection.key_hint}
									· {connection.key_hint}
								{/if}
								{#if !connection.has_credentials}
									· {$i18n.t('No credentials')}
								{/if}
							</div>
						</div>

						<div class="flex shrink-0 items-center gap-1 text-xs">
							<button
								class="px-2 py-1 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-850 transition flex items-center gap-1"
								type="button"
								disabled={testingId === connection.id}
								on:click={() => testHandler(connection)}
							>
								{#if testingId === connection.id}
									<Spinner className="size-3" />
								{/if}
								{$i18n.t('Test')}
							</button>
							<button
								class="px-2 py-1 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-850 transition"
								type="button"
								on:click={() => openForm(connection)}
							>
								{$i18n.t('Edit')}
							</button>
							<button
								class="px-2 py-1 rounded-lg text-red-500 hover:bg-gray-100 dark:hover:bg-gray-850 transition"
								type="button"
								on:click={() => {
									deleteTarget = connection;
									showDeleteConfirm = true;
								}}
							>
								{$i18n.t('Delete')}
							</button>
						</div>
					</div>
				{:else}
					<div class="text-sm text-gray-500 py-4 text-center">
						{$i18n.t('No connections yet.')}
					</div>
				{/each}
			</div>

			<div class="flex justify-end mt-4">
				<button
					class="px-3.5 py-1.5 text-sm font-medium bg-black hover:bg-gray-900 text-white dark:bg-white dark:text-black dark:hover:bg-gray-100 transition rounded-full"
					type="button"
					on:click={() => openForm(null)}
				>
					{$i18n.t('Add Connection')}
				</button>
			</div>
		{:else}
			<form class="flex flex-col gap-3" on:submit|preventDefault={submitHandler}>
				<div class="flex flex-col gap-1">
					<label class="text-xs text-gray-500" for="finance-provider">{$i18n.t('Provider')}</label>
					<select
						id="finance-provider"
						class="w-full rounded-lg px-3 py-2 text-sm bg-gray-50 dark:bg-gray-850 outline-hidden"
						bind:value={providerId}
						disabled={!!editing}
						on:change={() => (credentials = {})}
					>
						{#each providers as p (p.id)}
							<option value={p.id}>{p.name}</option>
						{/each}
					</select>
				</div>

				<div class="flex flex-col gap-1">
					<label class="text-xs text-gray-500" for="finance-name">{$i18n.t('Name')}</label>
					<input
						id="finance-name"
						class="w-full rounded-lg px-3 py-2 text-sm bg-gray-50 dark:bg-gray-850 outline-hidden"
						bind:value={name}
						placeholder={provider?.name ?? $i18n.t('Connection name')}
						autocomplete="off"
					/>
				</div>

				{#if provider}
					{#each provider.credential_fields as field (field.name)}
						<div class="flex flex-col gap-1">
							<label class="text-xs text-gray-500" for="finance-cred-{field.name}">
								{field.label}{field.required && !editing ? ' *' : ''}
							</label>
							{#if field.secret}
								<input
									id="finance-cred-{field.name}"
									class="w-full rounded-lg px-3 py-2 text-sm bg-gray-50 dark:bg-gray-850 outline-hidden"
									type="password"
									autocomplete="new-password"
									placeholder={editing?.has_credentials
										? $i18n.t('Leave blank to keep existing')
										: ''}
									bind:value={credentials[field.name]}
								/>
							{:else}
								<input
									id="finance-cred-{field.name}"
									class="w-full rounded-lg px-3 py-2 text-sm bg-gray-50 dark:bg-gray-850 outline-hidden"
									type="text"
									autocomplete="off"
									placeholder={editing?.has_credentials
										? $i18n.t('Leave blank to keep existing')
										: ''}
									bind:value={credentials[field.name]}
								/>
							{/if}
						</div>
					{/each}

					{#if provider.supports_paper}
						<div class="flex items-center justify-between">
							<div class="flex flex-col">
								<div class="text-sm">{$i18n.t('Paper trading')}</div>
								<div class="text-xs text-gray-500">
									{isPaper
										? $i18n.t('Simulated account, no real money.')
										: $i18n.t('Live account: real money is at risk.')}
								</div>
							</div>
							<Switch bind:state={isPaper} />
						</div>
					{/if}
				{/if}

				<div class="flex items-center justify-between">
					<div class="text-sm">{$i18n.t('Default connection')}</div>
					<Switch bind:state={isDefault} />
				</div>

				<div class="flex justify-end gap-2 mt-2">
					{#if connections.length > 0}
						<button
							class="px-3.5 py-1.5 text-sm rounded-full hover:bg-gray-100 dark:hover:bg-gray-850 transition"
							type="button"
							on:click={() => {
								mode = 'list';
								editing = null;
							}}
						>
							{$i18n.t('Cancel')}
						</button>
					{/if}
					<button
						class="px-3.5 py-1.5 text-sm font-medium bg-black hover:bg-gray-900 text-white dark:bg-white dark:text-black dark:hover:bg-gray-100 transition rounded-full flex items-center gap-1.5"
						type="submit"
						disabled={saving}
					>
						{#if saving}
							<Spinner className="size-3" />
						{/if}
						{$i18n.t('Save')}
					</button>
				</div>
			</form>
		{/if}
	</div>
</Modal>
