// SPDX-License-Identifier: GPL-3.0-only
// Catalog validation: no empty translations, missing parameters or accidental markup.
import { readFile } from 'node:fs/promises';
const messages = JSON.parse(await readFile(new URL('../src/i18n/messages.json', import.meta.url), 'utf8'));
let count = 0;
for (const [key, entries] of Object.entries(messages)) {
 if (!Array.isArray(entries) || entries.length !== 2 || entries.some(text => typeof text !== 'string' || !text.trim())) throw Error(`Incomplete SK/CZ: ${key}`);
 const params = text => [...text.matchAll(/\{([a-zA-Z]+)\}/g)].map(match => match[1]).sort().join(',');
 for (const text of entries) {
  if (params(key) !== params(text)) throw Error(`Parameter mismatch: ${key}`);
  if (/<\/?[a-z][^>]*>/i.test(text)) throw Error(`Unexpected markup: ${key}`);
 }
 count++;
}
console.log(`Validated ${count} paired SK/CZ messages and interpolation parameters`);
