<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';

	import { WEBUI_NAME } from '$lib/stores';
	import {
		getFinanceAccount,
		getFinanceConnections,
		getFinanceOrders,
		getFinancePositions,
		getFinanceProviders,
		type FinanceAccount,
		type FinanceAsset,
		type FinanceConnection,
		type FinanceOrder,
		type FinancePosition,
		type FinanceProvider
	} from '$lib/apis/finance';
	import type i18nType from '$lib/i18n';

	import Spinner from '$lib/components/common/Spinner.svelte';
	import ConnectionsModal from '$lib/components/markets/ConnectionsModal.svelte';
	import SymbolSearch from '$lib/components/markets/SymbolSearch.svelte';
	import SymbolPanel from '$lib/components/markets/SymbolPanel.svelte';

	const i18n: typeof i18nType = getContext('i18n');

	const SELECTED_CONNECTION_KEY = 'markets.connectionId';

	let loaded = false;
	let providers: FinanceProvider[] = [];
	let connections: FinanceConnection[] = [];
	let connectionId: string | null = null;

	let account: FinanceAccount | null = null;
	let positions: FinancePosition[] = [];
	let orders: FinanceOrder[] = [];
	let loadingAccount = false;
	let accountError: string | null = null;

	let selectedSymbol: string | null = null;
	let selectedName = '';

	let showConnections = false;
	let startInForm = false;

	$: activeConnection = connections.find((c) => c.id === connectionId) ?? null;

	const fmtMoney = (n: number | null | undefined, currency = 'USD') =>
		n === null || n === undefined
			? '—'
			: Number(n).toLocaleString(undefined, {
					style: 'currency',
					currency: currency || 'USD',
					maximumFractionDigits: 2
				});

	const fmtNum = (n: number | null | undefined, digits = 2) =>
		n === null || n === undefined
			? '—'
			: Number(n).toLocaleString(undefined, { maximumFractionDigits: digits });

	const plClass = (n: number | null | undefined) =>
		n === null || n === undefined || n === 0
			? ''
			: n > 0
				? 'text-green-600 dark:text-green-400'
				: 'text-red-600 dark:text-red-400';

	const loadConnections = async () => {
		const res = await getFinanceConnections(localStorage.token).catch((err) => {
			toast.error(`${err}`);
			return null;
		});
		connections = res ?? [];

		if (!connections.find((c) => c.id === connectionId)) {
			let stored: string | null = null;
			try {
				stored = localStorage.getItem(SELECTED_CONNECTION_KEY);
			} catch {
				// storage unavailable
			}
			connectionId =
				connections.find((c) => c.id === stored)?.id ??
				connections.find((c) => c.is_default)?.id ??
				connections[0]?.id ??
				null;
		}
	};

	const loadAccountData = async (connId: string | null) => {
		if (!connId) {
			account = null;
			positions = [];
			orders = [];
			return;
		}
		loadingAccount = true;
		accountError = null;
		const [a, p, o] = await Promise.all([
			getFinanceAccount(localStorage.token, connId).catch((err) => {
				accountError = `${err}`;
				return null;
			}),
			getFinancePositions(localStorage.token, connId).catch(() => null),
			getFinanceOrders(localStorage.token, 'open', 50, connId).catch(() => null)
		]);
		if (connId !== connectionId) return;
		account = a;
		positions = p ?? [];
		orders = o ?? [];
		loadingAccount = false;
	};

	$: if (loaded) {
		try {
			if (connectionId) localStorage.setItem(SELECTED_CONNECTION_KEY, connectionId);
		} catch {
			// storage unavailable
		}
		loadAccountData(connectionId);
	}

	const selectAsset = (asset: FinanceAsset) => {
		selectedSymbol = asset.symbol;
		selectedName = asset.name;
	};

	onMount(async () => {
		const [p] = await Promise.all([
			getFinanceProviders(localStorage.token).catch((err) => {
				toast.error(`${err}`);
				return null;
			}),
			loadConnections()
		]);
		providers = p ?? [];
		loaded = true;
	});
</script>

<svelte:head>
	<!-- LICENSE covers this Open WebUI browser-title identifier.
	Do not alter, remove, obscure, or replace it except as LICENSE permits:
	https://docs.openwebui.com/license. -->
	<title>{$i18n.t('Markets')} / {$WEBUI_NAME}</title>
</svelte:head>

