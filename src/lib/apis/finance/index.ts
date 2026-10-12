import { WEBUI_API_BASE_URL } from '$lib/constants';

export type FinanceCredentialField = {
	name: string;
	label: string;
	secret: boolean;
	required: boolean;
};

export type FinanceProvider = {
	id: string;
	name: string;
	kinds: string[];
	asset_classes: string[];
	supports_paper: boolean;
	credential_fields: FinanceCredentialField[];
};

export type FinanceConnection = {
	id: string;
	user_id: string;
	name: string;
	provider: string;
	kind: string;
	asset_classes: string[];
	is_paper: boolean;
	is_default: boolean;
	meta: Record<string, unknown> | null;
	has_credentials: boolean;
	key_hint: string | null;
	created_at: number;
	updated_at: number;
};

export type FinanceConnectionForm = {
	name: string;
	provider: string;
	kind: string;
	is_paper: boolean;
	is_default: boolean;
	credentials?: Record<string, string> | null;
	meta?: Record<string, unknown> | null;
};

export type FinanceAccountSummary = {
	status: string;
	equity: number | null;
	cash: number | null;
	buying_power: number | null;
	currency: string;
};

export type FinanceTestResult = {
	ok: boolean;
	error?: string;
	account?: FinanceAccountSummary;
};

export type FinanceQuote = {
	symbol: string;
	bid: number | null;
	ask: number | null;
	bid_size: number | null;
	ask_size: number | null;
	last: number | null;
	timestamp: string | null;
};

export type FinanceTimeframe = '1Min' | '5Min' | '15Min' | '1Hour' | '1Day' | '1Week';

export type FinanceBar = {
	t: number;
	o: number;
	h: number;
	l: number;
	c: number;
	v: number;
	vw: number | null;
};

export type FinanceBars = {
	symbol: string;
	timeframe: FinanceTimeframe;
	bars: FinanceBar[];
};

export type FinanceAsset = {
	symbol: string;
	name: string;
	exchange: string;
	asset_class: string;
	tradable: boolean;
};

export type FinanceAccount = FinanceAccountSummary & {
	portfolio_value: number | null;
	is_paper: boolean;
};

export type FinancePosition = {
	symbol: string;
	qty: number;
	side: string;
	avg_entry_price: number | null;
	market_value: number | null;
	cost_basis: number | null;
	unrealized_pl: number | null;
	unrealized_plpc: number | null;
	current_price: number | null;
	asset_class: string;
};

export type FinanceOrder = {
	id: string;
	symbol: string;
	side: string;
	type: string;
	qty: number | null;
	filled_qty: number | null;
	limit_price: number | null;
	stop_price: number | null;
	status: string;
	submitted_at: string | null;
	filled_avg_price: number | null;
	asset_class: string;
};

export type FinanceNewsItem = {
	id: string | number;
	headline: string;
	summary: string;
	source: string;
	url: string;
	symbols: string[];
	created_at: string;
};

