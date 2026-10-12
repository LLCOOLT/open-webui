<script lang="ts">
	import { getContext } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import { toast } from 'svelte-sonner';

	import { WEBUI_NAME } from '$lib/stores';
	import {
		deleteFinanceStrategyById,
		getFinanceBacktestById,
		getFinanceBacktests,
		getFinanceStrategyById,
		getFinanceStrategyVersions,
		runFinanceBacktest,
		updateFinanceStrategyById,
		validateFinanceStrategy,
		type FinanceBacktest,
		type FinanceStrategy,
		type FinanceStrategyForm,
		type FinanceStrategyVersion,
		type FinanceTimeframe
	} from '$lib/apis/finance';
	import type i18nType from '$lib/i18n';

	import Spinner from '$lib/components/common/Spinner.svelte';
	import CodeEditor from '$lib/components/common/CodeEditor.svelte';
	import ConfirmDialog from '$lib/components/common/ConfirmDialog.svelte';
	import {
		fmtNs,
		fmtNumber,
		fmtPct,
		signClass,
		statusClass
	} from '$lib/components/markets/backtestFormat';

	const i18n: typeof i18nType = getContext('i18n');

	const TIMEFRAMES: FinanceTimeframe[] = ['1Min', '5Min', '15Min', '1Hour', '1Day', '1Week'];

	$: strategyId = $page.params.id ?? '';

	let loaded = false;
	let strategy: FinanceStrategy | null = null;
	let versions: FinanceStrategyVersion[] = [];
	let backtests: FinanceBacktest[] = [];

	// form state
	let name = '';
	let description = '';
	let timeframe: FinanceTimeframe = '1Day';
	let universeText = '';
	let paramRows: { key: string; value: string }[] = [];
	let code = '';
	let editorKey = 0;
	let selectedVersion = '';

	// CodeEditor types onChange as () => void but calls it with the new value
	const onCodeChange = ((value: string) => {
		code = value;
	}) as unknown as () => void;

	let saving = false;
	let validating = false;
	let showDeleteConfirm = false;

	// backtest form
	const today = new Date();
	const isoDate = (d: Date) => d.toISOString().slice(0, 10);
	const fiveYearsAgo = new Date(today);
	fiveYearsAgo.setFullYear(today.getFullYear() - 5);

	let btSymbols = '';
	let btStart = isoDate(fiveYearsAgo);
	let btEnd = isoDate(today);
	let btCash = 10000;
	let btCommission = 0;
	let btSlippage = 5;
	let gridRows: { key: string; values: string }[] = [];
	let running = false;

	$: isPython = strategy?.kind === 'python';

	const parseSymbols = (text: string) =>
		text
			.split(/[,\s]+/)
			.map((s) => s.trim().toUpperCase())
			.filter(Boolean);

	const paramsFromRows = (): Record<string, number> | null => {
		const out: Record<string, number> = {};
		for (const row of paramRows) {
			const key = row.key.trim();
			if (!key) continue;
			const n = Number(row.value);
			if (row.value === '' || Number.isNaN(n)) {
				toast.error($i18n.t('Parameter "{{key}}" must be a number', { key }));
				return null;
			}
			out[key] = n;
		}
		return out;
	};

	const loadFields = (
		def: Record<string, unknown> | null,
		source: string | null,
		params: Record<string, number>
	) => {
		code = strategy?.kind === 'python' ? (source ?? '') : JSON.stringify(def ?? {}, null, 2);
		paramRows = Object.entries(params ?? {}).map(([key, value]) => ({
			key,
			value: `${value}`
		}));
		editorKey += 1;
	};

	const applyStrategy = (s: FinanceStrategy) => {
		strategy = s;
		name = s.name;
		description = s.description ?? '';
		timeframe = s.timeframe ?? '1Day';
		universeText = (s.universe ?? []).join(', ');
		loadFields(s.definition, s.source, s.params);
		selectedVersion = '';
	};

	const buildForm = (): FinanceStrategyForm | null => {
		if (!strategy) return null;
		const params = paramsFromRows();
		if (!params) return null;
		const form: FinanceStrategyForm = {
			name: name.trim() || strategy.name,
			description,
			kind: strategy.kind,
			params,
			timeframe,
			universe: parseSymbols(universeText),
			meta: strategy.meta ?? null
		};
		if (isPython) {
			form.source = code;
		} else {
			try {
				form.definition = JSON.parse(code);
			} catch (e) {
				toast.error($i18n.t('Definition is not valid JSON: {{error}}', { error: `${e}` }));
				return null;
			}
		}
		return form;
	};

	const validate = async () => {
		const form = buildForm();
		if (!form) return;
		validating = true;
		const res = await validateFinanceStrategy(localStorage.token, {
			kind: form.kind,
			definition: form.definition,
			source: form.source,
			params: form.params
		}).catch((err) => {
			toast.error(`${err}`);
			return null;
		});
		validating = false;
		if (res?.ok) toast.success($i18n.t('Strategy is valid'));
		else if (res) toast.error(res.error ?? $i18n.t('Strategy is invalid'));
	};

	const save = async () => {
		const form = buildForm();
		if (!form || !strategy) return;
		saving = true;
		const res = await updateFinanceStrategyById(localStorage.token, strategy.id, form).catch(
			(err) => {
				toast.error(`${err}`);
				return null;
			}
		);
		saving = false;
		if (res) {
			applyStrategy(res);
			toast.success($i18n.t('Saved version {{version}}', { version: res.version }));
			loadVersions();
		}
	};

	const deleteStrategy = async () => {
		if (!strategy) return;
		const res = await deleteFinanceStrategyById(localStorage.token, strategy.id).catch((err) => {
			toast.error(`${err}`);
			return null;
		});
		if (res) {
			toast.success($i18n.t('Strategy deleted'));
			goto('/markets/strategies');
		}
	};

	const loadVersion = (v: string) => {
		const ver = versions.find((x) => `${x.version}` === v);
		if (!ver) return;
		loadFields(ver.definition, ver.source, ver.params);
		toast.info(
			$i18n.t('Loaded version {{version}} into the editor (not saved)', { version: ver.version })
		);
	};

	const loadVersions = async () => {
		if (!strategy) return;
		versions =
			(await getFinanceStrategyVersions(localStorage.token, strategy.id).catch(() => null)) ?? [];
		versions = [...versions].sort((a, b) => b.version - a.version);
	};

	const loadBacktests = async () => {
		if (!strategy) return;
		backtests =
			(await getFinanceBacktests(localStorage.token, strategy.id, 10).catch(() => null)) ?? [];
	};

	const runBacktest = async () => {
		if (!strategy) return;
		const params = paramsFromRows();
		if (!params) return;
		const symbols = parseSymbols(btSymbols);
		if (symbols.length === 0) {
			toast.error($i18n.t('Enter at least one symbol'));
			return;
		}
		let grid: Record<string, number[]> | null = null;
		for (const row of gridRows) {
			const key = row.key.trim();
			if (!key) continue;
			const values = row.values
				.split(',')
				.map((v) => v.trim())
				.filter(Boolean)
				.map(Number);
			if (values.length === 0 || values.some((v) => Number.isNaN(v))) {
				toast.error($i18n.t('Grid values for "{{key}}" must be numbers', { key }));
				return;
			}
			grid = { ...(grid ?? {}), [key]: values };
		}

		running = true;
		const res = await runFinanceBacktest(localStorage.token, {
			strategy_id: strategy.id,
			symbols,
			timeframe,
			start: btStart || null,
			end: btEnd || null,
			params,
			grid,
			initial_cash: Number(btCash),
			commission_bps: Number(btCommission),
			slippage_bps: Number(btSlippage)
		}).catch((err) => {
			toast.error(`${err}`);
			return null;
		});
		running = false;
		if (res) goto(`/markets/backtests/${res.id}`);
	};

	const askModel = async () => {
		if (!strategy) return;
		const params = paramsFromRows() ?? strategy.params;
		const latest = backtests.find((b) => b.status === 'completed');
		let full: FinanceBacktest | null = null;
		if (latest) {
			full = await getFinanceBacktestById(localStorage.token, latest.id).catch(() => null);
		}
		const bt = full ?? latest ?? null;

		const lines = [
			`Please critique my trading strategy "${name || strategy.name}" and suggest concrete improvements.`,
			'',
			`Kind: ${strategy.kind}`,
			`Timeframe: ${timeframe}`,
			`Universe: ${parseSymbols(universeText).join(', ') || '(none)'}`,
			`Params: ${JSON.stringify(params)}`,
			'',
			isPython ? 'Source (Python):' : 'Definition (JSON):',
			'```' + (isPython ? 'python' : 'json'),
			code,
			'```'
		];
		if (bt) {
			const cfg = bt.config;
			lines.push(
				'',
				`Latest completed backtest: ${cfg?.symbols?.join(', ')} from ${cfg?.start ?? '?'} to ${cfg?.end ?? '?'}, commission ${cfg?.commission_bps} bps, slippage ${cfg?.slippage_bps} bps.`,
				`Strategy metrics: ${JSON.stringify(bt.metrics)}`,
				`Buy-and-hold benchmark metrics: ${JSON.stringify(bt.benchmark)}`
			);
		}
		lines.push(
			'',
			'Compare the results against simply buying and holding. Be wary of overfitting: prefer simple, robust rules, suggest out-of-sample / walk-forward checks, and do not just tune parameters to the backtest period. ' +
				(isPython
					? 'Return any revised code as a `def signals(df, params)` function returning (entries, exits) boolean pandas Series.'
					: 'Return any revised definition in the same JSON template format ("$name" refers to params).')
		);
		goto(`/?q=${encodeURIComponent(lines.join('\n'))}`);
	};

	const load = async (id: string) => {
		loaded = false;
		const s = await getFinanceStrategyById(localStorage.token, id).catch((err) => {
			toast.error(`${err}`);
			return null;
		});
		if (!s) {
			goto('/markets/strategies');
			return;
		}
		applyStrategy(s);
		btSymbols = (s.universe ?? []).join(', ');
		loaded = true;
		loadVersions();
		loadBacktests();
	};

	let loadedId = '';
	$: if (strategyId && strategyId !== loadedId) {
		loadedId = strategyId;
		load(strategyId);
	}
