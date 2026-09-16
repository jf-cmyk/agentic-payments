import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../docs/assets/agent-tools.js', import.meta.url), 'utf8');
function load(api = 'registerTool', location = 'document') {
  const tools = [], requests = [];
  const context = api === 'registerTool'
    ? { registerTool: tool => tools.push(tool) }
    : { provideContext: value => tools.push(...value.tools) };
  const scope = {
    document: location === 'document' ? { modelContext: context } : {},
    navigator: location === 'navigator' ? { modelContext: context } : {},
    window: { location: { origin: 'https://staging.example.org' } },
    URL, AbortSignal,
    fetch: async (url, options) => {
      requests.push({ url: String(url), options });
      return { ok: true, json: async () => ({ found: true }) };
    },
  };
  vm.runInNewContext(source, scope);
  return { tools, requests, scope };
}
for (const [api, location] of [['registerTool', 'document'], ['registerTool', 'navigator'], ['provideContext', 'navigator']]) {
  const { tools, requests, scope } = load(api, location);
  assert.equal(tools.length, 2);
  assert.equal(await tools[0].execute({ query: 'EUR/USD', asset_class: 'fx' }), '{"found":true}');
  assert.equal(requests[0].url, 'https://staging.example.org/v1/search?q=EUR%2FUSD&asset_class=fx');
  assert.equal(requests[0].options.credentials, 'omit');
  await assert.rejects(tools[0].execute({ query: '' }));
  await assert.rejects(tools[0].execute({ query: 'BTC', asset_class: 'invalid' }));
  assert.equal(requests.length, 1);
  await tools[1].execute({});
  assert.equal(requests[1].url, 'https://staging.example.org/data-packages.json');
  scope.fetch = async () => ({ ok: false, status: 503 });
  await assert.rejects(tools[1].execute({}), /503/);
}
vm.runInNewContext(source, { document: {}, navigator: {} });
console.log('WebMCP tests passed: current and legacy registration, validation, encoding, failure handling, unsupported browser.');
