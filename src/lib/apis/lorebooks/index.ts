import { WEBUI_API_BASE_URL } from '$lib/constants';

export type LorebookEntry = {
	id?: number;
	name?: string;
	comment?: string;
	enabled: boolean;
	keys: string[];
	secondary_keys: string[];
	selective: boolean;
	use_regex: boolean;
	case_sensitive: boolean;
	constant: boolean;
	insertion_order: number;
	priority: number;
	position: 'before_char' | 'after_char';
	content: string;
	extensions?: Record<string, unknown>;
};

export type LorebookData = {
	scan_depth?: number | null;
	token_budget?: number | null;
	recursive_scanning?: boolean;
	extensions?: Record<string, unknown>;
	entries: LorebookEntry[];
};

export type Lorebook = {
	id: string;
	user_id: string;
	name: string;
	description: string | null;
	data: LorebookData;
	meta: Record<string, unknown> | null;
	access_grants: object[];
	write_access?: boolean;
	created_at: number;
	updated_at: number;
	user?: { name?: string } | null;
};

export type LorebookForm = {
	name: string;
	description?: string | null;
	data: LorebookData;
	meta?: Record<string, unknown> | null;
	access_grants?: object[] | null;
};

const request = async (path: string, token: string, init: RequestInit = {}) => {
	const response = await fetch(`${WEBUI_API_BASE_URL}${path}`, {
		...init,
		headers: {
			Accept: 'application/json',
			...(init.body && !(init.body instanceof FormData)
				? { 'Content-Type': 'application/json' }
				: {}),
			authorization: `Bearer ${token}`,
			...(init.headers ?? {})
		}
	});
	if (!response.ok) throw await response.json().catch(() => response.statusText);
	return response;
};

export const getLorebooks = async (
	token: string,
	query: string = '',
	permission: 'read' | 'write' = 'read'
): Promise<{ items: Lorebook[]; total: number }> => {
	const params = new URLSearchParams({ permission });
	if (query) params.set('query', query);
	return (await request(`/lorebooks/?${params}`, token)).json();
};

export const getLorebookById = async (token: string, id: string): Promise<Lorebook> =>
	(await request(`/lorebooks/${encodeURIComponent(id)}`, token)).json();

export const createLorebook = async (token: string, form: LorebookForm): Promise<Lorebook> =>
	(
		await request('/lorebooks/create', token, { method: 'POST', body: JSON.stringify(form) })
	).json();

export const updateLorebookById = async (
	token: string,
	id: string,
	form: LorebookForm
): Promise<Lorebook> =>
	(
		await request(`/lorebooks/${encodeURIComponent(id)}/update`, token, {
			method: 'POST',
			body: JSON.stringify(form)
		})
	).json();

export const deleteLorebookById = async (token: string, id: string): Promise<boolean> =>
	(
		await request(`/lorebooks/${encodeURIComponent(id)}/delete`, token, { method: 'DELETE' })
	).json();

export const importLorebook = async (token: string, file: File): Promise<Lorebook> => {
	const body = new FormData();
	body.append('file', file);
	return (await request('/lorebooks/import', token, { method: 'POST', body })).json();
};

export const exportLorebook = async (token: string, id: string) => {
	const response = await request(`/lorebooks/${encodeURIComponent(id)}/export`, token);
	const disposition = response.headers.get('Content-Disposition') ?? '';
	return {
		blob: await response.blob(),
		filename: disposition.match(/filename="([^"]+)"/)?.[1] ?? 'lorebook.json'
	};
};

export const testLorebook = async (
	token: string,
	data: LorebookData,
	text: string
): Promise<{
	before_char: string[];
	after_char: string[];
	depth: { text: string; depth: number; role: string }[];
}> =>
	(
		await request('/lorebooks/test', token, {
			method: 'POST',
			body: JSON.stringify({ data, text })
		})
	).json();

export type ChatRoleplaySettings = {
	lorebook_ids: string[];
	authors_note: { text: string; depth: number; role: 'system' | 'user' | 'assistant' } | null;
	persona_id?: string | null;
	memory_enabled?: boolean;
	recall_enabled?: boolean;
};

export type ChatMemory = {
	available: boolean;
	enabled: boolean;
	summary: string | null;
	message_id: string | null;
	summarized_messages: number;
	total_messages: number;
	token_threshold: number;
};

