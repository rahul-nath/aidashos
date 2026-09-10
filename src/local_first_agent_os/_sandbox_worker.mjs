// SPDX-License-Identifier: AGPL-3.0-or-later
// Trusted process host. The caller supervises this entire process group.
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFile, realpath } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';

const [runtimeRoot, requestDigest, requestPath] = process.argv.slice(2);
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');
const requestBytes = await readFile(requestPath);
if (sha256(requestBytes) !== requestDigest) throw new Error('Tool-worker launch request changed');
const request = JSON.parse(requestBytes.toString('utf8'));
if (request.version !== 1) throw new Error('Unsupported tool-worker launch protocol');
if (await realpath(request.argv[0]) !== request.serviceExecutable ||
    sha256(await readFile(request.serviceExecutable)) !== request.serviceSha256) {
  throw new Error('Tool-worker service executable changed');
}
const { SandboxManager, SandboxRuntimeConfigSchema } = await import(
  pathToFileURL(`${runtimeRoot}/dist/index.js`).href
);
const config = SandboxRuntimeConfigSchema.parse(request.config);
const quote = value => `'${value.replaceAll("'", "'\\''")}'`;
let child;
try {
  await SandboxManager.initialize(config, undefined, false);
  const wrapped = await SandboxManager.wrapWithSandboxArgv(
    request.argv.map(quote).join(' '), '/bin/sh', undefined, undefined, request.cwd,
  );
  child = spawn(wrapped.argv[0], wrapped.argv.slice(1), {
    cwd: request.cwd, env: request.env, stdio: ['pipe', 'pipe', 'inherit'],
  });
  // The host owns both RPC pipes. Inherited stdio would let a surviving native
  // child answer fresh readiness RPCs after this policy host had died.
  const completion = new Promise((resolve, reject) => {
    child.once('error', reject);
    child.once('close', (code, signal) => resolve(code ?? (signal ? 128 : 125)));
    for (const stream of [process.stdin, child.stdin, child.stdout, process.stdout]) {
      stream.once('error', reject);
    }
  });
  process.stdin.pipe(child.stdin);
  child.stdout.pipe(process.stdout, { end: false });
  process.exitCode = await completion;
} finally {
  if (child?.stdin) process.stdin.unpipe(child.stdin);
  if (child?.stdout) child.stdout.unpipe(process.stdout);
  process.stdin.pause();
  if (child && child.exitCode === null) child.kill('SIGKILL');
  await SandboxManager.reset();
}
