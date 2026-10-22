import {stripTypeScriptTypes} from 'node:module';
import {readdir, readFile, mkdir, writeFile, rm} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {join, dirname} from 'node:path';
const root = dirname(fileURLToPath(import.meta.url));
await rm(join(root, 'generated'), {recursive: true, force: true});
async function build(relative = '') {
  for (const entry of await readdir(join(root, 'src', relative), {withFileTypes: true})) {
    const name = join(relative, entry.name);
    if (entry.isDirectory()) { await build(name); continue; }
    if (!name.endsWith('.ts')) continue;
    const source = await readFile(join(root, 'src', name), 'utf8');
    const output = join(root, 'generated', name.replace(/\.ts$/, '.js'));
    await mkdir(dirname(output), {recursive: true});
    await writeFile(output, '// Generated from MIT-licensed workflow source; see src/LICENSE.\n' +
      stripTypeScriptTypes(source, {mode: 'transform'}));
  }
}
await build();
const {workflowTool} = await import('./tool.mjs');
await writeFile(join(root, 'generated/workflow-tool.json'), JSON.stringify(await workflowTool(), null, 2) + '\n');
console.log('Built Workflow runtime and tool descriptor in generated/.');
