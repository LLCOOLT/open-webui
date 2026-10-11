<script lang="ts">
	import { getContext } from 'svelte';
	import { v4 as uuidv4 } from 'uuid';

	import { settings } from '$lib/stores';
	import Plus from '$lib/components/icons/Plus.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';
	import UserSettingSection from './UserSettingSection.svelte';

	const i18n: any = getContext('i18n');

	export let saveSettings: (settings: any) => void | Promise<void>;

	type Persona = {
		id: string;
		name: string;
		description: string;
		avatar_url?: string | null;
		is_default?: boolean;
	};

	let personas: Persona[] = structuredClone($settings?.personas ?? []);
	let saveTimer: ReturnType<typeof setTimeout>;

	const save = () => {
		clearTimeout(saveTimer);
		saveTimer = setTimeout(
			() => saveSettings({ personas: personas.filter((persona) => persona.name.trim()) }),
			500
		);
	};

	const add = () => {
		personas = [
			...personas,
			{ id: uuidv4(), name: '', description: '', avatar_url: null, is_default: !personas.length }
		];
	};

	const remove = (index: number) => {
		const wasDefault = personas[index].is_default;
		personas = personas.filter((_, i: number) => i !== index);
		if (wasDefault && personas.length) personas[0].is_default = true;
		save();
	};

	const setDefault = (index: number) => {
		personas = personas.map((persona, i: number) => ({ ...persona, is_default: i === index }));
		save();
	};

	// Small square thumbnail, like model avatars, so settings stay light.
	const setAvatar = (index: number, file: File | undefined) => {
		if (!file || !file.type.startsWith('image/')) return;
		const reader = new FileReader();
		reader.onload = () => {
			const image = new Image();
			image.onload = () => {
				const size = 128;
				const canvas = document.createElement('canvas');
				canvas.width = canvas.height = size;
				const scale = Math.max(size / image.width, size / image.height);
				const width = image.width * scale;
				const height = image.height * scale;
				canvas
					.getContext('2d')
					?.drawImage(image, (size - width) / 2, (size - height) / 2, width, height);
				personas[index].avatar_url = canvas.toDataURL('image/webp', 0.8);
				save();
			};
			image.src = reader.result as string;
		};
		reader.readAsDataURL(file);
	};
</script>

<UserSettingSection title={$i18n.t('Roleplay Personas')}>
	<div class="text-xs text-gray-500 mb-2">
		{$i18n.t(
			'Who you are in roleplay chats. Characters call you by the persona’s name and read its description. Pick one per chat in Chat Controls.'
		)}
	</div>

	<div class="flex flex-col gap-2">
		{#each personas as persona, index (persona.id)}
			<div class="rounded-xl border border-gray-100 dark:border-gray-850 p-2.5 flex gap-3">
				<label class="shrink-0 cursor-pointer" title={$i18n.t('Change avatar')}>
					{#if persona.avatar_url}
						<img src={persona.avatar_url} alt="" class="size-10 rounded-full object-cover" />
					{:else}
						<div
							class="size-10 rounded-full bg-gray-100 dark:bg-gray-850 flex items-center justify-center text-sm text-gray-500"
						>
							{persona.name.trim().charAt(0).toUpperCase() || '?'}
						</div>
					{/if}
					<input
						type="file"
						accept="image/*"
						hidden
						on:change={(event) => setAvatar(index, event.currentTarget.files?.[0])}
					/>
				</label>

				<div class="flex-1 min-w-0 space-y-1">
					<div class="flex items-center gap-2">
						<input
							class="flex-1 bg-transparent text-sm font-medium outline-hidden placeholder:text-gray-300 dark:placeholder:text-gray-700"
							placeholder={$i18n.t('Persona name')}
							aria-label={$i18n.t('Persona name')}
							bind:value={persona.name}
							on:input={save}
						/>
						<label class="flex items-center gap-1 text-xs text-gray-500">
							<input
								type="radio"
								name="default-persona"
								checked={persona.is_default}
								on:change={() => setDefault(index)}
							/>
							{$i18n.t('Default')}
						</label>
						<button
							type="button"
							class="p-1 rounded-lg text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-850"
							aria-label={$i18n.t('Remove')}
							on:click={() => remove(index)}
						>
							<XMark className="size-3" />
						</button>
					</div>
					<textarea
						class="w-full resize-y bg-transparent text-xs outline-hidden placeholder:text-gray-300 dark:placeholder:text-gray-700"
						rows="2"
						placeholder={$i18n.t('Appearance, background, how characters know you')}
						aria-label={$i18n.t('Persona description')}
						bind:value={persona.description}
						on:input={save}
					></textarea>
				</div>
			</div>
		{/each}
	</div>

	<button
		type="button"
		class="mt-2 flex items-center gap-1 text-xs px-2 py-1 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-850"
		on:click={add}
	>
		<Plus className="size-3" />
		{$i18n.t('Add Persona')}
	</button>
</UserSettingSection>
