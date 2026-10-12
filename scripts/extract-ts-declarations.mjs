// SPDX-License-Identifier: GPL-3.0-only
// Build-time extraction of chart contracts shared by Free and Pro.
import ts from '../web/node_modules/typescript/lib/typescript.js';
import { readFileSync } from 'node:fs';
const [path, ...requested] = process.argv.slice(2);
const source = readFileSync(path, 'utf8');
const file = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
const nodes = new Map();
for (const statement of file.statements) {
  const names = ts.isVariableStatement(statement)
    ? statement.declarationList.declarations.map(node => node.name.getText(file))
    : statement.name ? [statement.name.getText(file)] : [];
  for (const name of names) nodes.set(name, statement);
}
const retained = new Set(file.statements.filter(ts.isImportDeclaration));
const add = name => {
  const statement = nodes.get(name);
  if (!statement || retained.has(statement)) return;
  retained.add(statement);
  const visit = node => {
    if (ts.isIdentifier(node) && nodes.has(node.text)) add(node.text);
    ts.forEachChild(node, visit);
  };
  visit(statement);
};
for (const name of requested) {
  if (!nodes.has(name)) throw Error(`Shared declaration missing: ${name}`);
  add(name);
}
const used = new Set();
for (const node of retained) {
 if (ts.isImportDeclaration(node)) continue;
 const visit = child => { if (ts.isIdentifier(child)) used.add(child.text); ts.forEachChild(child, visit); };
 visit(node);
}
const printer = ts.createPrinter();
const chunks = [];
for (let node of file.statements) {
 if (!retained.has(node)) continue;
 if (ts.isImportDeclaration(node) && node.importClause) {
  const clause = node.importClause;
  const name = clause.name && used.has(clause.name.text) ? clause.name : undefined;
  let bindings = clause.namedBindings;
  if (bindings && ts.isNamedImports(bindings)) {
   const elements = bindings.elements.filter(element => used.has(element.name.text));
   bindings = elements.length ? ts.factory.updateNamedImports(bindings, elements) : undefined;
  } else if (bindings && !used.has(bindings.name.text)) bindings = undefined;
  if (!name && !bindings) continue;
  node = ts.factory.updateImportDeclaration(node, node.modifiers, ts.factory.updateImportClause(clause, clause.isTypeOnly, name, bindings), node.moduleSpecifier, node.attributes);
 }
 chunks.push(printer.printNode(ts.EmitHint.Unspecified, node, file));
}
process.stdout.write('// SPDX-License-Identifier: MIT\n// Shared night-chart geometry; Pro editing algorithms omitted.\n' +
 chunks.join('\n\n') + '\n');
