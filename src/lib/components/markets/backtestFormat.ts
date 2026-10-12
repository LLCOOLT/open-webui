import type { FinanceBacktestMetrics } from '$lib/apis/finance';

export const fmtPct = (n: number | null | undefined, digits = 2) =>
	n === null || n === undefined || Number.isNaN(n) ? '—' : `${(Number(n) * 100).toFixed(digits)}%`;

export const fmtNumber = (n: number | null | undefined, digits = 2) =>
	n === null || n === undefined || Number.isNaN(n)
		? '—'
		: Number(n).toLocaleString(undefined, { maximumFractionDigits: digits });

export const fmtMoney = (n: number | null | undefined) =>
	n === null || n === undefined
		? '—'
		: Number(n).toLocaleString(undefined, {
				style: 'currency',
				currency: 'USD',
				maximumFractionDigits: 2
			});

/** created_at/updated_at/completed_at are unix nanoseconds */
export const fmtNs = (ns: number | null | undefined) =>
	ns ? new Date(Math.floor(ns / 1_000_000)).toLocaleString() : '—';

/** bar / trade timestamps are unix seconds */
export const fmtSec = (s: number | null | undefined) =>
	s ? new Date(s * 1000).toLocaleDateString() : '—';

export const signClass = (n: number | null | undefined) =>
	n === null || n === undefined || n === 0
		? ''
		: n > 0
			? 'text-green-600 dark:text-green-400'
			: 'text-red-600 dark:text-red-400';

export const statusClass = (status: string) =>
	status === 'completed'
		? 'bg-green-500/15 text-green-700 dark:text-green-300'
		: status === 'failed'
			? 'bg-red-500/15 text-red-700 dark:text-red-300'
			: status === 'running'
				? 'bg-blue-500/15 text-blue-700 dark:text-blue-300'
				: 'bg-gray-500/15 text-gray-700 dark:text-gray-300';

export type MetricKey = keyof FinanceBacktestMetrics;

export const fmtMetric = (kind: string, n: number | null | undefined) =>
	kind === 'pct'
		? fmtPct(n)
		: kind === 'int'
			? fmtNumber(n, 0)
			: kind === 'money'
				? fmtMoney(n)
				: fmtNumber(n);