export const getChatMemory = async (token: string, chatId: string): Promise<ChatMemory> =>
	(await request(`/chats/${encodeURIComponent(chatId)}/roleplay/memory`, token)).json();

export const updateChatMemory = async (
	token: string,
	chatId: string,
	summary: string
): Promise<ChatMemory> =>
	(
		await request(`/chats/${encodeURIComponent(chatId)}/roleplay/memory`, token, {
			method: 'POST',
			body: JSON.stringify({ summary })
		})
	).json();

export const refreshChatMemory = async (token: string, chatId: string): Promise<ChatMemory> =>
	(
		await request(`/chats/${encodeURIComponent(chatId)}/roleplay/memory/refresh`, token, {
			method: 'POST'
		})
	).json();

// Character memory across chats: what the character remembers about the user.
export type ChatRecallEntry = {
	id: string;
	content: string;
	chat_id: string | null;
	created_at: number;
	updated_at: number;
};

export type ChatRecall = {
	available: boolean;
	enabled: boolean;
	character_enabled: boolean;
	model_id: string;
	char: string;
	entries: ChatRecallEntry[];
	updated: boolean;
	new_messages: number;
};

export const getChatRecall = async (token: string, chatId: string): Promise<ChatRecall> =>
	(await request(`/chats/${encodeURIComponent(chatId)}/roleplay/recall`, token)).json();

export const updateChatRecall = async (
	token: string,
	chatId: string,
	entries: { id?: string | null; content: string }[]
): Promise<ChatRecall> =>
	(
		await request(`/chats/${encodeURIComponent(chatId)}/roleplay/recall`, token, {
			method: 'POST',
			body: JSON.stringify({ entries })
		})
	).json();

export const refreshChatRecall = async (
	token: string,
	chatId: string,
	auto = false
): Promise<ChatRecall> =>
	(
		await request(`/chats/${encodeURIComponent(chatId)}/roleplay/recall/refresh`, token, {
			method: 'POST',
			body: JSON.stringify({ auto })
		})
	).json();

export const getChatRoleplaySettings = async (
	token: string,
	chatId: string
): Promise<ChatRoleplaySettings> =>
	(await request(`/chats/${encodeURIComponent(chatId)}/roleplay`, token)).json();

export const updateChatRoleplaySettings = async (
	token: string,
	chatId: string,
	settings: ChatRoleplaySettings
): Promise<ChatRoleplaySettings> =>
	(
		await request(`/chats/${encodeURIComponent(chatId)}/roleplay`, token, {
			method: 'POST',
			body: JSON.stringify(settings)
		})
	).json();

export type RoleplayAssistForm = {
	model_id: string;
	messages: { role: string; content: unknown }[];
	chat_id?: string | null;
	message_id?: string | null;
	persona_id?: string | null;
	draft?: string;
	style?: string;
};

// Writes the user's next message, as their persona, for them to review before sending.
export const impersonateUser = async (
	token: string,
	form: RoleplayAssistForm
): Promise<{ content: string }> =>
	(
		await request('/chats/roleplay/impersonate', token, {
			method: 'POST',
			body: JSON.stringify(form)
		})
	).json();

export const generateSceneImage = async (
	token: string,
	form: RoleplayAssistForm
): Promise<{ prompt: string; images: { id?: string; url: string }[] }> =>
	(
		await request('/chats/roleplay/scene-image', token, {
			method: 'POST',
			body: JSON.stringify(form)
		})
	).json();

export type LoreSuggestion = { name: string; keys: string[]; content: string };

export const suggestLoreEntries = async (
	token: string,
	chatId: string
): Promise<{ suggestions: LoreSuggestion[]; source: 'memory' | 'chat' }> =>
	(
		await request(`/chats/${encodeURIComponent(chatId)}/roleplay/lore/suggest`, token, {
			method: 'POST'
		})
	).json();

// The character's first message for a new chat, picking up from what it remembers about the user.
export const writeCharacterOpening = async (
	token: string,
	form: RoleplayAssistForm
): Promise<{ content: string; remembered: number }> =>
	(
		await request('/chats/roleplay/opening', token, {
			method: 'POST',
			body: JSON.stringify(form)
		})
	).json();
