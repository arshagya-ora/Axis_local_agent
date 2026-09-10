export class ApiError extends Error {
  constructor(message, code = "unavailable", detail = {}) {
    super(message);
    this.code = code;
    this.detail = detail;
  }
}
export class AxisApi {
  constructor() {
    this.url = "http://127.0.0.1:8766";
    this.token = "";
  }
  async load() {
    const value = await chrome.storage.local.get([
      "axisServiceUrl",
      "axisUiToken",
    ]);
    this.configure(value.axisServiceUrl || this.url, value.axisUiToken || "");
  }
  configure(url, token) {
    const parsed = new URL(url);
    if (
      parsed.protocol !== "http:" ||
      parsed.hostname !== "127.0.0.1" ||
      parsed.username ||
      parsed.password ||
      parsed.pathname !== "/" ||
      parsed.search ||
      parsed.hash
    )
      throw new Error("Use a loopback URL such as http://127.0.0.1:8766.");
    this.url = parsed.origin;
    this.token = token.trim();
  }
  async request(path, { method = "GET", body, signal, stream = false, binary = false } = {}) {
    if (!this.token)
      throw new ApiError(
        "Pair the local AXIS service in Settings.",
        "not_paired",
      );
    let response;
    try {
      response = await fetch(this.url + path, {
        method,
        headers: {
          Authorization: `Bearer ${this.token}`,
          ...(body ? { "Content-Type": binary ? "application/octet-stream" : "application/json" } : {}),
        },
        body: body ? (binary ? body : JSON.stringify(body)) : undefined,
        signal: signal || AbortSignal.timeout(binary ? 120000 : 10000),
        cache: "no-store",
        credentials: "omit",
      });
    } catch (error) {
      if (signal?.aborted) throw error;
      throw new ApiError(
        "AXIS service is offline or unreachable. Check the service address and startup instructions.",
      );
    }
    if (!response.ok) {
      let data;
      try {
        data = await response.json();
      } catch {
        data = {};
      }
      throw new ApiError(
        data.message ||
          (response.status === 401
            ? "Pairing credential was rejected."
            : "The service could not accept this request."),
        data.code || "unavailable",
        data,
      );
    }
    return stream ? response : response.json();
  }
  async stream(after, onEvent, signal) {
    const response = await this.request(`/api/events?after=${after}`, {
      stream: true,
      signal,
    });
    const reader = response.body.getReader(),
      decoder = new TextDecoder();
    let buffer = "";
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder
          .decode(value, { stream: true })
          .replace(/\r\n/g, "\n");
        if (buffer.length > 1000000)
          throw new Error("Event stream exceeded its buffer limit.");
        let boundary;
        while ((boundary = buffer.indexOf("\n\n")) >= 0) {
          const block = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          const data = block
            .split("\n")
            .filter((line) => line.startsWith("data:"))
            .map((line) => line.slice(5).trimStart())
            .join("\n");
          if (data) await onEvent(JSON.parse(data));
        }
      }
    } finally {
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }
}
