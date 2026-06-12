/**
 * Minimal ambient declarations for the Node.js builtins this package uses.
 *
 * The package intentionally has no runtime dependencies and only
 * `typescript` + `vitest` as devDependencies, so `@types/node` is not
 * available. Only the exact APIs used by src/ and test/ are declared here;
 * extend this file if new builtins are needed.
 */

/** The Node.js global console — only the methods this package uses. */
declare var console: {
  warn(message?: unknown, ...optionalParams: unknown[]): void;
};

/** The Node.js (18+) global fetch — only the surface HttpSink uses. */
declare function fetch(
  url: string,
  init?: {
    method?: string;
    headers?: Record<string, string>;
    body?: string;
  },
): Promise<{ ok: boolean; status: number; text(): Promise<string> }>;

declare module "node:crypto" {
  export function randomUUID(): string;
}

declare module "node:fs" {
  export function appendFileSync(path: string, data: string): void;
  export function mkdirSync(path: string, options?: { recursive?: boolean }): string | undefined;
  export function mkdtempSync(prefix: string): string;
  export function readFileSync(path: string, encoding: "utf-8"): string;
  export function rmSync(path: string, options?: { recursive?: boolean; force?: boolean }): void;
  export function existsSync(path: string): boolean;
}

declare module "node:path" {
  export function dirname(path: string): string;
  export function join(...parts: string[]): string;
}

declare module "node:os" {
  export function tmpdir(): string;
}

declare module "node:async_hooks" {
  export class AsyncLocalStorage<T> {
    run<R>(store: T, callback: () => R): R;
    getStore(): T | undefined;
  }
}
