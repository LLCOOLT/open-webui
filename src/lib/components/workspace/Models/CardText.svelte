<script lang="ts">
	import { onDestroy } from 'svelte';

	import { renderCardText } from '$lib/utils/cardText';

	// Card text (creator notes, description, greetings) rendered the way the card's author laid it
	// out. It goes in a sandboxed frame without scripts, so a card's own CSS cannot restyle the page.
	export let text = '';

	let frame: HTMLIFrameElement;
	let observer: ResizeObserver | null = null;

	const dark = () =>
		typeof document !== 'undefined' && document.documentElement.classList.contains('dark');

	$: srcdoc = `<!doctype html><html><head><meta charset="utf-8"><base target="_blank"><style>
html{color-scheme:${dark() ? 'dark' : 'light'}}
body{margin:0;background:transparent;color:${dark() ? '#d1d5db' : '#374151'};font:14px/1.55 ui-sans-serif,system-ui,sans-serif;overflow-wrap:anywhere}
img,video{max-width:100%;height:auto}
a{color:${dark() ? '#7dd3fc' : '#0369a1'}}
p{margin:0 0 .6em}p:last-child{margin-bottom:0}
pre{white-space:pre-wrap}
table{max-width:100%}
</style></head><body>${renderCardText(text ?? '')}</body></html>`;

	const fit = () => {
		const doc = frame?.contentDocument;
		if (!doc?.documentElement) return;
		frame.style.height = `${Math.ceil(doc.documentElement.scrollHeight)}px`;
	};

	const onLoad = () => {
		fit();
		observer?.disconnect();
		const body = frame?.contentDocument?.body;
		if (body && typeof ResizeObserver !== 'undefined') {
			observer = new ResizeObserver(fit);
			observer.observe(body);
		}
	};

	onDestroy(() => observer?.disconnect());
</script>

<iframe
	bind:this={frame}
	title="Card text"
	class="w-full block border-0 min-h-5"
	sandbox="allow-same-origin allow-popups allow-popups-to-escape-sandbox"
	referrerpolicy="no-referrer"
	{srcdoc}
	on:load={onLoad}
></iframe>
