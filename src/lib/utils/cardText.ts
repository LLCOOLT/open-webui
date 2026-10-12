import DOMPurify from 'dompurify';
import { marked } from 'marked';

// Tags that mean a card's text is written as HTML (as many Chub and RisuRealm cards are), rather
// than plain text that merely contains angle brackets like <START> or <user>.
const HTML_TAG_RE =
	/<\/?(?:a|b|i|u|s|p|br|hr|em|strong|small|big|sub|sup|div|span|img|center|font|h[1-6]|ul|ol|li|table|thead|tbody|tr|td|th|blockquote|pre|code|details|summary|style|figure|figcaption|audio|video|source|mark|del|ins)\b[^>]*>/i;

/** Card text as safe HTML: markdown is rendered, HTML is kept, anything active is removed. */
export const renderCardText = (text: string): string => {
	const source = HTML_TAG_RE.test(text) ? text : text.replace(/</g, '&lt;');
	const html = marked.parse(source, { breaks: true, gfm: true, async: false }) as string;
	return DOMPurify.sanitize(html, {
		FORCE_BODY: true, // keep a leading <style>
		ADD_TAGS: ['style'],
		FORBID_TAGS: [
			'script',
			'iframe',
			'frame',
			'object',
			'embed',
			'form',
			'input',
			'button',
			'textarea',
			'select',
			'meta',
			'link',
			'base'
		]
	});
};

/** Text only, for one-line previews. */
export const cardPlainText = (text: string): string => {
	if (!HTML_TAG_RE.test(text ?? '')) return text ?? '';
	const doc = new DOMParser().parseFromString(DOMPurify.sanitize(text), 'text/html');
	doc.querySelectorAll('style').forEach((node) => node.remove());
	return (doc.body.textContent ?? '').replace(/\n{3,}/g, '\n\n').trim();
};
