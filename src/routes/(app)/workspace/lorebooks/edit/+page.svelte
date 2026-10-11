<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import { getContext, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';

	import {
		getLorebookById,
		updateLorebookById,
		type Lorebook,
		type LorebookForm
	} from '$lib/apis/lorebooks';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import LorebookEditor from '$lib/components/workspace/Lorebooks/LorebookEditor.svelte';

	const i18n: any = getContext('i18n');

	let lorebook: Lorebook | null = null;

	const onSubmit = async (form: LorebookForm) => {
		if (!lorebook) return;
		try {
			lorebook = {
				...(await updateLorebookById(localStorage.token, lorebook.id, form)),
				write_access: true
			};
			toast.success($i18n.t('Lorebook updated'));
		} catch (error: any) {
			toast.error(`${error?.detail ?? error}`);
		}
	};

	onMount(async () => {
		const id = $page.url.searchParams.get('id');
		try {
			lorebook = id ? await getLorebookById(localStorage.token, id) : null;
		} catch {
			lorebook = null;
		}
		if (!lorebook) {
			toast.error($i18n.t('Lorebook not found'));
			goto('/workspace/lorebooks');
		}
	});
</script>

{#if lorebook}
	{#key lorebook.id}
		<LorebookEditor {lorebook} {onSubmit} onBack={() => goto('/workspace/lorebooks')} />
	{/key}
{:else}
	<div class="flex justify-center py-10"><Spinner className="size-5" /></div>
{/if}
