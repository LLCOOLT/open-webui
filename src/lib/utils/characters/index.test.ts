import CRC32 from 'crc-32';
import JSZip from 'jszip';
import { describe, expect, it } from 'vitest';

import { previewCharacterFile } from './index';

const encoder = new TextEncoder();

const chunk = (type: string, data: Uint8Array) => {
	const typeBytes = encoder.encode(type);
	const out = new Uint8Array(12 + data.length);
	const view = new DataView(out.buffer);
	view.setUint32(0, data.length);
	out.set(typeBytes, 4);
	out.set(data, 8);
	view.setUint32(8 + data.length, CRC32.buf(data, CRC32.buf(typeBytes)) >>> 0);
	return out;
};

const png = (texts: Record<string, string>, corrupt = false) => {
	const parts = [
		new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
		chunk('IHDR', new Uint8Array(13)),
		...Object.entries(texts).map(([keyword, text]) =>
			chunk('tEXt', encoder.encode(`${keyword}\0${text}`))
		),
		chunk('IEND', new Uint8Array())
	];
	const bytes = new Uint8Array(parts.reduce((size, part) => size + part.length, 0));
	let offset = 0;
	for (const part of parts) {
		bytes.set(part, offset);
		offset += part.length;
	}
	if (corrupt) bytes[30] ^= 0xff;
	return bytes;
};

const base64Json = (value: object) =>
	btoa(String.fromCharCode(...encoder.encode(JSON.stringify(value))));

const v2 = {
	spec: 'chara_card_v2',
	data: {
		name: 'Seraphina',
		description: 'Guardian',
		first_mes: 'Hi',
		alternate_greetings: ['Hello', ''],
		character_book: { entries: [{ keys: ['a'], content: 'b' }] }
	}
};
const v3 = { spec: 'chara_card_v3', data: { ...v2.data, name: 'Sera' } };

describe('previewCharacterFile', () => {
	it('reads JSON cards', async () => {
		const preview = await previewCharacterFile(new File([JSON.stringify(v2)], 'sera.json'));
		expect(preview).toMatchObject({
			name: 'Seraphina',
			spec: 'V2',
			greetingCount: 2,
			lorebookEntryCount: 1,
			error: null
		});
	});

	it('prefers the ccv3 chunk in PNG cards', async () => {
		const file = new File([png({ chara: base64Json(v2), ccv3: base64Json(v3) })], 'sera.png');
		const preview = await previewCharacterFile(file);
		expect(preview.name).toBe('Sera');
		expect(preview.spec).toBe('V3');
		expect(preview.image).toMatch(/^blob:/);
	});

	it('reports a corrupt PNG instead of throwing', async () => {
		const preview = await previewCharacterFile(
			new File([png({ chara: base64Json(v2) }, true)], 'bad.png')
		);
		expect(preview.error).toMatch(/CRC/);
	});

	it('reads CHARX archives', async () => {
		const zip = new JSZip();
		zip.file('card.json', JSON.stringify(v3));
		const file = new File([await zip.generateAsync({ type: 'uint8array' })], 'sera.charx');
		expect((await previewCharacterFile(file)).name).toBe('Sera');
	});

	it('reads legacy formats and rejects nameless cards', async () => {
		const textgen = { char_name: 'Bot', char_persona: 'Robot', char_greeting: 'Beep' };
		expect(
			await previewCharacterFile(new File([JSON.stringify(textgen)], 'bot.json'))
		).toMatchObject({ name: 'Bot', spec: 'Text Generation', greetingCount: 1 });

		const nameless = await previewCharacterFile(new File(['{"description": "x"}'], 'x.json'));
		expect(nameless.error).toBeTruthy();
	});
});