const request = async <T>(
	token: string,
	path: string,
	method: 'GET' | 'POST' | 'DELETE' = 'GET',
	body?: unknown
): Promise<T> => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/finance${path}`, {
		method,
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		...(body !== undefined ? { body: JSON.stringify(body) } : {})
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err?.detail ?? err;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

const buildQuery = (params: Record<string, string | number | null | undefined>) => {
	const searchParams = new URLSearchParams();
	for (const [key, value] of Object.entries(params)) {
		if (value !== undefined && value !== null && value !== '') {
			searchParams.append(key, `${value}`);
		}
	}
	const query = searchParams.toString();
	return query ? `?${query}` : '';
};

export const getFinanceProviders = async (token: string) =>
	request<FinanceProvider[]>(token, '/providers');

export const getFinanceConnections = async (token: string) =>
	request<FinanceConnection[]>(token, '/connections');

export const createFinanceConnection = async (token: string, form: FinanceConnectionForm) =>
	request<FinanceConnection>(token, '/connections/create', 'POST', form);

export const updateFinanceConnectionById = async (
	token: string,
	id: string,
	form: FinanceConnectionForm
) =>
	request<FinanceConnection>(token, `/connections/${encodeURIComponent(id)}/update`, 'POST', form);

export const deleteFinanceConnectionById = async (token: string, id: string) =>
	request<boolean>(token, `/connections/${encodeURIComponent(id)}/delete`, 'DELETE');

export const testFinanceConnectionById = async (token: string, id: string) =>
	request<FinanceTestResult>(token, `/connections/${encodeURIComponent(id)}/test`, 'POST');

export const getFinanceQuote = async (
	token: string,
	symbol: string,
	connectionId?: string | null
) => request<FinanceQuote>(token, `/quote${buildQuery({ symbol, connection_id: connectionId })}`);

export const getFinanceBars = async (
	token: string,
	symbol: string,
	timeframe: FinanceTimeframe,
	options: {
		start?: string | null;
		end?: string | null;
		limit?: number | null;
		connectionId?: string | null;
	} = {}
) =>
	request<FinanceBars>(
		token,
		`/bars${buildQuery({
			symbol,
			timeframe,
			start: options.start,
			end: options.end,
			limit: options.limit,
			connection_id: options.connectionId
		})}`
	);

export const searchFinanceAssets = async (token: string, q: string, limit: number = 10) =>
	request<FinanceAsset[]>(token, `/search${buildQuery({ q, limit })}`);

export const getFinanceAccount = async (token: string, connectionId?: string | null) =>
	request<FinanceAccount>(token, `/account${buildQuery({ connection_id: connectionId })}`);

export const getFinancePositions = async (token: string, connectionId?: string | null) =>
	request<FinancePosition[]>(token, `/positions${buildQuery({ connection_id: connectionId })}`);

export const getFinanceOrders = async (
	token: string,
	status: 'open' | 'closed' | 'all' = 'open',
	limit: number | null = null,
	connectionId?: string | null
) =>
	request<FinanceOrder[]>(
		token,
		`/orders${buildQuery({ status, limit, connection_id: connectionId })}`
	);

export const getFinanceNews = async (token: string, symbols: string[] = [], limit: number = 10) =>
	request<FinanceNewsItem[]>(token, `/news${buildQuery({ symbols: symbols.join(','), limit })}`);

// ---------------------------------------------------------------------------
// Strategies & backtests (Phase 2)
// ---------------------------------------------------------------------------

export type FinanceStrategyKind = 'template' | 'python';

export type FinanceStrategy = {
	id: string;
	user_id: string;
	name: string;
	description: string;
	kind: FinanceStrategyKind;
	definition: Record<string, unknown> | null;
	source: string | null;
	params: Record<string, number>;
	timeframe: FinanceTimeframe;
	universe: string[];
	version: number;
	meta: Record<string, unknown> | null;
	created_at: number;
	updated_at: number;
};

export type FinanceStrategyTemplate = {
	id: string;
	name: string;
	description: string;
	kind: 'template';
	definition: Record<string, unknown>;
	params: Record<string, number>;
	timeframe: FinanceTimeframe;
	universe: string[];
};

export type FinanceStrategyForm = {
	name: string;
	description: string;
	kind: FinanceStrategyKind;
	definition?: Record<string, unknown> | null;
	source?: string | null;
	params: Record<string, number>;
	timeframe: FinanceTimeframe;
	universe: string[];
	meta?: Record<string, unknown> | null;
};

export type FinanceStrategyVersion = {
	version: number;
	definition: Record<string, unknown> | null;
	source: string | null;
	params: Record<string, number>;
	created_at: number;
};

export type FinanceValidateResult = { ok: boolean; error?: string };

export type FinanceBacktestMetrics = {
	total_return: number | null;
	cagr: number | null;
	sharpe: number | null;
	sortino: number | null;
	max_drawdown: number | null;
	win_rate: number | null;
	profit_factor: number | null;
	trades: number | null;
	exposure: number | null;
	avg_trade_return: number | null;
	final_equity: number | null;
};

export type FinancePoint = { t: number; v: number };

export type FinanceBacktestTrade = {
	symbol: string;
	entry_t: number;
	entry_price: number;
	exit_t: number | null;
	exit_price: number | null;
	qty: number;
	return_pct: number | null;
	pnl: number | null;
	exit_reason: string | null;
};

export type FinanceBacktestResult = {
	equity: FinancePoint[];
	benchmark_equity: FinancePoint[];
	trades: FinanceBacktestTrade[];
	per_symbol: Record<
		string,
		{ metrics: FinanceBacktestMetrics | null; benchmark: FinanceBacktestMetrics | null }
	>;
	grid?: { params: Record<string, number>; metrics: FinanceBacktestMetrics | null }[];
	best_params?: Record<string, number> | null;
};

export type FinanceBacktestConfig = {
	symbols: string[];
	timeframe: FinanceTimeframe;
	start: string | null;
	end: string | null;
	params: Record<string, number>;
	grid?: Record<string, number[]> | null;
	initial_cash: number;
	commission_bps: number;
	slippage_bps: number;
};

export type FinanceBacktestStatus = 'queued' | 'running' | 'completed' | 'failed';

export type FinanceBacktest = {
	id: string;
	user_id: string;
	strategy_id: string | null;
	strategy_version: number | null;
	name: string;
	config: FinanceBacktestConfig;
	status: FinanceBacktestStatus;
	error: string | null;
	metrics: FinanceBacktestMetrics | null;
	benchmark: FinanceBacktestMetrics | null;
	result?: FinanceBacktestResult | null;
	created_at: number;
	updated_at: number;
	completed_at: number | null;
};

export type FinanceBacktestRunForm = {
	strategy_id?: string | null;
	definition?: Record<string, unknown> | null;
	symbols: string[];
	timeframe?: FinanceTimeframe;
	start?: string | null;
	end?: string | null;
	params?: Record<string, number>;
	grid?: Record<string, number[]> | null;
	initial_cash?: number;
	commission_bps?: number;
	slippage_bps?: number;
};

export const getFinanceStrategyTemplates = async (token: string) =>
	request<FinanceStrategyTemplate[]>(token, '/strategies/templates');

export const getFinanceStrategies = async (token: string) =>
	request<FinanceStrategy[]>(token, '/strategies');

export const createFinanceStrategy = async (token: string, form: FinanceStrategyForm) =>
	request<FinanceStrategy>(token, '/strategies/create', 'POST', form);

export const getFinanceStrategyById = async (token: string, id: string) =>
	request<FinanceStrategy>(token, `/strategies/${encodeURIComponent(id)}`);

export const updateFinanceStrategyById = async (
	token: string,
	id: string,
	form: FinanceStrategyForm
) => request<FinanceStrategy>(token, `/strategies/${encodeURIComponent(id)}/update`, 'POST', form);

export const getFinanceStrategyVersions = async (token: string, id: string) =>
	request<FinanceStrategyVersion[]>(token, `/strategies/${encodeURIComponent(id)}/versions`);

export const deleteFinanceStrategyById = async (token: string, id: string) =>
	request<boolean>(token, `/strategies/${encodeURIComponent(id)}/delete`, 'DELETE');

export const validateFinanceStrategy = async (
	token: string,
	body: {
		kind: FinanceStrategyKind;
		definition?: Record<string, unknown> | null;
		source?: string | null;
		params?: Record<string, number>;
	}
) => request<FinanceValidateResult>(token, '/strategies/validate', 'POST', body);

export const runFinanceBacktest = async (token: string, form: FinanceBacktestRunForm) =>
	request<FinanceBacktest>(token, '/backtests/run', 'POST', form);

export const getFinanceBacktests = async (
	token: string,
	strategyId?: string | null,
	limit?: number | null
) =>
	request<FinanceBacktest[]>(token, `/backtests${buildQuery({ strategy_id: strategyId, limit })}`);

export const getFinanceBacktestById = async (token: string, id: string) =>
	request<FinanceBacktest>(token, `/backtests/${encodeURIComponent(id)}`);

export const deleteFinanceBacktestById = async (token: string, id: string) =>
	request<boolean>(token, `/backtests/${encodeURIComponent(id)}/delete`, 'DELETE');
