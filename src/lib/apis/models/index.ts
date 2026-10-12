import { WEBUI_API_BASE_URL } from '$lib/constants';

export const exportModels = async (token: string, ids?: string[]) => {
	const query = new URLSearchParams();
	(ids ?? []).forEach((id) => query.append('ids', id));
	if (ids && !ids.length) return [];
	const response = await fetch(`${WEBUI_API_BASE_URL}/models/export${ids ? `?${query}` : ''}`, {
		headers: { authorization: `Bearer ${token}` }
	});
	if (!response.ok) throw await response.json();
	return response.json();
};

export const getModelItems = async (
	token: string = '',
	query,
	viewOption,
	selectedTag,
	orderBy,
	direction,
	page,
	signal?: AbortSignal
) => {
	let error = null;

	const searchParams = new URLSearchParams();
	if (query) {
		searchParams.append('query', query);
	}
	if (viewOption) {
		searchParams.append('view_option', viewOption);
	}
	if (selectedTag) {
		searchParams.append('tag', selectedTag);
	}
	if (orderBy) {
		searchParams.append('order_by', orderBy);
	}
	if (direction) {
		searchParams.append('direction', direction);
	}
	if (page) {
		searchParams.append('page', page.toString());
	}

	const res = await fetch(`${WEBUI_API_BASE_URL}/models/list?${searchParams.toString()}`, {
		method: 'GET',
		signal,
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			if (signal?.aborted) return null;
			error = err;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const getModelTags = async (token: string = '') => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/models/tags`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const getBaseModelTags = async (token: string = '') => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/models/base/tags`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const importModels = async (token: string, models: object[]) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/models/import`, {
		method: 'POST',
		headers: {
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify({ models: models })
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const getAllModels = async (token: string = '') => {
	const res = await fetch(`${WEBUI_API_BASE_URL}/models/all`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			authorization: `Bearer ${token}`
		}
	});
	if (!res.ok) throw await res.json();
	return res.json();
};

export const getBaseModels = async (token: string = '', tag: string = '') => {
	let error = null;

	const searchParams = new URLSearchParams();
	if (tag) {
		searchParams.append('tag', tag);
	}

	const res = await fetch(`${WEBUI_API_BASE_URL}/models/base?${searchParams.toString()}`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const createNewModel = async (token: string, model: object) => {
	let error = null;

	const { id, base_model_id, name, meta, params, access_grants, is_active, commit_message } =
		model as any;
	const payload = {
		id,
		base_model_id,
		name,
		meta,
		params,
		access_grants,
		is_active,
		commit_message
	};

	const res = await fetch(`${WEBUI_API_BASE_URL}/models/create`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify(payload)
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err.detail;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const getModelById = async (token: string, id: string) => {
	let error = null;

	const searchParams = new URLSearchParams();
	searchParams.append('id', id);

	const res = await fetch(`${WEBUI_API_BASE_URL}/models/model?${searchParams.toString()}`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err;

			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const toggleModelById = async (token: string, id: string) => {
	let error = null;

	const searchParams = new URLSearchParams();
	searchParams.append('id', id);

	const res = await fetch(`${WEBUI_API_BASE_URL}/models/model/toggle?${searchParams.toString()}`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err;

			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const updateModelById = async (token: string, id: string, model: object) => {
	let error = null;

	const { base_model_id, name, meta, params, access_grants, is_active, commit_message } =
		model as any;
	const payload = {
		id,
		base_model_id,
		name,
		meta,
		params,
		access_grants,
		is_active,
		commit_message
	};

	const res = await fetch(`${WEBUI_API_BASE_URL}/models/model/update`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify(payload)
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err;

			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const updateModelAccessGrants = async (
	token: string,
	id: string,
	name: string,
	accessGrants: any[]
) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/models/model/access/update`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify({ id, name, access_grants: accessGrants })
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const deleteModelById = async (token: string, id: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/models/model/delete`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify({ id })
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err.detail;

			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const deleteAllModels = async (token: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/models/delete/all`, {
		method: 'DELETE',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err;

			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const importCharacterCards = async (
	token: string,
	files: File[],
	baseModelId: string,
	isPublic: boolean = false
) => {
	const body = new FormData();
	files.forEach((file) => body.append('files', file));
	body.append('base_model_id', baseModelId);
	body.append('public', isPublic ? 'true' : 'false');

	const response = await fetch(`${WEBUI_API_BASE_URL}/models/characters/import`, {
		method: 'POST',
		headers: { authorization: `Bearer ${token}` },
		body
	});
	if (!response.ok) throw await response.json();
	return response.json();
};

export const exportCharacterCard = async (
	token: string,
	id: string,
	format: 'png' | 'json' = 'png'
): Promise<{ blob: Blob; filename: string; rawTruncated: boolean }> => {
	const query = new URLSearchParams({ id, format });
	const response = await fetch(`${WEBUI_API_BASE_URL}/models/model/character/export?${query}`, {
		headers: { authorization: `Bearer ${token}` }
	});
	if (!response.ok) throw await response.json();

	const disposition = response.headers.get('Content-Disposition') ?? '';
	const filename = disposition.match(/filename="([^"]+)"/)?.[1] ?? `character.${format}`;
	return {
		blob: await response.blob(),
		filename,
		rawTruncated: response.headers.get('X-Character-Raw-Truncated') === 'true'
	};
};

export const getCharacterGreetings = async (token: string, id: string): Promise<string[]> => {
	const query = new URLSearchParams({ id });
	const response = await fetch(`${WEBUI_API_BASE_URL}/models/model/character/greetings?${query}`, {
		headers: { authorization: `Bearer ${token}` }
	});
	if (!response.ok) throw await response.json();
	return response.json();
};

export const convertSystemPromptToCharacter = async (
	token: string,
	form: { system: string; name?: string; base_model_id?: string | null; use_ai?: boolean }
): Promise<{ character: Record<string, any>; detected: string[]; method: 'parser' | 'ai' }> => {
	const response = await fetch(`${WEBUI_API_BASE_URL}/models/characters/convert`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json', authorization: `Bearer ${token}` },
		body: JSON.stringify(form)
	});
	if (!response.ok) throw await response.json();
	return response.json();
};

export type StoryCharacterDraft = {
	name: string;
	aliases: string[];
	description: string;
	personality: string;
	mes_example: string;
	first_mes: string;
	creator_notes: string;
	mentions: number;
	selected: boolean;
	detailed?: boolean;
};

export type StoryDraft = {
	characters: StoryCharacterDraft[];
	lorebook: { name: string; entries: Record<string, any>[] };
	events: string[];
	chunks: number;
	failed_chunks: number;
};

const postJson = async (token: string, path: string, body: object) => {
	const response = await fetch(`${WEBUI_API_BASE_URL}${path}`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json', authorization: `Bearer ${token}` },
		body: JSON.stringify(body)
	});
	if (!response.ok) throw await response.json().catch(() => response.statusText);
	return response.json();
};

export const analyzeStory = (
	token: string,
	form: {
		text: string;
		title: string;
		base_model_id: string;
		analysis_model_id?: string | null;
		detailed?: boolean;
	}
): Promise<StoryDraft> => postJson(token, '/models/characters/story/analyze', form);

export const writeStoryProfile = (
	token: string,
	form: {
		text: string;
		title: string;
		base_model_id: string;
		analysis_model_id?: string | null;
		character: object;
	}
): Promise<{ description: string; personality: string; mes_example: string }> =>
	postJson(token, '/models/characters/story/profile', form);

export const draftStoryGreeting = (
	token: string,
	form: { base_model_id: string; character: object; scenario: string }
): Promise<{ first_mes: string }> => postJson(token, '/models/characters/story/greeting', form);

export const createCharactersFromStory = (
	token: string,
	form: {
		title: string;
		base_model_id: string;
		public: boolean;
		scenario: string;
		characters: object[];
		lorebook: object | null;
	}
) => postJson(token, '/models/characters/story/create', form);

export type CardSource = {
	id: string;
	name: string;
	url: string;
	searchable?: boolean;
	supports_tags?: boolean;
	result_tags?: boolean;
};

export type CardSummary = {
	source: string;
	id: string;
	name: string;
	tagline: string;
	creator: string;
	tags: string[];
	image_url: string;
	page_url: string;
	nsfw: boolean;
	has_lorebook: boolean | null;
	tokens: number | null;
	downloads: string;
};

export const getCardSources = async (token: string): Promise<CardSource[]> => {
	const response = await fetch(`${WEBUI_API_BASE_URL}/models/characters/sources`, {
		headers: { authorization: `Bearer ${token}` }
	});
	if (!response.ok) throw await response.json().catch(() => response.statusText);
	return response.json();
};

export const searchCardSource = async (
	token: string,
	source: string,
	params: {
		q: string;
		page: number;
		sort: string;
		nsfw: boolean;
		tags?: string[];
		exclude_tags?: string[];
	},
	signal?: AbortSignal
): Promise<{ items: CardSummary[]; page: number; has_more: boolean }> => {
	const query = new URLSearchParams({
		q: params.q,
		page: String(params.page),
		sort: params.sort,
		nsfw: String(params.nsfw),
		tags: (params.tags ?? []).join(','),
		exclude_tags: (params.exclude_tags ?? []).join(',')
	});
	const response = await fetch(
		`${WEBUI_API_BASE_URL}/models/characters/sources/${encodeURIComponent(source)}/search?${query}`,
		{ headers: { authorization: `Bearer ${token}` }, signal }
	);
	if (!response.ok) throw await response.json().catch(() => response.statusText);
	return response.json();
};

export type CardPreview = {
	name: string;
	creator: string;
	creator_notes: string;
	description: string;
	personality: string;
	scenario: string;
	first_mes: string;
	alternate_greetings: string[];
	mes_example: string;
	system_prompt: string;
	tags: string[];
	lorebook_entries: number;
	expressions: string[];
	size: number;
};

export const previewCardFromSource = async (
	token: string,
	source: string,
	id: string
): Promise<CardPreview> => {
	const response = await fetch(
		`${WEBUI_API_BASE_URL}/models/characters/sources/${encodeURIComponent(source)}/card?id=${encodeURIComponent(id)}`,
		{ headers: { authorization: `Bearer ${token}` } }
	);
	if (!response.ok) throw await response.json().catch(() => response.statusText);
	return response.json();
};

export const importCardFromSource = (
	token: string,
	form: { source?: string; id?: string; url?: string; base_model_id: string; public: boolean }
) => postJson(token, '/models/characters/sources/import', form);

export const characterExpressionUrl = (modelId: string, name: string) =>
	`${WEBUI_API_BASE_URL}/models/model/character/expression?id=${encodeURIComponent(modelId)}&name=${encodeURIComponent(name)}`;

export const uploadCharacterExpressions = async (
	token: string,
	modelId: string,
	files: File[]
): Promise<{ expressions: string[]; skipped: string[] }> => {
	const body = new FormData();
	files.forEach((file) => body.append('files', file));
	const response = await fetch(
		`${WEBUI_API_BASE_URL}/models/model/character/expressions?id=${encodeURIComponent(modelId)}`,
		{ method: 'POST', headers: { authorization: `Bearer ${token}` }, body }
	);
	if (!response.ok) throw await response.json().catch(() => response.statusText);
	return response.json();
};

export const deleteCharacterExpression = async (
	token: string,
	modelId: string,
	name: string
): Promise<{ expressions: string[] }> => {
	const response = await fetch(
		`${WEBUI_API_BASE_URL}/models/model/character/expression?id=${encodeURIComponent(modelId)}&name=${encodeURIComponent(name)}`,
		{ method: 'DELETE', headers: { authorization: `Bearer ${token}` } }
	);
	if (!response.ok) throw await response.json().catch(() => response.statusText);
	return response.json();
};

// With `forSpeech`, a character without expression images is classified against the default
// emotions, for emotion-aware speech.
export const classifyCharacterExpression = (
	token: string,
	modelId: string,
	text: string,
	forSpeech = false
): Promise<{ name: string | null }> =>
	postJson(token, '/models/model/character/expression/classify', {
		model_id: modelId,
		text,
		for_speech: forSpeech
	});

// Draws one expression image (from the avatar when image editing is set up).
export const generateCharacterExpression = (
	token: string,
	modelId: string,
	name: string,
	style = ''
): Promise<{ expressions: string[] }> =>
	postJson(
		token,
		`/models/model/character/expressions/generate?id=${encodeURIComponent(modelId)}`,
		{
			name,
			style
		}
	);

export type ModelHistoryEntry = {
	id: string;
	model_id: string;
	parent_id: string | null;
	user_id: string;
	commit_message: string | null;
	created_at: number;
	user?: { name: string } | null;
};

export type ModelSnapshot = {
	name: string;
	base_model_id: string | null;
	params: Record<string, any>;
	meta: Record<string, any>;
};

export const getModelHistory = async (
	token: string,
	id: string,
	page = 1
): Promise<ModelHistoryEntry[]> => {
	const response = await fetch(
		`${WEBUI_API_BASE_URL}/models/model/history?${new URLSearchParams({ id, page: String(page) })}`,
		{
			headers: { authorization: `Bearer ${token}` }
		}
	);
	if (!response.ok) throw await response.json();
	return response.json();
};

export const getModelHistoryEntry = async (
	token: string,
	id: string,
	versionId: string
): Promise<ModelHistoryEntry & { snapshot: ModelSnapshot }> => {
	const response = await fetch(
		`${WEBUI_API_BASE_URL}/models/model/history/${encodeURIComponent(versionId)}?${new URLSearchParams({ id })}`,
		{
			headers: { authorization: `Bearer ${token}` }
		}
	);
	if (!response.ok) throw await response.json();
	return response.json();
};

export const deleteModelHistoryVersion = async (token: string, id: string, versionId: string) => {
	const response = await fetch(
		`${WEBUI_API_BASE_URL}/models/model/history/${encodeURIComponent(versionId)}?${new URLSearchParams({ id })}`,
		{
			method: 'DELETE',
			headers: { authorization: `Bearer ${token}` }
		}
	);
	if (!response.ok) throw await response.json();
	return response.json();
};

export const setProductionModelVersion = async (token: string, id: string, versionId: string) => {
	const response = await fetch(
		`${WEBUI_API_BASE_URL}/models/model/update/version?${new URLSearchParams({ id })}`,
		{
			method: 'POST',
			headers: { authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
			body: JSON.stringify({ version_id: versionId })
		}
	);
	if (!response.ok) throw await response.json();
	return response.json();
};
