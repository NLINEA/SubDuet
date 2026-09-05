"use strict";

// Pure URL policy: no requests, credentials, storage, or DOM access.
const aiConnections = (() => {
  const presets = Object.freeze({
    openai: "https://api.openai.com/v1",
    zai: "https://api.z.ai/api/paas/v4",
    custom: "",
    local: "",
  });

  function loopback(host) {
    const normalized = host.toLowerCase().replace(/^\[|\]$/g, "").replace(/\.$/, "");
    return normalized === "localhost" || normalized.endsWith(".localhost")
      || normalized === "::1" || /^127(?:\.\d{1,3}){3}$/.test(normalized);
  }

  function describe(provider, raw) {
    if (!Object.hasOwn(presets, provider)) {
      throw new Error("Choose your AI provider first.");
    }
    if (!raw || /[\s\u0000-\u001F\u007F\\?#]/.test(raw)) {
      throw new Error("Enter an endpoint without credentials, whitespace, query strings or fragments.");
    }
    const authority = raw.match(/^https?:\/\/([^/]+)/i)?.[1];
    if (!authority || /[@%]/.test(authority)) {
      throw new Error("Use a complete endpoint without embedded credentials or an encoded host.");
    }
    let url;
    try {
      url = new URL(raw);
    } catch {
      throw new Error("Enter a complete AI endpoint URL.");
    }
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) {
      throw new Error("Use an HTTP or HTTPS endpoint without embedded credentials.");
    }
    if (url.protocol === "http:" && !loopback(url.hostname)) {
      throw new Error("Use HTTPS for remote AI; HTTP is only allowed on this device.");
    }
    if (provider === "local" && !loopback(url.hostname)) {
      throw new Error("Local AI must use a loopback host on this device.");
    }
    if (presets[provider] && url.origin !== new URL(presets[provider]).origin) {
      throw new Error("This endpoint does not match the selected provider.");
    }
    return { origin: url.origin, baseUrl: url.href.replace(/\/+$/, ""), local: loopback(url.hostname) };
  }

  return Object.freeze({ presets, describe });
})();