</script>

<svelte:head>
	<title>{strategy?.name ?? $i18n.t('Strategy')} / {$WEBUI_NAME}</title>
</svelte:head>

<ConfirmDialog
	bind:show={showDeleteConfirm}
	title={$i18n.t('Delete strategy?')}
	message={$i18n.t('This will permanently delete {{name}}.', { name: strategy?.name ?? '' })}
	on:confirm={deleteStrategy}
/>

<div class="h-full overflow-y-auto px-2.5 pb-6">
	{#if !loaded || !strategy}
		<div class="flex justify-center py-10"><Spinner className="size-5" /></div>
	{:else}
		<div class="flex flex-col gap-4 max-w-6xl mx-auto pt-1">
			<div class="flex flex-wrap items-center gap-2">
				<a href="/markets/strategies" class="text-sm text-gray-500 hover:underline"
					>{$i18n.t('Strategies')}</a
				>
				<span class="text-gray-400">/</span>
				<span class="text-sm font-medium truncate">{strategy.name}</span>
				<span class="text-xs text-gray-500">v{strategy.version}</span>
				{#if isPython}
					<span
						class="rounded-full px-2 py-0.5 text-xs bg-yellow-500/15 text-yellow-700 dark:text-yellow-300"
						>{$i18n.t('Python')}</span
					>
				{/if}
				<div class="flex-1"></div>
				{#if versions.length > 0}
					<select
						class="rounded-xl px-2.5 h-8 text-sm bg-gray-50 dark:bg-gray-850 outline-hidden"
						bind:value={selectedVersion}
						on:change={() => loadVersion(selectedVersion)}
						aria-label={$i18n.t('Version history')}
					>
						<option value="" disabled>{$i18n.t('Version history')}</option>
						{#each versions as v (v.version)}
							<option value={`${v.version}`}>v{v.version} · {fmtNs(v.created_at)}</option>
						{/each}
					</select>
				{/if}
				<button
					class="px-3 h-8 text-sm rounded-full bg-gray-50 hover:bg-gray-100 dark:bg-gray-850 dark:hover:bg-gray-800 transition"
					type="button"
					on:click={askModel}>{$i18n.t('Ask the model to improve')}</button
				>
				<button
					class="px-3 h-8 text-sm rounded-full bg-gray-50 hover:bg-gray-100 dark:bg-gray-850 dark:hover:bg-gray-800 transition disabled:opacity-50"
					type="button"
					disabled={validating}
					on:click={validate}>{$i18n.t('Validate')}</button
				>
				<button
					class="px-3 h-8 text-sm rounded-full text-red-600 bg-gray-50 hover:bg-gray-100 dark:bg-gray-850 dark:hover:bg-gray-800 transition"
					type="button"
					on:click={() => (showDeleteConfirm = true)}>{$i18n.t('Delete')}</button
				>
				<button
					class="px-3.5 h-8 text-sm font-medium bg-black hover:bg-gray-900 text-white dark:bg-white dark:text-black dark:hover:bg-gray-100 transition rounded-full disabled:opacity-50"
					type="button"
					disabled={saving}
					on:click={save}>{$i18n.t('Save')}</button
				>
			</div>

			<div class="grid grid-cols-1 lg:grid-cols-3 gap-4">
				<div class="lg:col-span-2 flex flex-col gap-3">
					<div class="grid grid-cols-1 md:grid-cols-2 gap-3">
						<label class="flex flex-col gap-1 text-xs text-gray-500">
							{$i18n.t('Name')}
							<input
								class="rounded-xl px-3 py-1.5 text-sm bg-gray-50 dark:bg-gray-850 text-gray-900 dark:text-gray-100 outline-hidden"
								bind:value={name}
							/>
						</label>
						<div class="grid grid-cols-2 gap-3">
							<label class="flex flex-col gap-1 text-xs text-gray-500">
								{$i18n.t('Timeframe')}
								<select
									class="rounded-xl px-2.5 py-1.5 text-sm bg-gray-50 dark:bg-gray-850 text-gray-900 dark:text-gray-100 outline-hidden"
									bind:value={timeframe}
								>
									{#each TIMEFRAMES as tf (tf)}
										<option value={tf}>{tf}</option>
									{/each}
								</select>
							</label>
							<label class="flex flex-col gap-1 text-xs text-gray-500">
								{$i18n.t('Universe')}
								<input
									class="rounded-xl px-3 py-1.5 text-sm bg-gray-50 dark:bg-gray-850 text-gray-900 dark:text-gray-100 outline-hidden"
									placeholder="SPY, QQQ"
									bind:value={universeText}
								/>
							</label>
						</div>
					</div>
					<label class="flex flex-col gap-1 text-xs text-gray-500">
						{$i18n.t('Description')}
						<textarea
							class="rounded-xl px-3 py-1.5 text-sm bg-gray-50 dark:bg-gray-850 text-gray-900 dark:text-gray-100 outline-hidden resize-y"
							rows="2"
							bind:value={description}
						></textarea>
					</label>

					<div class="flex flex-col gap-1">
						<div class="text-xs text-gray-500">
							{isPython ? $i18n.t('Source (Python)') : $i18n.t('Definition (JSON)')}
						</div>
						<div
							class="rounded-xl border border-gray-100 dark:border-gray-850 overflow-hidden min-h-[24rem]"
						>
							{#key editorKey}
								<CodeEditor
									id="strategy-{strategy.id}"
									value={code}
									lang={isPython ? 'python' : 'json'}
									className="text-xs"
									onChange={onCodeChange}
									onSave={save}
								/>
							{/key}
						</div>
						<div class="text-xs text-gray-500">
							{#if isPython}
								{$i18n.t(
									'Define signals(df, params) returning (entries, exits) boolean pandas Series. df has open, high, low, close, volume columns.'
								)}
							{:else}
								{$i18n.t(
									'"$name" refers to a parameter. Indicators: sma, ema, rsi, bbands (.upper/.middle/.lower), macd (.macd/.signal/.hist), atr, roc, highest, lowest. Ops: >, <, >=, <=, crosses_above, crosses_below.'
								)}
							{/if}
						</div>
					</div>
				</div>

				<div class="flex flex-col gap-4">
					<div class="rounded-2xl border border-gray-100 dark:border-gray-850 p-3">
						<div class="flex items-center justify-between mb-2">
							<div class="text-sm font-medium">{$i18n.t('Parameters')}</div>
							<button
								class="text-xs text-gray-500 hover:text-gray-900 dark:hover:text-white"
								type="button"
								on:click={() => (paramRows = [...paramRows, { key: '', value: '' }])}
								>+ {$i18n.t('Add')}</button
							>
						</div>
						<div class="flex flex-col gap-1.5">
							{#each paramRows as row, idx (idx)}
								<div class="flex items-center gap-1.5">
									<input
										class="w-1/2 rounded-lg px-2 py-1 text-sm bg-gray-50 dark:bg-gray-850 outline-hidden"
										placeholder={$i18n.t('Name')}
										bind:value={row.key}
									/>
									<input
										class="w-1/2 rounded-lg px-2 py-1 text-sm bg-gray-50 dark:bg-gray-850 outline-hidden"
										type="number"
										step="any"
										bind:value={row.value}
									/>
									<button
										class="text-gray-400 hover:text-red-600 px-1"
										type="button"
										aria-label={$i18n.t('Remove')}
										on:click={() => (paramRows = paramRows.filter((_, i) => i !== idx))}>×</button
									>
								</div>
							{:else}
								<div class="text-xs text-gray-500">{$i18n.t('No parameters')}</div>
							{/each}
						</div>
					</div>

					<div class="rounded-2xl border border-gray-100 dark:border-gray-850 p-3">
						<div class="text-sm font-medium mb-2">{$i18n.t('Run backtest')}</div>
						<div class="flex flex-col gap-2 text-xs text-gray-500">
							<label class="flex flex-col gap-1">
								{$i18n.t('Symbols')}
								<input
									class="rounded-lg px-2 py-1 text-sm bg-gray-50 dark:bg-gray-850 text-gray-900 dark:text-gray-100 outline-hidden"
									bind:value={btSymbols}
								/>
							</label>
							<div class="grid grid-cols-2 gap-2">
								<label class="flex flex-col gap-1">
									{$i18n.t('Start')}
									<input
										type="date"
										class="rounded-lg px-2 py-1 text-sm bg-gray-50 dark:bg-gray-850 text-gray-900 dark:text-gray-100 outline-hidden"
										bind:value={btStart}
									/>
								</label>
								<label class="flex flex-col gap-1">
									{$i18n.t('End')}
									<input
										type="date"
										class="rounded-lg px-2 py-1 text-sm bg-gray-50 dark:bg-gray-850 text-gray-900 dark:text-gray-100 outline-hidden"
										bind:value={btEnd}
									/>
								</label>
							</div>
							<div class="grid grid-cols-3 gap-2">
								<label class="flex flex-col gap-1">
									{$i18n.t('Initial cash')}
									<input
										type="number"
										min="0"
										class="rounded-lg px-2 py-1 text-sm bg-gray-50 dark:bg-gray-850 text-gray-900 dark:text-gray-100 outline-hidden"
										bind:value={btCash}
									/>
								</label>
								<label class="flex flex-col gap-1">
									{$i18n.t('Commission (bps)')}
									<input
										type="number"
										min="0"
										step="any"
										class="rounded-lg px-2 py-1 text-sm bg-gray-50 dark:bg-gray-850 text-gray-900 dark:text-gray-100 outline-hidden"
										bind:value={btCommission}
									/>
								</label>
								<label class="flex flex-col gap-1">
									{$i18n.t('Slippage (bps)')}
									<input
										type="number"
										min="0"
										step="any"
										class="rounded-lg px-2 py-1 text-sm bg-gray-50 dark:bg-gray-850 text-gray-900 dark:text-gray-100 outline-hidden"
										bind:value={btSlippage}
									/>
								</label>
							</div>

							<div class="flex items-center justify-between mt-1">
								<span>{$i18n.t('Parameter grid (optional)')}</span>
								<button
									class="hover:text-gray-900 dark:hover:text-white"
									type="button"
									on:click={() => (gridRows = [...gridRows, { key: '', values: '' }])}
									>+ {$i18n.t('Add')}</button
								>
							</div>
							{#each gridRows as row, idx (idx)}
								<div class="flex items-center gap-1.5">
									<input
										class="w-1/3 rounded-lg px-2 py-1 text-sm bg-gray-50 dark:bg-gray-850 text-gray-900 dark:text-gray-100 outline-hidden"
										placeholder={$i18n.t('Param')}
										bind:value={row.key}
									/>
									<input
										class="flex-1 rounded-lg px-2 py-1 text-sm bg-gray-50 dark:bg-gray-850 text-gray-900 dark:text-gray-100 outline-hidden"
										placeholder="5, 10, 20"
										bind:value={row.values}
									/>
									<button
										class="text-gray-400 hover:text-red-600 px-1"
										type="button"
										aria-label={$i18n.t('Remove')}
										on:click={() => (gridRows = gridRows.filter((_, i) => i !== idx))}>×</button
									>
								</div>
							{/each}

							<button
								class="mt-1 px-3.5 py-1.5 text-sm font-medium bg-black hover:bg-gray-900 text-white dark:bg-white dark:text-black dark:hover:bg-gray-100 transition rounded-full disabled:opacity-50 flex items-center justify-center gap-2"
								type="button"
								disabled={running}
								on:click={runBacktest}
							>
								{#if running}<Spinner className="size-3.5" />{/if}
								{$i18n.t('Run')}
							</button>
							<div class="text-[0.6875rem]">
								{$i18n.t('The saved strategy version is used; save first to test edits.')}
							</div>
						</div>
					</div>

					<div class="rounded-2xl border border-gray-100 dark:border-gray-850 p-3">
						<div class="text-sm font-medium mb-2">{$i18n.t('Recent backtests')}</div>
						{#if backtests.length === 0}
							<div class="text-xs text-gray-500">{$i18n.t('No backtests yet')}</div>
						{:else}
							<div class="flex flex-col gap-1">
								{#each backtests as b (b.id)}
									<a
										href="/markets/backtests/{b.id}"
										class="flex items-center gap-2 text-xs rounded-lg px-1.5 py-1 hover:bg-gray-50 dark:hover:bg-gray-850"
									>
										<span class="rounded-full px-1.5 py-0.5 {statusClass(b.status)}"
											>{b.status}</span
										>
										<span class="truncate flex-1">{(b.config?.symbols ?? []).join(', ')}</span>
										<span class={signClass(b.metrics?.total_return)}
											>{fmtPct(b.metrics?.total_return)}</span
										>
										<span class="text-gray-500">/ {fmtPct(b.benchmark?.total_return)}</span>
										<span class="text-gray-500">S {fmtNumber(b.metrics?.sharpe)}</span>
									</a>
								{/each}
							</div>
						{/if}
					</div>
				</div>
			</div>
		</div>
	{/if}
</div>