<ConnectionsModal
	bind:show={showConnections}
	{providers}
	{connections}
	{startInForm}
	on:change={async (e) => {
		await loadConnections();
		if (e.detail?.id && (connections.length === 1 || e.detail.is_default)) {
			connectionId = e.detail.id;
		}
		loadAccountData(connectionId);
	}}
/>

<div class="h-full overflow-y-auto px-2.5 pb-4">
	{#if !loaded}
		<div class="w-full h-full flex justify-center items-center">
			<Spinner className="size-5" />
		</div>
	{:else if connections.length === 0}
		<div class="flex min-h-[calc(100dvh-8rem)] w-full flex-col items-center justify-center">
			<div class="max-w-sm text-center text-gray-900 dark:text-gray-100">
				<div class="mb-1.5 text-sm">{$i18n.t('No market connections')}</div>
				<div class="text-center text-xs leading-5 text-gray-500 mb-4">
					{$i18n.t(
						'Connect a brokerage account to view quotes, charts, positions and orders. Paper trading is enabled by default.'
					)}
				</div>
				<button
					class="px-3.5 py-1.5 text-sm font-medium bg-black hover:bg-gray-900 text-white dark:bg-white dark:text-black dark:hover:bg-gray-100 transition rounded-full"
					type="button"
					on:click={() => {
						startInForm = true;
						showConnections = true;
					}}
				>
					{$i18n.t('Add connection')}
				</button>
			</div>
		</div>
	{:else}
		<div class="flex flex-col gap-3 max-w-6xl mx-auto">
			<div class="flex flex-wrap items-center gap-2 pt-1">
				<select
					class="rounded-xl px-2.5 h-8 text-sm bg-gray-50 dark:bg-gray-850 outline-hidden max-w-60"
					bind:value={connectionId}
					aria-label={$i18n.t('Connection')}
				>
					{#each connections as c (c.id)}
						<option value={c.id}>{c.name}</option>
					{/each}
				</select>

				{#if activeConnection}
					<span
						class="rounded-md px-1.5 py-0.5 text-[0.6875rem] font-semibold {activeConnection.is_paper
							? 'bg-blue-500/15 text-blue-600 dark:text-blue-400'
							: 'bg-red-500/15 text-red-600 dark:text-red-400'}"
					>
						{activeConnection.is_paper ? $i18n.t('PAPER') : $i18n.t('LIVE')}
					</span>
				{/if}

				<div class="flex-1"></div>

				<button
					class="px-3 h-8 text-[0.8125rem] rounded-xl hover:bg-gray-50 dark:hover:bg-gray-850 transition"
					type="button"
					on:click={() => {
						startInForm = false;
						showConnections = true;
					}}
				>
					{$i18n.t('Connections')}
				</button>
			</div>

			<!-- Account summary -->
			<div class="grid grid-cols-1 sm:grid-cols-3 gap-2">
				{#each [{ label: $i18n.t('Equity'), value: account?.equity }, { label: $i18n.t('Cash'), value: account?.cash }, { label: $i18n.t('Buying Power'), value: account?.buying_power }] as stat}
					<div class="rounded-2xl border border-gray-100 dark:border-gray-850 px-3 py-2">
						<div class="text-xs text-gray-500">{stat.label}</div>
						<div class="text-lg font-semibold">
							{#if loadingAccount && !account}
								<Spinner className="size-4" />
							{:else}
								{fmtMoney(stat.value, account?.currency)}
							{/if}
						</div>
					</div>
				{/each}
			</div>
			{#if accountError}
				<div class="text-xs text-red-500">{accountError}</div>
			{/if}

			<!-- Symbol search + chart -->
			<SymbolSearch on:select={(e) => selectAsset(e.detail)} />
			{#if selectedSymbol}
				<SymbolPanel symbol={selectedSymbol} name={selectedName} {connectionId} />
			{/if}

			<!-- Positions -->
			<div class="rounded-2xl border border-gray-100 dark:border-gray-850 p-3">
				<div class="text-sm font-medium mb-2">{$i18n.t('Positions')}</div>
				{#if positions.length === 0}
					<div class="text-xs text-gray-500">{$i18n.t('No open positions')}</div>
				{:else}
					<div class="overflow-x-auto">
						<table class="w-full text-sm text-left">
							<thead class="text-xs text-gray-500">
								<tr>
									<th class="py-1 pr-3 font-normal">{$i18n.t('Symbol')}</th>
									<th class="py-1 pr-3 font-normal text-right">{$i18n.t('Qty')}</th>
									<th class="py-1 pr-3 font-normal text-right">{$i18n.t('Avg Entry')}</th>
									<th class="py-1 pr-3 font-normal text-right">{$i18n.t('Price')}</th>
									<th class="py-1 pr-3 font-normal text-right">{$i18n.t('Market Value')}</th>
									<th class="py-1 font-normal text-right">{$i18n.t('Unrealized P/L')}</th>
								</tr>
							</thead>
							<tbody>
								{#each positions as p (p.symbol)}
									<tr
										class="border-t border-gray-100 dark:border-gray-850 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-850/50"
										on:click={() => {
											selectedSymbol = p.symbol;
											selectedName = '';
										}}
									>
										<td class="py-1.5 pr-3 font-medium">{p.symbol}</td>
										<td class="py-1.5 pr-3 text-right">{fmtNum(p.qty, 4)}</td>
										<td class="py-1.5 pr-3 text-right">{fmtMoney(p.avg_entry_price)}</td>
										<td class="py-1.5 pr-3 text-right">{fmtMoney(p.current_price)}</td>
										<td class="py-1.5 pr-3 text-right">{fmtMoney(p.market_value)}</td>
										<td class="py-1.5 text-right {plClass(p.unrealized_pl)}">
											{fmtMoney(p.unrealized_pl)}
											{#if p.unrealized_plpc !== null && p.unrealized_plpc !== undefined}
												<span class="text-xs">({fmtNum(p.unrealized_plpc * 100)}%)</span>
											{/if}
										</td>
									</tr>
								{/each}
							</tbody>
						</table>
					</div>
				{/if}
			</div>

			<!-- Open orders -->
			<div class="rounded-2xl border border-gray-100 dark:border-gray-850 p-3">
				<div class="text-sm font-medium mb-2">{$i18n.t('Open Orders')}</div>
				{#if orders.length === 0}
					<div class="text-xs text-gray-500">{$i18n.t('No open orders')}</div>
				{:else}
					<div class="overflow-x-auto">
						<table class="w-full text-sm text-left">
							<thead class="text-xs text-gray-500">
								<tr>
									<th class="py-1 pr-3 font-normal">{$i18n.t('Symbol')}</th>
									<th class="py-1 pr-3 font-normal">{$i18n.t('Side')}</th>
									<th class="py-1 pr-3 font-normal">{$i18n.t('Type')}</th>
									<th class="py-1 pr-3 font-normal text-right">{$i18n.t('Qty')}</th>
									<th class="py-1 pr-3 font-normal text-right">{$i18n.t('Filled')}</th>
									<th class="py-1 pr-3 font-normal text-right">{$i18n.t('Limit')}</th>
									<th class="py-1 pr-3 font-normal text-right">{$i18n.t('Stop')}</th>
									<th class="py-1 pr-3 font-normal">{$i18n.t('Status')}</th>
									<th class="py-1 font-normal">{$i18n.t('Submitted')}</th>
								</tr>
							</thead>
							<tbody>
								{#each orders as o (o.id)}
									<tr class="border-t border-gray-100 dark:border-gray-850">
										<td class="py-1.5 pr-3 font-medium">{o.symbol}</td>
										<td
											class="py-1.5 pr-3 {o.side === 'buy'
												? 'text-green-600 dark:text-green-400'
												: 'text-red-600 dark:text-red-400'}">{o.side}</td
										>
										<td class="py-1.5 pr-3">{o.type}</td>
										<td class="py-1.5 pr-3 text-right">{fmtNum(o.qty, 4)}</td>
										<td class="py-1.5 pr-3 text-right">{fmtNum(o.filled_qty, 4)}</td>
										<td class="py-1.5 pr-3 text-right">{fmtMoney(o.limit_price)}</td>
										<td class="py-1.5 pr-3 text-right">{fmtMoney(o.stop_price)}</td>
										<td class="py-1.5 pr-3">{o.status}</td>
										<td class="py-1.5 text-xs text-gray-500">
											{o.submitted_at ? new Date(o.submitted_at).toLocaleString() : '—'}
										</td>
									</tr>
								{/each}
							</tbody>
						</table>
					</div>
				{/if}
			</div>
		</div>
	{/if}
</div>
