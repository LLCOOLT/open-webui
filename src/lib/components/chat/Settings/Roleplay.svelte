<script lang="ts">
	// Settings > Roleplay: who you are (personas) and how character chats look and sound, in one
	// place. The same preferences also appear in the admin's default interface settings.
	import { getContext } from 'svelte';
	import { config, settings } from '$lib/stores';
	import Switch from '$lib/components/common/Switch.svelte';
	import Personas from './Personas.svelte';
	import UserSettingSection from './UserSettingSection.svelte';

	const i18n: any = getContext('i18n');

	export let saveSettings: (settings: any) => void | Promise<void>;

	$: defaults = ($config?.ui?.default_interface_settings ?? {}) as Record<string, any>;
	// The user's choice, else the admin's default, else the built-in one.
	$: current = { ...defaults, ...(($settings ?? {}) as Record<string, any>) };

	$: options = [
		{
			key: 'roleplayFormatting',
			fallback: true,
			label: $i18n.t('Roleplay formatting'),
			description: $i18n.t(
				'In chats with characters, show *actions* in muted italics and "dialogue" in a stronger color.'
			)
		},
		{
			key: 'characterCardGreeting',
			fallback: false,
			label: $i18n.t("Open new chats with the card's greeting"),
			description: $i18n.t(
				'When off, a new chat with a character starts empty, so it can carry on from what the character remembers; its greetings are offered to pick from instead.'
			)
		},
		{
			key: 'characterExpressions',
			fallback: true,
			label: $i18n.t('Character expressions'),
			description: $i18n.t(
				"For characters with expression images, pick the one that fits each reply (using the task model) and show it as the character's picture."
			)
		},
		{
			key: 'characterPortrait',
			fallback: true,
			requires: 'characterExpressions',
			label: $i18n.t('Show character portrait'),
			description: $i18n.t(
				"On wide screens, show the character's current expression beside the chat."
			)
		},
		{
			key: 'autoPlayCharacterVoice',
			fallback: false,
			label: $i18n.t('Auto-play character voices'),
			description: $i18n.t('Read replies aloud when the character has its own voice set.')
		},
		{
			key: 'expressiveSpeech',
			fallback: true,
			hidden: !($config?.audio?.tts?.expressive ?? true),
			label: $i18n.t('Expressive character voices'),
			description: $i18n.t(
				"Speak each character reply in the emotion picked for it (using the task model), on voice engines that can: OpenAI's newer speech models, OpenAI Realtime, ElevenLabs and Azure."
			)
		}
	];

	const toggle = (key: string, state: boolean) => saveSettings({ [key]: state });
</script>

<div class="flex flex-col h-full justify-between text-sm" id="tab-roleplay">
	<div class="overflow-y-auto max-h-[28rem] md:max-h-full pr-1">
		<Personas {saveSettings} />

		<UserSettingSection title={$i18n.t('Character chats')}>
			<div class="flex flex-col gap-3">
				{#each options as option (option.key)}
					{#if !option.hidden && (!option.requires || (current[option.requires] ?? true))}
						<div>
							<div class="flex items-center justify-between gap-2.5">
								<div
									id="roleplay-{option.key}-label"
									class="text-xs text-gray-600 dark:text-gray-400"
								>
									{option.label}
								</div>
								<Switch
									ariaLabelledbyId="roleplay-{option.key}-label"
									state={current[option.key] ?? option.fallback}
									on:change={(event) => toggle(option.key, event.detail)}
								/>
							</div>
							<p class="mt-1.5 text-[0.6875rem] text-gray-600 dark:text-gray-500">
								{option.description}
							</p>
						</div>
					{/if}
				{/each}
			</div>
		</UserSettingSection>
	</div>
</div>
