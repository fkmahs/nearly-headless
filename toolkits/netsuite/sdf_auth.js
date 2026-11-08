// Answers the `suitecloud account:setup -d` prompts for one NEW auth ID, so the only
// human step is clicking Allow in the browser. Called by the SDF kit's `login sb|prod`; don't run it directly.
// -d pins the login to the account's own domain; without it NetSuite reuses whichever account
// the browser is signed in to (a production auth ID can end up bound to a sandbox).
// Stops on any prompt it doesn't recognise.
'use strict';
const { spawn } = require('child_process');

const [authId, domain] = process.argv.slice(2);
if (!authId || !/^[a-z0-9-]+\.app\.netsuite\.com$/i.test(domain || '')) {
  console.error('sdf_auth: usage: sdf_auth.js <auth-id> <account>.app.netsuite.com');
  process.exit(2);
}

const child = spawn('suitecloud account:setup -d', { shell: true, stdio: ['pipe', 'pipe', 'pipe'] });
let buf = '';
const answered = new Set();
const strip = s => s.replace(/\x1b\[[0-9;?]*[A-Za-z]/g, '');

function answer(key, keys) {
  if (answered.has(key)) return;
  answered.add(key);
  setTimeout(() => child.stdin.write(keys), 400);
  if (key === 'auth-id') console.log(`\nsdf_auth: waiting for Allow in the browser (${domain}) ...`);
}

function stop(why) {
  console.error(`\nsdf_auth: ${why}; stopping`);
  child.kill();
  process.exit(3);
}

function onData(data) {
  const text = strip(data.toString());
  buf += text;
  if (/authenticated|successfully|error/i.test(text)) process.stdout.write(text);
  const i = buf.lastIndexOf('? ');
  const prompt = i >= 0 ? buf.slice(i) : buf; // only the latest prompt
  if (/^\? Select or create an authentication ID/.test(prompt) && /Create a new authentication ID/.test(prompt)) {
    if (!/[❯>] Create a new authentication ID/.test(prompt)) return stop('menu cursor is not on "Create a new authentication ID"');
    answer('create-new', '\r');
  } else if (/^\? Enter your NetSuite domain/.test(prompt)) {
    answer('domain', domain + '\r');
  } else if (/^\? Enter an authentication ID/.test(prompt)) {
    answer('auth-id', authId + '\r');
  } else if (/^\? /.test(prompt) && /:\s*$/.test(prompt)) {
    stop('unrecognised prompt: ' + prompt.slice(0, 120).replace(/\s+/g, ' '));
  }
}

child.stdout.on('data', onData);
child.stderr.on('data', onData);
child.on('exit', code => process.exit(code ?? 1));
setTimeout(() => stop('no Allow within 10 minutes'), 600000);
