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
