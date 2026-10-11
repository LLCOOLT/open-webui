import CRC32 from 'crc-32';
import JSZip from 'jszip';

// Client-side preview of a character card before import. The server parses and validates the
// card itself (backend/open_webui/utils/roleplay/cards.py); this only reads enough to show the
// admin what they are about to import.

export type CharacterPreview = {
	file: File;
	name: string;
	description: string;
	creatorNotes: string;
	spec: string;
	greetingCount: number;
	lorebookEntryCount: number;
	image: string | null;
	error: string | null;
};

const PNG_SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];
const ZIP_SIGNATURE = [0x50, 0x4b, 0x03, 0x04];

const startsWith = (data: Uint8Array, signature: number[]) =>
	signature.every((byte, index) => data[index] === byte);

const readPngTextChunks = (data: Uint8Array): Record<string, string> => {
	const view = new DataView(data.buffer, data.byteOffset, data.byteLength);
	const texts: Record<string, string> = {};
	let offset = PNG_SIGNATURE.length;

	while (offset + 12 <= data.length) {
		const length = view.getUint32(offset);
		const typeBytes = data.subarray(offset + 4, offset + 8);
		const type = String.fromCharCode(...typeBytes);
		const end = offset + 8 + length;
		if (end + 4 > data.length) throw new Error('Truncated PNG file');

		const chunk = data.subarray(offset + 8, end);
		// CRC32.buf returns a signed 32-bit integer; compare as unsigned.
		const crc = CRC32.buf(chunk, CRC32.buf(typeBytes)) >>> 0;
		if (crc !== view.getUint32(end)) throw new Error(`Invalid CRC for PNG chunk "${type}"`);

		if (type === 'tEXt') {
			const separator = chunk.indexOf(0);
			if (separator > 0) {
				const keyword = String.fromCharCode(...chunk.subarray(0, separator));
				texts[keyword] ??= new TextDecoder('latin1').decode(chunk.subarray(separator + 1));
			}
		}
		if (type === 'IEND') break;
		offset = end + 4;
	}
	return texts;
};

const decodeCardText = (text: string) => {
	const trimmed = text.trim();
	if (trimmed.startsWith('{')) return JSON.parse(trimmed);
	const bytes = Uint8Array.from(atob(trimmed), (char) => char.charCodeAt(0));
	return JSON.parse(new TextDecoder().decode(bytes));
};

const text = (value: unknown) => (typeof value === 'string' ? value.trim() : '');

const summarize = (card: any) => {
	if (card?.spec === 'chara_card_v3' || card?.spec === 'chara_card_v2' || card?.data?.name) {
		const data = card.data ?? {};
		const greetings = [data.first_mes, ...(data.alternate_greetings ?? [])];
		return {
			spec: card.spec === 'chara_card_v3' ? 'V3' : 'V2',
			name: text(data.name),
			description: text(data.description),
			creatorNotes: text(data.creator_notes),
			greetingCount: greetings.filter((greeting) => text(greeting)).length,
			lorebookEntryCount: Object.keys(data.character_book?.entries ?? {}).length
		};
	}
	if (card?.char_name) {
		return {
			spec: 'Text Generation',
			name: text(card.char_name),
			description: text(card.char_persona),
			creatorNotes: '',
			greetingCount: text(card.char_greeting) ? 1 : 0,
			lorebookEntryCount: 0
		};
	}
	const character = card?.character ?? card?.info?.character;
	if (character) {
		return {
			spec: 'Character.AI',
			name: text(character.name),
			description: text(character.description),
			creatorNotes: text(character.title),
			greetingCount: text(character.greeting) ? 1 : 0,
			lorebookEntryCount: 0
		};
	}
	return {
		spec: 'V1',
		name: text(card?.name),
		description: text(card?.description),
		creatorNotes: '',
		greetingCount: text(card?.first_mes) ? 1 : 0,
		lorebookEntryCount: 0
	};
};

const charxIcon = async (zip: JSZip, card: any): Promise<string | null> => {
	const assets: any[] = card?.data?.assets ?? [];
	const icons = assets.filter((asset) => asset?.type === 'icon');
	const icon = icons.find((asset) => asset?.name === 'main') ?? icons[0];
	const uri: string = icon?.uri ?? '';
	const prefix = ['embeded://', 'embedded://'].find((value) => uri.startsWith(value));
	const entry = prefix ? zip.file(uri.slice(prefix.length).replace(/^\//, '')) : null;
	return entry ? URL.createObjectURL(await entry.async('blob')) : null;
};

const readCard = async (file: File): Promise<{ card: any; image: string | null }> => {
	const data = new Uint8Array(await file.arrayBuffer());

	if (startsWith(data, PNG_SIGNATURE)) {
		const texts = readPngTextChunks(data);
		const cardText = texts['ccv3'] ?? texts['chara'];
		if (!cardText) throw new Error('This PNG does not contain a character card');
		return { card: decodeCardText(cardText), image: URL.createObjectURL(file) };
	}

	if (startsWith(data, ZIP_SIGNATURE)) {
		const zip = await JSZip.loadAsync(data);
		const cardFile = zip.file('card.json');
		if (!cardFile) throw new Error('This archive has no card.json');
		const card = JSON.parse(await cardFile.async('string'));
		return { card, image: await charxIcon(zip, card) };
	}

	return { card: JSON.parse(new TextDecoder().decode(data).replace(/^\uFEFF/, '')), image: null };
};

export const previewCharacterFile = async (file: File): Promise<CharacterPreview> => {
	const empty = {
		file,
		name: file.name,
		description: '',
		creatorNotes: '',
		spec: '',
		greetingCount: 0,
		lorebookEntryCount: 0,
		image: null
	};
	try {
		const { card, image } = await readCard(file);
		const summary = summarize(card);
		if (!summary.name) throw new Error('The card has no name');
		return { ...empty, ...summary, image, error: null };
	} catch (error) {
		return { ...empty, error: error instanceof Error ? error.message : `${error}` };
	}
};

export const revokePreviewImage = (preview: CharacterPreview) => {
	if (preview.image) URL.revokeObjectURL(preview.image);
};
