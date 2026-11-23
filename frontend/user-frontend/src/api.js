// 部署到子路径（例如 nginx 反代 /shopagent/user/）时，由 VITE_API_BASE 指定同源接口前缀，
// 各服务再把 segment 拼在后面，避免把 127.0.0.1:端口 打进 bundle（远端浏览器连不上）。
const API_BASE = import.meta.env.VITE_API_BASE || "";
const serviceUrl = (segment, host, port) =>
  API_BASE ? `${API_BASE}/${segment}` : `http://${host}:${port}`;

export const CUSTOMER_SERVICE_URL = serviceUrl(
  "customer",
  import.meta.env.CUSTOMER_SERVICE_HOST || "127.0.0.1",
  import.meta.env.CUSTOMER_SERVICE_PORT || "18000",
);
export const ECOMMERCE_SERVICE_URL = serviceUrl(
  "ecommerce",
  import.meta.env.ECOMMERCE_SERVICE_HOST || "127.0.0.1",
  import.meta.env.ECOMMERCE_SERVICE_PORT || "18001",
);

// WebSocket 必须是绝对地址：API_BASE 模式下 CUSTOMER_SERVICE_URL 是相对路径，
// 直接 replace(/^http/, "ws") 会保留相对路径，而 new WebSocket() 只接受绝对 URL。
const toWebSocketBase = (url) => {
  if (!url.startsWith("/")) return url.replace(/^http/, "ws");
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${window.location.host}${url}`;
};

async function parseResponse(response) {
  const body = await response
    .json()
    .catch(() => ({ detail: response.statusText }));
  if (!response.ok) {
    const detail =
      typeof body.detail === "string" ? body.detail : body.detail?.message;
    throw new Error(detail || body.message || response.statusText);
  }
  return body;
}

export async function login(userId) {
  return parseResponse(
    await fetch(`${ECOMMERCE_SERVICE_URL}/api/v1/auth/demo-token`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: userId, role: "user" }),
    }),
  );
}

export async function listDemoUsers() {
  return parseResponse(
    await fetch(`${ECOMMERCE_SERVICE_URL}/api/v1/demo/users`),
  );
}

export async function apiFetch(baseUrl, path, token, options = {}) {
  const headers = {
    Authorization: `Bearer ${token}`,
    ...(options.headers || {}),
  };
  return parseResponse(
    await fetch(`${baseUrl}${path}`, { ...options, headers }),
  );
}

export const customerApi = {
  history: (token) =>
    apiFetch(CUSTOMER_SERVICE_URL, "/api/v1/chat/history", token),
  send: (token, payload) =>
    apiFetch(CUSTOMER_SERVICE_URL, "/api/v1/chat/messages", token, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
};

export const commerceApi = {
  orders: (token) => apiFetch(ECOMMERCE_SERVICE_URL, "/api/v1/orders", token),
  products: (token) =>
    apiFetch(ECOMMERCE_SERVICE_URL, "/api/v1/products", token),
  order: (token, id) =>
    apiFetch(
      ECOMMERCE_SERVICE_URL,
      `/api/v1/orders/${encodeURIComponent(id)}`,
      token,
    ),
  logistics: (token, id) =>
    apiFetch(
      ECOMMERCE_SERVICE_URL,
      `/api/v1/orders/${encodeURIComponent(id)}/logistics`,
      token,
    ),
  afterSales: (token, orderId = "") =>
    apiFetch(
      ECOMMERCE_SERVICE_URL,
      `/api/v1/after-sales${orderId ? `?order_id=${encodeURIComponent(orderId)}` : ""}`,
      token,
    ),
  cancel: (token, id) =>
    apiFetch(
      ECOMMERCE_SERVICE_URL,
      `/api/v1/orders/${encodeURIComponent(id)}/cancel`,
      token,
      {
        method: "POST",
        headers: { "Idempotency-Key": newId("cancel") },
      },
    ),
  updateAddress: (token, id, address) =>
    apiFetch(
      ECOMMERCE_SERVICE_URL,
      `/api/v1/orders/${encodeURIComponent(id)}/address`,
      token,
      {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": newId("address"),
        },
        body: JSON.stringify({ address }),
      },
    ),
  createAfterSale: (token, id, payload) =>
    apiFetch(
      ECOMMERCE_SERVICE_URL,
      `/api/v1/orders/${encodeURIComponent(id)}/after-sales`,
      token,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": newId("after_sale"),
        },
        body: JSON.stringify(payload),
      },
    ),
};

export function createRealtimeConnection(token, callbacks = {}) {
  let socket;
  let stopped = false;
  let retryTimer;
  let attempts = 0;
  let hasConnected = false;
  const websocketUrl = `${toWebSocketBase(CUSTOMER_SERVICE_URL)}/api/v1/realtime`;

  const clearTimers = () => {
    window.clearTimeout(retryTimer);
  };
  const connect = () => {
    if (stopped) return;
    callbacks.onState?.(attempts ? "reconnecting" : "connecting");
    socket = new WebSocket(websocketUrl);
    socket.addEventListener("open", () =>
      socket.send(JSON.stringify({ type: "authenticate", token })),
    );
    socket.addEventListener("message", (browserEvent) => {
      const event = JSON.parse(browserEvent.data);
      if (event.event_type === "connected") {
        const reconnected = hasConnected;
        hasConnected = true;
        attempts = 0;
        callbacks.onState?.("connected");
        callbacks.onConnected?.({ reconnected });
      } else {
        callbacks.onEvent?.(event);
      }
    });
    socket.addEventListener("close", () => {
      clearTimers();
      if (stopped) return callbacks.onState?.("closed");
      callbacks.onState?.("reconnecting");
      retryTimer = window.setTimeout(
        connect,
        Math.min(1000 * 2 ** attempts++, 10000),
      );
    });
    socket.addEventListener("error", () => socket?.close());
  };
  connect();
  return {
    close() {
      stopped = true;
      clearTimers();
      socket?.close(1000, "client closed");
    },
  };
}

export function newId(prefix) {
  return `${prefix}_${Date.now()}_${Math.random().toString(16).slice(2)}`;
}
