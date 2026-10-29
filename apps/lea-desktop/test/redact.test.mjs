import { test } from 'node:test';
import assert from 'node:assert/strict';
import { REDACTED, redactToml } from '../src/redact.mjs';

const SAMPLE = `# Lea config
model = "gemini/gemini-3.1-pro-preview"
max_turns = 20
anthropic_api_key = "sk-ant-top-level"
# openai_api_key = "sk-commented-out"
# max_spend_usd = 25.0
openai.api_key = "sk-dotted"
"MISTRAL_API_KEY" = "quoted-key"
PORTKEY_BASE_URL = "https://gateway.example.edu/v1"

[keys]
google = "AIza-google"
anthropic = 'sk-ant-table'
note = """multi
line
secret"""
after = "still-in-keys"

[provider_endpoints]
HOSTED_VLLM_API_BASE = "http://localhost:8000/v1"
HOSTED_VLLM_API_KEY = "optional"
OLLAMA_API_BASE = "http://alice:hunter2@ollama.local:11434"
label = "lab server"   # inline comment

[[provider_endpoints.servers]]
url = "http://localhost:1234/v1"
token = "srv-token"

[environment]
LEA_JOB_TIMEOUT_SECONDS = "1800"
HTTPS_PROXY = "http://user:pw@proxy.example.edu:3128"
# OLD_VAR = "commented-env"

[environment.extra]
ANY = "x"

[other]
title = "kept"
`;

test('secret-shaped keys are redacted everywhere, other values survive', () => {
  const out = redactToml(SAMPLE);
  assert.match(out, /^anthropic_api_key = "<redacted>"$/m);
  assert.match(out, /^openai\.api_key = "<redacted>"$/m);
  assert.match(out, /^"MISTRAL_API_KEY" = "<redacted>"$/m);
  assert.match(out, /^HOSTED_VLLM_API_KEY = "<redacted>"$/m);
  assert.match(out, /^token = "<redacted>"$/m);
  assert.match(out, /^model = "gemini\/gemini-3\.1-pro-preview"$/m);
  assert.match(out, /^max_turns = 20$/m);
  assert.match(out, /^PORTKEY_BASE_URL = "https:\/\/gateway\.example\.edu\/v1"$/m);
  assert.match(out, /^HOSTED_VLLM_API_BASE = "http:\/\/localhost:8000\/v1"$/m);
  assert.match(out, /^label = "lab server"   # inline comment$/m);
  assert.match(out, /^url = "http:\/\/localhost:1234\/v1"$/m);
  assert.match(out, /^title = "kept"$/m);
  for (const leak of ['sk-ant-top-level', 'sk-dotted', 'quoted-key', 'optional', 'srv-token']) {
    assert.ok(!out.includes(leak), `leaked ${leak}`);
  }
});

test('every value under [keys] and [environment] (and sub-tables) is redacted, the keys stay', () => {
  const out = redactToml(SAMPLE);
  assert.match(out, /^google = "<redacted>"$/m);
  assert.match(out, /^anthropic = "<redacted>"$/m);
  assert.match(out, /^after = "<redacted>"$/m);
  assert.match(out, /^LEA_JOB_TIMEOUT_SECONDS = "<redacted>"$/m);
  assert.match(out, /^HTTPS_PROXY = "<redacted>"$/m);
  assert.match(out, /^ANY = "<redacted>"$/m);
  for (const leak of ['AIza-google', 'sk-ant-table', 'still-in-keys', '1800', 'proxy.example.edu', '"x"']) {
    assert.ok(!out.includes(leak), `leaked ${leak}`);
  }
  // table headers are kept verbatim
  for (const header of ['[keys]', '[provider_endpoints]', '[[provider_endpoints.servers]]', '[environment]', '[environment.extra]', '[other]']) {
    assert.ok(out.includes(header), `lost ${header}`);
  }
});

test('commented-out secrets are redacted, commented-out non-secrets are kept', () => {
  const out = redactToml(SAMPLE);
  assert.match(out, /^# openai_api_key = "<redacted>"$/m);
  assert.match(out, /^# OLD_VAR = "<redacted>"$/m);
  assert.match(out, /^# max_spend_usd = 25\.0$/m);
  assert.ok(!out.includes('sk-commented-out'));
  assert.ok(!out.includes('commented-env'));
});

test('a commented-out [environment] header only affects commented lines', () => {
  const out = redactToml('# [environment]\n# FOO = "bar"\nmodel = "m"\n');
  assert.equal(out, `# [environment]\n# FOO = ${REDACTED}\nmodel = "m"\n`);
});

test('multi-line secret strings are dropped whole', () => {
  const out = redactToml(SAMPLE);
  assert.match(out, /^note = "<redacted>"$/m);
  assert.ok(!out.includes('multi'));
  assert.ok(!out.includes('secret"""'));
  assert.ok(!/^line$/m.test(out));
  // the assignment after the multi-line string is still seen
  assert.match(out, /^after = "<redacted>"$/m);
});

test('user:password in a kept URL is redacted', () => {
  const out = redactToml(SAMPLE);
  assert.match(out, /^OLLAMA_API_BASE = "http:\/\/<redacted>@ollama\.local:11434"$/m);
  assert.ok(!out.includes('hunter2'));
});

test('empty values, comments and blank lines pass through unchanged', () => {
  const text = '\n# just a comment\nempty =\nkeyless = # only a comment\n\n';
  assert.equal(redactToml(text), text);
});
