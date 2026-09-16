/* Free browser discovery only. Paid data stays behind the existing access flows. */
(() => {
  const context = document.modelContext || navigator.modelContext;
  if (!context) return;

  async function readJson(path, params = {}) {
    const url = new URL(path, window.location.origin);
    for (const [key, value] of Object.entries(params)) url.searchParams.set(key, value);
    const response = await fetch(url, {
      method: "GET", credentials: "omit", headers: { Accept: "application/json" },
      signal: AbortSignal.timeout(10000),
    });
    if (!response.ok) throw new Error(`Discovery unavailable (HTTP ${response.status})`);
    return JSON.stringify(await response.json());
  }

  const tools = [{
    name: "search_blocksize_instruments",
    description: "Find Blocksize market-data instruments. Free catalog search; results are not live quotes.",
    inputSchema: {
      type: "object", additionalProperties: false, required: ["query"],
      properties: {
        query: { type: "string", minLength: 1, maxLength: 64 },
        asset_class: { type: "string", enum: ["all", "crypto", "equity", "fx", "metal"] },
      },
    },
    annotations: { readOnlyHint: true },
    async execute({ query, asset_class = "all" }) {
      if (typeof query !== "string" || !query.trim() || query.length > 64)
        throw new Error("Provide an instrument query between 1 and 64 characters.");
      if (!["all", "crypto", "equity", "fx", "metal"].includes(asset_class))
        throw new Error("Unsupported asset class.");
      return readJson("/v1/search", { q: query.trim(), asset_class });
    },
  }, {
    name: "get_blocksize_data_catalog",
    description: "Read Blocksize data products and integration routes without purchasing data.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: true },
    execute: () => readJson("/data-packages.json"),
  }];
  if (typeof context.registerTool === "function") {
    for (const tool of tools) context.registerTool(tool);
  } else if (typeof context.provideContext === "function") {
    context.provideContext({ tools });
  }
})();
