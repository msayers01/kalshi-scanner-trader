// REST helpers. Token (if the backend requires one) is kept in localStorage and sent as a Bearer header.

const TOKEN_KEY = "kst.token";

export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setToken(t: string): void {
  try {
    localStorage.setItem(TOKEN_KEY, t);
  } catch {
    /* private mode: token lives for this page only */
  }
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const r = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  if (!r.ok) {
    let msg = r.statusText;
    try {
      const j = await r.json();
      msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? j);
    } catch {
      /* non-JSON error */
    }
    if (r.status === 401) {
      const t = window.prompt("kst API token required:");
      if (t) {
        setToken(t);
        return call<T>(method, path, body);
      }
    }
    throw new ApiError(r.status, msg);
  }
  const ct = r.headers.get("content-type") ?? "";
  return (ct.includes("json") ? r.json() : r.text()) as Promise<T>;
}

export const api = {
  get: <T>(p: string) => call<T>("GET", p),
  post: <T>(p: string, body: unknown = {}) => call<T>("POST", p, body),
  del: <T>(p: string) => call<T>("DELETE", p),
};
