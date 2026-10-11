<script lang="ts">
	import { goto } from '$app/navigation';
	import { getContext } from 'svelte';
	import { toast } from 'svelte-sonner';

	import { createLorebook, type LorebookForm } from '$lib/apis/lorebooks';
	import LorebookEditor from '$lib/components/workspace/Lorebooks/LorebookEditor.svelte';

	const i18n: any = getContext('i18n');

	const onSubmit = async (form: LorebookForm) => {
		try {
			const created = await createLorebook(localStorage.token, form);
			toast.success($i18n.t('Lorebook created'));
			await goto(`/workspace/lorebooks/edit?id=${encodeURIComponent(created.id)}`);
		} catch (error: any) {
			toast.error(`${error?.detail ?? error}`);
		}
	};
</script>

<LorebookEditor {onSubmit} onBack={() => goto('/workspace/lorebooks')} />
